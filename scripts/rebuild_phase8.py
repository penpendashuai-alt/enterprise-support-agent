"""Rebuild final reports from committed raw records and signed reviews, offline."""

import argparse
import json
from pathlib import Path

from audit_phase8_drafts import apply
from compare_phase8 import compare
from eval_support.schema import load_dataset
from eval_support.scoring import costs, summarize
from summarize_phase8 import assemble


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new derived report directory")
    args.output.mkdir(parents=True)
    root = Path("evaluation/phase8")
    dataset = load_dataset("evaluation/datasets/agent_v1.json")
    regressions = load_dataset("evaluation/datasets/phase8_regressions.json")
    audits = json.loads((root / "draft-audit.json").read_text(encoding="utf-8"))
    summaries = {}
    groups = {
        "dev-baseline-interrupted": (
            dataset,
            [root / "runs/dev-baseline"],
            "reviews.json",
            "dev-baseline",
        ),
        "dev-baseline": (
            dataset,
            [root / "runs/dev-baseline-v2"],
            "reviews.v2.json",
            "dev-baseline-v2",
        ),
        "dev-candidate": (
            dataset,
            [root / "runs/dev-candidate"],
            "reviews.v2.json",
            "dev-candidate",
        ),
        "cache": (
            dataset,
            [root / "runs/cache-dev", root / "runs/cache-heldout"],
            "reviews.json",
            "cache-heldout",
        ),
    }
    for variant in ["baseline", "candidate"]:
        groups["heldout-" + variant] = (
            dataset,
            sorted(p for p in (root / "heldout").glob(variant + "-*") if p.is_dir()),
            "reviews.json",
            variant + "-heldout",
        )
        groups["regression-" + variant] = (
            regressions,
            [root / ("runs/regression-" + variant)],
            "reviews.json",
            None,
        )

    def save(name, value):
        (args.output / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    for name, (data, runs, review_name, audit_group) in groups.items():
        cases, records, reviews = assemble(
            data, runs, [p / review_name for p in runs if (p / review_name).exists()]
        )
        summary = summarize(cases, records, reviews)
        summary["healthy"] = summarize([c for c in cases if c.fault == "none"], records, reviews)
        summary["faults"] = summarize([c for c in cases if c.fault != "none"], records, reviews)
        if audit_group:
            summary["supplemental_draft_audit"] = apply(
                summary, [a for a in audits if a["group"] == audit_group]
            )
        summaries[name] = summary
        save(name, summary)
    for split in ["dev", "heldout", "regression"]:
        save(
            split + "-paired",
            compare(summaries[split + "-baseline"], summaries[split + "-candidate"]),
        )
    events = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "calls").glob("*.json")]
    save(
        "cost",
        {
            "providers": costs(events),
            "unique_calls": len({e["call_id"] for e in events}),
            "known_cny": sum(e["estimated_cny"] or 0 for e in events),
            "unknown_usage": sum(e["estimated_cny"] is None for e in events),
        },
    )


if __name__ == "__main__":
    main()
