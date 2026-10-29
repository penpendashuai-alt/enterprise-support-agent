"""Freeze performance input/order before measurement; no API calls."""

import argparse
import json
import random
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--campaign", type=Path, default=Path("evaluation/resume-improvement/20261022-r1")
    )
    p.add_argument("--phase", choices=["pilot", "confirm"], required=True)
    p.add_argument("--per-condition", type=int, required=True)
    args = p.parse_args()
    if args.per_condition % 20 or args.per_condition < 20:
        p.error("Balanced complete query cycles of 20 required")
    path = args.campaign / f"datasets/performance-{args.phase}.json"
    if path.exists():
        raise ValueError("Preserve performance protocol")
    rows = json.loads((args.campaign / "datasets/retrieval.json").read_text(encoding="utf-8"))[
        "cases"
    ]
    queries = [
        r for r in rows if r["split"] == "dev" and r["answerable"] and r["case_id"].endswith("-1")
    ]
    queries += [
        r for r in rows if r["split"] == "dev" and r["answerable"] and r["case_id"].endswith("-3")
    ][:5]
    assert len(queries) == 20
    cases = []

    def case(q, condition, suffix, warmup=False):
        return {
            "case_id": f"perf-{args.phase}-{condition}-{q['case_id']}-{suffix}",
            "family_id": q["family_id"],
            "split": "regression",
            "category": "performance",
            "tags": [condition, *(["warmup"] if warmup else [])],
            "turns": [
                {
                    "operation": "user",
                    "text": "请根据演示企业知识库说明：" + q["query"],
                    "allowed_tools": ["query_service_status", "get_device_information"],
                    "expected_ticket_count": 0,
                }
            ],
            "points": [
                {
                    "point_id": "faithful",
                    "description": "回答所问条款，引用必须与内容匹配；失败/不完整回答单独报告，不作为成功时延",
                    "evidence_any_of": [],
                }
            ],
            "answerability": "answerable",
        }

    rng = random.Random(20261022)
    ordered = list(queries)
    rng.shuffle(ordered)
    for index, q in enumerate(ordered):
        cases.append(case(q, "warm", "warmup", True))
        for repeat in range(args.per_condition // 20):
            for condition in ("off", "warm") if (index + repeat) % 2 == 0 else ("warm", "off"):
                cases.append(case(q, condition, f"r{repeat + 1}"))
    path.write_text(
        json.dumps(
            {
                "version": f"resume-performance-{args.phase}-v1",
                "description": f"20 query clusters; {args.per_condition} measurements per condition; 20 warmup tasks excluded, each directly precedes its query block to fit the unchanged 300s cache TTL; balanced off/warm order across query/repeat; actual cache hits and query rewrites recorded; one worker/concurrency=1; no TTFT; upper accounting shares performance budget",
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print({"file": str(path), "warmup": 20, "per_condition": args.per_condition, "paid_calls": 0})


if __name__ == "__main__":
    main()
