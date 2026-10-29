"""Compact review packets; retain evidence text and multi-turn facts, never assign semantic scores."""

import argparse
import json
import random
from pathlib import Path


def compact(record):
    return {
        "status": record["status"],
        "structural": record.get("structural"),
        "turns": [
            {
                "input": turn.get("user_message"),
                "operation": turn["operation"],
                "http_status": turn["http_status"],
                "answer": turn.get("response", {}).get("content"),
                "custom": {
                    k: v
                    for k, v in turn.get("response", {}).get("custom_data", {}).items()
                    if k
                    in {
                        "kind",
                        "draft",
                        "missing_fields",
                        "ticket",
                        "status",
                        "draft_version",
                        "citation_check",
                    }
                },
                "tools": turn.get("tools"),
                "evidence": [
                    {k: e.get(k) for k in ("number", "doc_id", "chunk_id", "text")}
                    for e in (turn.get("retrieval") or {}).get("evidence", [])
                ],
                "ticket_count": turn.get("ticket_count"),
            }
            for turn in record.get("turns", [])
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--campaign", type=Path, default=Path("evaluation/resume-improvement/20261022-r1")
    )
    p.add_argument("--split", default="heldout")
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    args = p.parse_args()
    mapping_file = Path(".cache/resume-20261022/review-display-map.json")
    mapping = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
    cases = json.loads((args.campaign / "datasets/agent.json").read_text(encoding="utf-8"))["cases"]
    for case in cases:
        cid = case["case_id"]
        if case["split"] != args.split or not args.start <= int(cid.rsplit("-", 1)[-1]) <= args.end:
            continue
        print(json.dumps({"case": case}, ensure_ascii=False))
        if cid not in mapping:
            variants = ["baseline", "candidate"]
            random.SystemRandom().shuffle(variants)
            mapping[cid] = dict(zip(("A", "B"), variants))
            mapping_file.parent.mkdir(parents=True, exist_ok=True)
            mapping_file.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
        for label, variant in mapping[cid].items():
            files = list((args.campaign / "heldout" / variant).rglob(cid + ".json"))
            if len(files) != 1:
                print(json.dumps({"display": label, "record_count": len(files)}))
                continue
            print(
                json.dumps(
                    {"display": label, **compact(json.loads(files[0].read_text(encoding="utf-8")))},
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()
