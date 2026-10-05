"""Offline review exchange and task summaries. Does not import the runtime."""

import argparse
import json
from pathlib import Path

from eval_support.schema import Review, digest, load_dataset
from eval_support.scoring import costs, export_reviews, summarize


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assemble(dataset, runs, review_paths):
    manifests = [read(p / "manifest.json") for p in runs]
    if any(m["layer"] != "agent" for m in manifests):
        raise ValueError("Component records cannot count as end-to-end tasks")
    for key in [
        "variant",
        "dataset_sha256",
        "source_sha256",
        "prompt_sha256",
        "config",
        "index_manifest",
    ]:
        if len({digest(m[key]) for m in manifests}) != 1:
            raise ValueError(f"Incompatible run shards: {key}")
    if manifests[0]["dataset_sha256"] != digest(dataset.model_dump()):
        raise ValueError("Dataset differs from frozen run")
    planned = [cid for m in manifests for cid in m["planned_cases"]]
    if len(planned) != len(set(planned)):
        raise ValueError("Shards overlap; summarize repeated experiments separately")
    records = [
        read(path / f"{cid}.json")
        for path, m in zip(runs, manifests, strict=True)
        for cid in m["records"]
    ]
    reviews = [Review.model_validate(r) for p in review_paths for r in read(p)]
    keys = [(r.run_id, r.case_id, r.attempt) for r in reviews]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate review judgments")
    cases = [c for c in dataset.cases if c.case_id in planned]
    return cases, records, reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/agent_v1.json"))
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--reviews", type=Path, nargs="*", default=[])
    parser.add_argument("--export-review", action="store_true")
    parser.add_argument("--ledger", type=Path, default=Path("evaluation/phase8/calls"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new derived report path")
    cases, records, reviews = assemble(load_dataset(args.dataset), args.runs, args.reviews)
    if args.export_review:
        report = {
            "reviews": export_reviews(records),
            "packets": [
                {
                    "case": next(c.model_dump() for c in cases if c.case_id == r["case_id"]),
                    "raw_record": r,
                }
                for r in records
            ],
        }
    else:
        report = summarize(cases, records, reviews)
        report["healthy"] = summarize([c for c in cases if c.fault == "none"], records, reviews)
        report["faults"] = summarize([c for c in cases if c.fault != "none"], records, reviews)
        report["campaign_cost"] = costs([read(p) for p in args.ledger.glob("*.json")])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
