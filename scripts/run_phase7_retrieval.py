"""Small paid retrieval comparison; at most 30 bounded queries, no chat calls."""

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from core import settings
from execution.redis_runtime import initialize_redis
from rag.config import get_settings
from rag.hybrid_retriever import HybridRetriever


async def run(args):
    if args.output.exists():
        raise ValueError("Preserve billing records")
    config = get_settings()
    assert config.RAG_RETRIEVAL_MODE == "dense" and not config.RAG_DENSE_LEGACY
    assert config.RAG_DENSE_CANDIDATES == 20 and config.RAG_DENSE_THRESHOLD == 0.65
    settings.RAG_CACHE_ENABLED = True
    settings.REDIS_NAMESPACE = "esa:p7:retrieval:" + uuid4().hex
    backend = HybridRetriever(config)
    report = {
        "budget_cny": 10,
        "layer": "real Qdrant and DashScope embedding; no chat",
        "cases": [],
    }
    queries = [
        "VPN 报错 809，设备 LAPTOP-001，应该如何排查？",
        "公司 P1 工单如何判定？必须在几分钟内解决？",
        "演示制度中 MFA 丢失后如何恢复登录？",
    ]
    try:
        async with initialize_redis():
            for repeat in range(2):
                for query in queries:
                    for mode in (
                        ["baseline", "cached", "cached"]
                        if repeat == 0
                        else ["cached", "baseline", "cached"]
                    ):
                        settings.RAG_CACHE_ENABLED = mode == "cached"
                        started = perf_counter()
                        result = await backend.retrieve(query)
                        report["cases"].append(
                            {
                                "repeat": repeat,
                                "mode": mode,
                                "query": query,
                                "seconds": perf_counter() - started,
                                "result": result.model_dump(),
                            }
                        )
                        assert result.status not in {
                            "unavailable",
                            "configuration_error",
                            "index_inconsistent",
                        }, result.error_code
                        reference = next(
                            r
                            for r in report["cases"]
                            if r["query"] == query and r["mode"] == "baseline"
                        )
                        assert result.model_dump()["evidence"] == reference["result"]["evidence"]
            report["status"] = "passed"
    finally:
        await backend.close()
        report["embedding_tokens"] = sum(
            r["result"].get("usage", {}).get("embedding_tokens", 0) for r in report["cases"]
        )
        report["estimated_cny"] = report["embedding_tokens"] * 0.5 / 1_000_000
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"cases": len(report["cases"]), "estimated_cny": report["estimated_cny"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
