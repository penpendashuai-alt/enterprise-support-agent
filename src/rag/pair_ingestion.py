from datetime import UTC, datetime
from time import perf_counter

from rag.embeddings import Embeddings
from rag.lexical_store import LexicalStore, lexical_contract
from rag.models import RAGError, digest
from rag.snapshot import snapshot_chunks
from rag.vector_store import VectorStore


async def build_pair(snapshot, settings, dense=None, lexical=None, embedder=None):
    chunks = snapshot_chunks(snapshot)
    sid = snapshot["snapshot_id"]
    common = {
        "owner": "enterprise-support-agent",
        "status": "building",
        "snapshot_id": sid,
        "chunk_count": len(chunks),
        "built_at": datetime.now(UTC).isoformat(),
    }
    dm = {
        **common,
        "kind": "index_manifest",
        "corpus_hash": sid,
        "contract": settings.index_contract(),
        "index_version": digest([sid, settings.index_contract()]),
    }
    lm = {
        **common,
        "contract": lexical_contract(),
        "index_version": digest([sid, lexical_contract()]),
    }
    own_d, own_l, own_e = dense is None, lexical is None, embedder is None
    dense, lexical, embedder = (
        dense or VectorStore(settings),
        lexical or LexicalStore(settings),
        embedder or Embeddings(settings),
    )
    begun_d, begun_l = False, False
    start = perf_counter()
    report = {
        "snapshot_id": sid,
        "chunks": len(chunks),
        "dense_collection": settings.QDRANT_COLLECTION,
        "es_index": settings.ES_INDEX,
        "dense_version": dm["index_version"],
        "es_version": lm["index_version"],
    }
    try:
        begun_d = await dense.begin(dm)
        begun_l = await lexical.begin(lm)
        if begun_d:
            for offset in range(0, len(chunks), settings.EMBED_BATCH_SIZE):
                batch = chunks[offset : offset + settings.EMBED_BATCH_SIZE]
                vectors = await embedder.embed([f"{c.title}\n{c.text}" for c in batch])
                await dense.upsert(batch, vectors, dm["index_version"], sid)
        if begun_l:
            for offset in range(0, len(chunks), 100):
                await lexical.upsert(chunks[offset : offset + 100], sid, lm["index_version"])
        expected = {c.chunk_id: (c.content_hash, sid) for c in chunks}
        for store in [dense, lexical]:
            rows = await store.inventory()
            if (
                len(rows) != len(expected)
                or {r["chunk_id"]: (r["content_hash"], r["snapshot_id"]) for r in rows} != expected
            ):
                raise RAGError("index_inventory_mismatch")
        if begun_d:
            await dense.write_manifest({**dm, "status": "ready"})
        if begun_l:
            await lexical.write_manifest({**lm, "status": "ready"})
        report.update(status="ready", reused=not begun_d and not begun_l)
    except Exception as exc:
        for store, begun, meta in [(dense, begun_d, dm), (lexical, begun_l, lm)]:
            if begun:
                try:
                    await store.write_manifest({**meta, "status": "failed"})
                except Exception:
                    pass
        report.update(
            status="failed", error=exc.code if isinstance(exc, RAGError) else type(exc).__name__
        )
    finally:
        report.update(
            seconds=perf_counter() - start,
            embedding_tokens=embedder.usage_tokens,
            embedding_requests=embedder.requests,
        )
        for store, own in [(dense, own_d), (lexical, own_l), (embedder, own_e)]:
            if own:
                await store.close()
    return report
