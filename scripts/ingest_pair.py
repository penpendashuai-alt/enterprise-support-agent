import argparse
import asyncio
import json
from pathlib import Path

from rag.config import get_settings
from rag.pair_ingestion import build_pair
from rag.snapshot import snapshot_chunks


def phase5_settings():
    settings = get_settings()
    return type(settings).model_validate(
        {
            **settings.model_dump(),
            "QDRANT_COLLECTION": "enterprise_support_dense_v2",
            "ES_INDEX": "enterprise-support-lexical-v2",
            "RERANK_USE_EMBED_KEY": True,
            "RAG_DENSE_LEGACY": False,
        }
    )


async def run(args):
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    snapshot_chunks(snapshot)
    settings = phase5_settings()
    settings.require_dense()
    report = await build_pair(snapshot, settings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print({k: report.get(k) for k in ["status", "chunks", "embedding_tokens", "error"]})
    return report["status"] == "ready"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot", type=Path, default=Path("evaluation/datasets/snapshot_v2.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
