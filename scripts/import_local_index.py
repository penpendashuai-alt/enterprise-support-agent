"""Explicit paid import of the frozen v2 snapshot into a new local collection."""

import argparse
import asyncio
import json
from pathlib import Path

from rag.config import get_settings
from rag.embeddings import Embeddings
from rag.models import digest
from rag.snapshot import snapshot_chunks
from rag.vector_store import VectorStore


async def run(args):
    config = get_settings()
    config.require_dense()
    if not config.QDRANT_LOCAL or config.CI_TEST_MODE or not args.paid or not 0 < args.budget <= 20:
        raise ValueError("Explicit local endpoint, paid opt-in and budget (0,20] required")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    chunks = snapshot_chunks(snapshot)
    if snapshot["chunk_contract"] != {
        k: config.index_contract()[k] for k in snapshot["chunk_contract"]
    }:
        raise ValueError("Snapshot chunk contract mismatch")
    upper_tokens = sum(len((c.title + "\n" + c.text).encode()) for c in chunks)
    if upper_tokens * 3 * 0.5 / 1_000_000 > args.budget:
        raise ValueError("Conservative UTF-8 byte cost bound exceeds budget")
    store, embedder = VectorStore(config), Embeddings(config)
    version = digest([snapshot["snapshot_id"], config.index_contract()])
    manifest = {
        "kind": "index_manifest",
        "owner": "enterprise-support-agent",
        "status": "building",
        "index_version": version,
        "snapshot_id": snapshot["snapshot_id"],
        "corpus_hash": snapshot["snapshot_id"],
        "chunk_count": len(chunks),
        "contract": config.index_contract(),
    }
    try:
        if await store.begin(manifest):
            for offset in range(0, len(chunks), config.EMBED_BATCH_SIZE):
                batch = chunks[offset : offset + config.EMBED_BATCH_SIZE]
                vectors = await embedder.embed([c.title + "\n" + c.text for c in batch])
                await store.upsert(batch, vectors, version, snapshot["snapshot_id"])
            rows = await store.inventory()
            expected = {c.chunk_id: (c.content_hash, snapshot["snapshot_id"]) for c in chunks}
            if (
                len(rows) != len(expected)
                or {r["chunk_id"]: (r["content_hash"], r["snapshot_id"]) for r in rows} != expected
            ):
                raise ValueError("Index inventory mismatch")
            await store.write_manifest({**manifest, "status": "ready"})
        print(
            json.dumps(
                {
                    "status": "ready",
                    "index_version": version,
                    "embedding_tokens": embedder.usage_tokens,
                }
            )
        )
    finally:
        await store.close()
        await embedder.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument(
        "--snapshot", type=Path, default=Path("evaluation/datasets/snapshot_v2.json")
    )
    asyncio.run(run(parser.parse_args()))
