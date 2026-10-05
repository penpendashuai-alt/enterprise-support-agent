"""Pair offline summaries by task ID; never combine variants into extra tasks."""

import argparse
import json
from pathlib import Path


def compare(baseline, candidate):
    left = {r["case_id"]: r for r in baseline["tasks"]}
    right = {r["case_id"]: r for r in candidate["tasks"]}
    if left.keys() != right.keys():
        raise ValueError("Paired runs must have identical planned tasks")
    transitions = []
    for key, before in left.items():
        after = right[key]
        transitions.append(
            {
                "case_id": key,
                "baseline": before["outcome"],
                "candidate": after["outcome"],
                "change": "improved"
                if after["outcome"] == "confirmed_success"
                and before["outcome"] != "confirmed_success"
                else "regressed"
                if before["outcome"] == "confirmed_success"
                and after["outcome"] != "confirmed_success"
                else "unchanged",
            }
        )
    return {
        "paired_tasks": len(left),
        "transitions": transitions,
        "improved": sum(r["change"] == "improved" for r in transitions),
        "regressed": sum(r["change"] == "regressed" for r in transitions),
        "baseline": baseline,
        "candidate": candidate,
        "interpretation": "Single paired run on synthetic families; nonindependent review. No statistical significance or production generalization claim.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior paired summaries")
    result = compare(
        json.loads(args.baseline.read_text(encoding="utf-8")),
        json.loads(args.candidate.read_text(encoding="utf-8")),
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
