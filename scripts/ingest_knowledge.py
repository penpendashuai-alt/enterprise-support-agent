import argparse
import asyncio
import json
from pathlib import Path

from rag.config import get_settings
from rag.ingestion import build_index


async def run(args):
    settings = get_settings()
    settings.require_connections()
    if args.collection:
        settings = type(settings).model_validate(
            {**settings.model_dump(), "QDRANT_COLLECTION": args.collection}
        )
    report = await build_index(args.root, settings)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: report[k] for k in ("status", "files", "chunks", "collection")}, ensure_ascii=False
        )
    )
    return report["status"] == "ready"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data/knowledge"))
    parser.add_argument("--collection")
    parser.add_argument("--report", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
