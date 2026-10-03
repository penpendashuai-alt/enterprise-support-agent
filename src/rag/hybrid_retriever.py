import asyncio
import json
from time import perf_counter

from rag.config import RAGSettings
from rag.embeddings import Embeddings
from rag.fusion import reciprocal_rank_fusion
from rag.lexical_store import LexicalStore
from rag.models import Candidate, RAGError, RetrievalResult
from rag.reranker import Reranker
from rag.vector_store import VectorStore


def select_evidence(rows: list[Candidate], settings: RAGSettings, mode: str):
    kind = "rerank" if mode.endswith("rerank") else "rrf" if mode == "hybrid" else mode
    threshold = {
        "dense": settings.RAG_DENSE_THRESHOLD,
        "bm25": settings.RAG_BM25_THRESHOLD,
        "rrf": settings.RAG_RRF_THRESHOLD,
        "rerank": settings.RAG_RERANK_THRESHOLD,
    }[kind]
    evidence, decisions = [], []
    remaining = settings.RAG_CONTEXT_CHARS - 2
    for row in rows:
        if row.score_type != kind:
            raise RAGError("selection_score_type_mismatch")
        item = row.model_copy(
            update={"number": len(evidence) + 1, "text": row.text[: settings.RAG_SNIPPET_CHARS]}
        )
        length = len(json.dumps(item.model_dump(), ensure_ascii=False)) + (2 if evidence else 0)
        reason = (
            "threshold"
            if row.score < threshold
            else "final_k"
            if len(evidence) >= settings.RAG_FINAL_K
            else "character_budget"
            if length > remaining
            else "selected"
        )
        decisions.append(
            {"chunk_id": row.chunk_id, "reason": reason, "score_type": kind, "threshold": threshold}
        )
        if reason == "selected":
            evidence.append(item)
            remaining -= length
    return evidence, decisions


def transient(error):
    return isinstance(error, RAGError) and error.code.endswith(
        ("unavailable", "timeout", "rate_limit")
    )


