"""Balanced real retrieval cache experiment; all physical calls share campaign limits."""

import json
import random
from time import perf_counter
from uuid import uuid4

from eval_support.ledger import now, write
from eval_support.schema import digest

from resume_support.runtime import PhysicalObserver


async def run(args):
    from core import settings
    from execution.redis_runtime import initialize_redis
    from rag.config import get_settings
    from rag.hybrid_retriever import HybridRetriever

    args.output.mkdir(parents=True, exist_ok=False)
    dataset = json.loads((args.campaign / "datasets/retrieval.json").read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["split"] == "dev" and c["answerable"]]
    queries = [c for c in cases if c["case_id"].endswith("-1")]
    queries += [c for c in cases if c["case_id"].endswith("-3")][:5]
    assert len(queries) == 20
    random.Random(20261022).shuffle(queries)
    conditions = ("off", "cold", "warm")
    order = [
        {"case_id": q["case_id"], "repeat": repeat, "condition": condition}
        for repeat in range(2)
        for index, q in enumerate(queries)
        for condition in conditions[(index + repeat) % 3 :] + conditions[: (index + repeat) % 3]
    ]
    config = get_settings()
    observer = PhysicalObserver(args.campaign, "performance", uuid4().hex)
    restore = observer.patch_embeddings()
    backend = HybridRetriever(config)
    settings.RAG_CACHE_ENABLED = True
    report = {
        "run_id": observer.run_id,
        "layer": "real local formal 72-block Qdrant; real DashScope; no chat",
        "started_at": now(),
        "dataset_sha256": digest(dataset),
        "index": await backend.dense.manifest(),
        "source_sha256": digest((args.source / "src/rag/cache.py").read_text(encoding="utf-8")),
        "planned_measurements": order,
        "warmup_policy": "one fresh namespace per condition; warm prefill immediately before measurement; prefill billed, not measured",
        "records": [],
        "status": "running",
    }
    write(args.output / "report.json", report)
    by_id = {q["case_id"]: q for q in queries}
    reference = {}
    try:
        async with initialize_redis():
            for item in order:
                observer.case_id = f"{item['case_id']}-{item['repeat']}-{item['condition']}"
                settings.REDIS_NAMESPACE = "esa:resume:cache:" + uuid4().hex
                settings.RAG_CACHE_ENABLED = item["condition"] != "off"
                query = by_id[item["case_id"]]["query"]
                warmup = None
                if item["condition"] == "warm":
                    observer.turn = -1
                    warmup = (await backend.retrieve(query)).model_dump()
                    if observer.exhausted:
                        report["interrupted_warmup"] = {**item, "result": warmup}
                        break
                observer.turn = 0
                before = len(observer.events())
                point = perf_counter()
                result = (await backend.retrieve(query)).model_dump()
                elapsed = perf_counter() - point
                signature = digest(result["evidence"])
                reference.setdefault(item["case_id"], signature)
                expected_cache = {"off": None, "cold": "miss", "warm": "hit"}[item["condition"]]
                row = {
                    **item,
                    "seconds": elapsed,
                    "result": result,
                    "warmup": warmup,
                    "measurement_attempts": len(observer.events()) - before,
                    "evidence_equal": reference[item["case_id"]] == signature,
                    "expected_cache_state": expected_cache,
                    "cache_state_matches": result.get("cache", {}).get("status") == expected_cache,
                }
                report["records"].append(row)
                write(args.output / "report.json", report)
                print(
                    json.dumps({**item, "seconds": elapsed, "status": result["status"]}), flush=True
                )
                if observer.exhausted:
                    break
    finally:
        await backend.close()
        restore()
        observer.export()
        report.update(
            finished_at=now(),
            status="complete"
            if len(report["records"]) == len(order) and not observer.exhausted
            else "incomplete",
        )
        write(args.output / "report.json", report)
