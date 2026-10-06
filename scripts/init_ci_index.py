"""Seed only an explicitly selected CI collection with deterministic vectors."""

import asyncio
import json

from rag.config import get_settings
from rag.models import Chunk, digest
from rag.vector_store import VectorStore


async def main():
    config = get_settings()
    config.require_dense()
    if not config.CI_TEST_MODE:
        raise ValueError("Only CI_TEST_MODE collections may be seeded")
    chunk = Chunk(
        doc_id="ci-mfa",
        chunk_id="ci-mfa-1",
        title="CI 演示制度",
        source_type="synthetic",
        source_path="ci-fixture",
        url=None,
        document_version="ci-v1",
        location="第1段",
        text="演示制度要求启用 MFA。",
        content_hash=digest("ci-mfa-v1"),
    )
    manifest = {
        "owner": "enterprise-support-agent",
        "status": "building",
        "index_version": "ci-v1",
        "snapshot_id": "ci-v1",
        "chunk_count": 1,
        "contract": config.index_contract(),
    }
    store = VectorStore(config)
    try:
        if await store.begin(manifest):
            await store.upsert([chunk], [[1.0] + [0.0] * 1023], "ci-v1", "ci-v1")
            await store.write_manifest({**manifest, "status": "ready"})
        assert await store.chunk_count() == 1
        assert len(await store.search([1.0] + [0.0] * 1023, 5)) == 1
        print(json.dumps({"index": "ci-v1", "count": 1, "paid_calls": 0}))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
