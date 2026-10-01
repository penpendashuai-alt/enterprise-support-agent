"""Verify the configured cloud services using only a newly created disposable collection."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from qdrant_client import models

from rag.config import get_settings
from rag.embeddings import Embeddings
from rag.models import Chunk, digest
from rag.vector_store import VectorStore


async def main():
    settings = get_settings()
    settings.require_connections()
    name = f"enterprise_support_dense_probe_{uuid4().hex}"
    settings.QDRANT_COLLECTION = name
    store, embedder = VectorStore(settings), Embeddings(settings)
    report = {
        "date": datetime.now(UTC).isoformat(),
        "collection": name,
        "network": "local Windows to user-configured Europe Qdrant Cloud; provider DashScope Beijing",
        "contract": settings.index_contract(),
    }
    created = False
    try:
        before = await store.client.get_collections()
        counts = {
            c.name: (await store.client.get_collection(c.name)).points_count
            for c in before.collections
        }
        report["existing_point_counts"] = counts
        created = await store.client.create_collection(
            name, vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE)
        )
        if not created:
            raise RuntimeError("Probe creation not confirmed")
        vector, query = await embedder.embed(["VPN 错误 809", "VPN error 809"])
        manifest = {
            "kind": "index_manifest",
            "owner": "enterprise-support-agent",
            "contract": settings.index_contract(),
            "status": "ready",
            "index_version": "probe",
            "chunk_count": 1,
        }
        await store.write_manifest(manifest)
        await store.client.create_payload_index(
            name, "kind", models.PayloadSchemaType.KEYWORD, wait=True
        )
        chunk = Chunk(
            doc_id="probe",
            chunk_id="probe:v1",
            title="连接验证",
            source_type="synthetic",
            source_path="probe",
            url=None,
            document_version="1",
            location="测试",
            text="VPN 错误 809",
            content_hash=digest("VPN 错误 809"),
        )
        await store.upsert([chunk], [vector], "probe")
        await store.manifest()
        hits = await store.search(query, 1)
        assert hits and hits[0].payload["doc_id"] == "probe"
        report.update(
            status="passed",
            dimensions=[len(vector), len(query)],
            score=hits[0].score,
            embedding_tokens=embedder.usage_tokens,
        )
        report["existing_unchanged"] = all(
            [(await store.client.get_collection(c)).points_count == n for c, n in counts.items()]
        )
    except Exception as exc:
        report.update(status="failed", error=getattr(exc, "code", type(exc).__name__))
    finally:
        if created:
            report["probe_deleted"] = await store.client.delete_collection(name)
        await store.close()
        await embedder.close()
        destination = Path("evaluation/results/phase4/cloud-probe.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False))
    return report["status"] == "passed"


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