class HybridRetriever:
    def __init__(
        self, settings: RAGSettings, dense=None, lexical=None, embedder=None, reranker=None
    ):
        self.settings = settings
        mode = settings.RAG_RETRIEVAL_MODE
        self.dense = dense or (VectorStore(settings) if mode != "bm25" else None)
        self.embedder = embedder or (Embeddings(settings) if mode != "bm25" else None)
        self.lexical = lexical or (
            LexicalStore(settings) if mode in {"bm25", "hybrid", "hybrid_rerank"} else None
        )
        self.reranker = reranker or (Reranker(settings) if mode.endswith("rerank") else None)

    async def close(self):
        for client in [self.dense, self.lexical, self.embedder, self.reranker]:
            if client:
                await client.close()

    async def retrieve(self, query: str):
        from core import settings

        if (
            settings.RAG_CACHE_ENABLED
            and self.settings.RAG_RETRIEVAL_MODE == "dense"
            and not self.settings.RAG_DENSE_LEGACY
        ):
            from rag.cache import runtime

            return await runtime().retrieve(self, query)
        return await self._retrieve(query)

    async def _retrieve(self, query: str):
        settings = self.settings
        requested = settings.RAG_RETRIEVAL_MODE
        actual = requested
        started = perf_counter()
        timings, usage, versions, counts = {}, {}, {}, {}
        failures, manifests = [], {}
        snapshot_id = None

        async def stage(name, coroutine, timeout):
            start = perf_counter()
            try:
                async with asyncio.timeout(timeout):
                    return await coroutine
            except TimeoutError:
                return RAGError(f"{name}_timeout")
            except RAGError as exc:
                return exc
            except Exception:
                return RAGError(f"{name}_invalid_response")
            finally:
                timings[name + "_seconds"] = perf_counter() - start

        def check_failures(results, allow_partial):
            good = {}
            for name, value in results.items():
                if isinstance(value, Exception):
                    code = value.code if isinstance(value, RAGError) else "retrieval_failed"
                    failures.append({"stage": name, "reason": code})
                    if not (allow_partial and settings.RAG_ALLOW_FALLBACK and transient(value)):
                        raise value
                else:
                    good[name] = value
            if not good:
                raise RAGError("retrieval_unavailable")
            return good

        async def dense_search(meta):
            assert self.embedder is not None and self.dense is not None
            t = perf_counter()
            vectors, consumed = await self.embedder.embed_with_usage([query])
            usage.update(consumed)
            timings["embedding_seconds"] = perf_counter() - t
            t = perf_counter()
            hits = await self.dense.search(vectors[0], settings.RAG_DENSE_CANDIDATES)
            timings["qdrant_query_seconds"] = perf_counter() - t
            rows = []
            for rank, hit in enumerate(hits, 1):
                payload = hit.payload or {}
                if (
                    payload.get("index_version") != meta["index_version"]
                    or payload.get("snapshot_id") != meta["snapshot_id"]
                ):
                    raise RAGError("index_dense_payload_mismatch")
                rows.append(
                    Candidate.model_validate(
                        {
                            **payload,
                            "number": 0,
                            "score": hit.score,
                            "score_type": "dense",
                            "dense_score": hit.score,
                            "dense_rank": rank,
                            "collection": self.dense.collection,
                        }
                    )
                )
            return rows

        try:
            async with asyncio.timeout(settings.RAG_TIMEOUT):
                backends = [
                    (name, client, timeout)
                    for name, client, timeout in [
                        ("dense", self.dense, settings.QDRANT_TIMEOUT),
                        ("bm25", self.lexical, settings.ES_TIMEOUT),
                    ]
                    if client
                ]
                values = await asyncio.gather(
                    *(
                        stage(name + "_index", client.manifest(), timeout)
                        for name, client, timeout in backends
                    )
                )
                manifests = check_failures(
                    dict(zip([b[0] for b in backends], values, strict=True)), len(backends) == 2
                )
                for name, meta in manifests.items():
                    if meta.get("status") != "ready" or not meta.get("snapshot_id"):
                        raise RAGError("index_not_ready_or_snapshot_missing")
                    versions[name] = meta["index_version"]
                snapshots = {m["snapshot_id"] for m in manifests.values()}
                if len(snapshots) != 1:
                    raise RAGError("index_pair_snapshot_mismatch")
                snapshot_id = snapshots.pop()
                names, operations = [], []
                if "dense" in manifests:
                    names.append("dense")
                    operations.append(
                        stage("dense", dense_search(manifests["dense"]), settings.RAG_TIMEOUT)
                    )
                if "bm25" in manifests:
                    assert self.lexical is not None
                    names.append("bm25")
                    operations.append(
                        stage(
                            "bm25",
                            self.lexical.search(
                                query, settings.RAG_BM25_CANDIDATES, manifests["bm25"]
                            ),
                            settings.ES_TIMEOUT,
                        )
                    )
                recalled = check_failures(
                    dict(zip(names, await asyncio.gather(*operations), strict=True)),
                    len(backends) == 2,
                )
                counts.update({name: len(rows) for name, rows in recalled.items()})
                if len(recalled) == 2:
                    candidates = reciprocal_rank_fusion(
                        recalled["dense"], recalled["bm25"], settings.RAG_RRF_C
                    )
                    actual = "hybrid"
                else:
                    actual, candidates = next(iter(recalled.items()))
                counts["union"] = len(candidates)
                if self.reranker:
                    input_rows = candidates[: settings.RERANK_MAX_CANDIDATES]
                    counts["rerank_input"] = len(input_rows)
                    ranked = await stage(
                        "rerank", self.reranker.rerank(query, input_rows), settings.RERANK_TIMEOUT
                    )
                    if isinstance(ranked, RAGError):
                        failures.append({"stage": "rerank", "reason": ranked.code})
                        if not (settings.RAG_ALLOW_FALLBACK and transient(ranked)):
                            raise ranked
                    else:
                        candidates, consumed = ranked
                        usage.update(consumed)
                        actual = (
                            "hybrid_rerank"
                            if actual == "hybrid"
                            else "dense_rerank"
                            if actual == "dense"
                            else "bm25_rerank"
                        )
                evidence, decisions = select_evidence(candidates, settings, actual)
                return RetrievalResult(
                    status="ok" if evidence else "insufficient" if candidates else "empty",
                    query=query,
                    evidence=evidence,
                    candidates=candidates,
                    collection=settings.QDRANT_COLLECTION if self.dense else settings.ES_INDEX,
                    index_version=versions.get("dense", versions.get("bm25")),
                    requested_mode=requested,
                    actual_mode=actual,
                    snapshot_id=snapshot_id,
                    backend_versions=versions,
                    selection=decisions,
                    counts=counts,
                    failures=failures,
                    timings={**timings, "retrieval_seconds": perf_counter() - started},
                    usage=usage,
                )
        except (RAGError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, RAGError) else "retrieval_timeout"
            status = (
                "index_inconsistent"
                if code.startswith("index_")
                else "configuration_error"
                if code.endswith(
                    ("auth", "input", "input_limit", "missing", "invalid_response", "mismatch")
                )
                else "unavailable"
            )
            return RetrievalResult(
                status=status,
                query=query,
                error_code=code,
                requested_mode=requested,
                actual_mode=actual,
                failures=failures,
                snapshot_id=snapshot_id,
                backend_versions=versions,
                counts=counts,
                timings={**timings, "retrieval_seconds": perf_counter() - started},
                usage=usage,
            )
