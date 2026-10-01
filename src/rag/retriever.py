import asyncio
import json
from time import perf_counter

from rag.config import RAGSettings, get_settings
from rag.embeddings import Embeddings
from rag.models import Evidence, RAGError, RetrievalResult
from rag.vector_store import VectorStore


class Retriever:
    def __init__(self, settings: RAGSettings, store=None, embedder=None):
        self.settings = settings
        self.store = store or VectorStore(settings)
        self.embedder = embedder or Embeddings(settings)

    async def close(self):
        await self.store.close()
        await self.embedder.close()

    async def retrieve(self, query: str) -> RetrievalResult:
        started = perf_counter()
        timings = {}
        usage = {}
        try:
            async with asyncio.timeout(self.settings.RAG_TIMEOUT):
                point = perf_counter()
                manifest = await self.store.manifest()
                timings["index_check_seconds"] = perf_counter() - point
                if manifest.get("status") != "ready":
                    raise RAGError("index_not_ready")
                point = perf_counter()
                vectors, usage = await self.embedder.embed_with_usage([query])
                vector = vectors[0]
                timings["embedding_seconds"] = perf_counter() - point
                point = perf_counter()
                hits = await self.store.search(vector, self.settings.RAG_TOP_K)
                timings["qdrant_query_seconds"] = perf_counter() - point
                candidates, evidence = [], []
                budget = self.settings.RAG_CONTEXT_CHARS
                for hit in hits:
                    payload = hit.payload or {}
                    if payload.get("index_version") != manifest["index_version"]:
                        raise RAGError("index_payload_mismatch")
                    candidate = Evidence.model_validate(
                        {
                            **payload,
                            "score": hit.score,
                            "number": 0,
                            "collection": self.store.collection,
                        }
                    )
                    candidate.text = candidate.text[: self.settings.RAG_SNIPPET_CHARS]
                    candidates.append(candidate)
                    if hit.score < self.settings.RAG_MIN_SCORE:
                        continue
                    item = candidate.model_copy(update={"number": len(evidence) + 1})
                    length = len(json.dumps(item.model_dump(), ensure_ascii=False))
                    if length <= budget:
                        evidence.append(item)
                        budget -= length
                return RetrievalResult(
                    status="ok" if evidence else "insufficient" if candidates else "empty",
                    query=query,
                    evidence=evidence,
                    candidates=candidates,
                    index_version=manifest["index_version"],
                    collection=self.store.collection,
                    timings={**timings, "retrieval_seconds": perf_counter() - started},
                    usage=usage,
                )
        except (RAGError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, RAGError) else "retrieval_timeout"
            return RetrievalResult(
                status="configuration_error"
                if code.startswith(("index_", "unknown_", "embedding_input"))
                else "unavailable",
                query=query,
                collection=self.store.collection,
                error_code=code,
                timings={**timings, "retrieval_seconds": perf_counter() - started},
                usage=usage,
            )
        except Exception:
            return RetrievalResult(
                status="unavailable",
                query=query,
                error_code="retrieval_failed",
                timings={"retrieval_seconds": perf_counter() - started},
                usage=usage,
            )


_runtimes: dict = {}


async def retrieve(query: str) -> RetrievalResult:
    loop = asyncio.get_running_loop()
    try:
        if loop not in _runtimes:
            settings = get_settings()
            settings.require_mode()
            if settings.RAG_RETRIEVAL_MODE == "dense" and settings.RAG_DENSE_LEGACY:
                _runtimes[loop] = Retriever(settings)
            else:
                from rag.hybrid_retriever import HybridRetriever

                _runtimes[loop] = HybridRetriever(settings)
        return await _runtimes[loop].retrieve(query)
    except (ValueError, RAGError):
        return RetrievalResult(
            status="configuration_error", query=query, error_code="rag_not_configured"
        )


async def close_retriever():
    runtime = _runtimes.pop(asyncio.get_running_loop(), None)
    if runtime:
        await runtime.close()
