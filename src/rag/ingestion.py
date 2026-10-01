from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from rag.chunker import chunk_document
from rag.config import RAGSettings
from rag.embeddings import Embeddings
from rag.loader import load_document, read_manifest
from rag.models import RAGError, digest
from rag.vector_store import VectorStore


async def build_index(root: Path, settings: RAGSettings, store=None, embedder=None) -> dict:
    started = perf_counter()
    try:
        raw, specs = read_manifest(root)
    except Exception as exc:
        return {
            "status": "failed",
            "error": "invalid_manifest",
            "collection": settings.QDRANT_COLLECTION,
            "error_type": type(exc).__name__,
            "files": 0,
            "chunks": 0,
            "failures": [],
            "seconds": perf_counter() - started,
        }
    chunks, failures = [], []
    for spec in specs:
        try:
            chunks.extend(
                chunk_document(
                    spec,
                    load_document(root, spec),
                    settings.RAG_CHUNK_SIZE,
                    settings.RAG_CHUNK_OVERLAP,
                )
            )
        except Exception as exc:
            failures.append(
                {
                    "doc_id": spec.doc_id,
                    "error": exc.code if isinstance(exc, RAGError) else type(exc).__name__,
                }
            )
    report = {
        "files": len(specs),
        "failures": failures,
        "chunks": len(chunks),
        "collection": settings.QDRANT_COLLECTION,
        "contract": settings.index_contract(),
        "parse_policy": "all documents must succeed; no activation on partial ingestion",
    }
    if failures or not chunks:
        return {
            **report,
            "status": "failed",
            "error": "parse_failed",
            "seconds": perf_counter() - started,
        }
    corpus_hash = digest({"manifest": raw, "chunks": [c.model_dump() for c in chunks]})
    version = digest([corpus_hash, settings.index_contract()])
    manifest = {
        "kind": "index_manifest",
        "owner": "enterprise-support-agent",
        "status": "building",
        "index_version": version,
        "corpus_hash": corpus_hash,
        "contract": settings.index_contract(),
        "chunk_count": len(chunks),
        "built_at": datetime.now(UTC).isoformat(),
    }
    own_store, own_embedder = store is None, embedder is None
    store = store or VectorStore(settings)
    embedder = embedder or Embeddings(settings)
    begun = False
    try:
        begun = await store.begin(manifest)
        if begun:
            for offset in range(0, len(chunks), settings.EMBED_BATCH_SIZE):
                batch = chunks[offset : offset + settings.EMBED_BATCH_SIZE]
                vectors = await embedder.embed([f"{chunk.title}\n{chunk.text}" for chunk in batch])
                await store.upsert(batch, vectors, version)
            if await store.chunk_count() != len(chunks):
                raise RAGError("index_count_mismatch")
            manifest["status"] = "ready"
            await store.write_manifest(manifest)
        return {
            **report,
            "status": "ready",
            "reused": not begun,
            "index_version": version,
            "corpus_hash": corpus_hash,
            "seconds": perf_counter() - started,
            "embedding_tokens": embedder.usage_tokens,
            "embedding_requests": embedder.requests,
            "retries": embedder.retries,
        }
    except Exception as exc:
        if begun:
            manifest["status"] = "failed"
            try:
                await store.write_manifest(manifest)
            except Exception:
                pass
        return {
            **report,
            "status": "failed",
            "index_version": version,
            "error": exc.code if isinstance(exc, RAGError) else type(exc).__name__,
            "seconds": perf_counter() - started,
            "embedding_tokens": embedder.usage_tokens,
        }
    finally:
        if own_store:
            await store.close()
        if own_embedder:
            await embedder.close()
