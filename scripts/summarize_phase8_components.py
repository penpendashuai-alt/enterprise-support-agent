"""Offline exact-source coverage, separate from semantic judgments."""

import argparse
import json
from pathlib import Path

from eval_support.schema import load_dataset
from eval_support.scoring import ratio


def covered(point, chunks):
    return any(
        all(
            any(c["doc_id"] == source.doc_id and source.quote in c["text"] for c in chunks)
            for source in group
        )
        for group in point.evidence_any_of
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("evaluation/datasets/phase8_regressions.json")
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve earlier component reports")
    cases = {c.case_id: c for c in load_dataset(args.dataset).cases}
    manifest = json.loads((args.input / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for cid in manifest["records"]:
        record = json.loads((args.input / f"{cid}.json").read_text(encoding="utf-8"))
        for point in cases[cid].points:
            if not point.evidence_any_of:
                continue
            result = record["retrieval"]
            rows.append(
                {
                    "case_id": cid,
                    "point_id": point.point_id,
                    "candidate_covered": covered(point, result["candidates"]),
                    "evidence_covered": covered(point, result["evidence"]),
                }
            )
    report = {
        "method": "Exact annotated source quote coverage; not semantic support or end-to-end success",
        "router_bypassed": True,
        "planned_tasks": len(manifest["planned_cases"]),
        "completed_tasks": len(manifest["records"]),
        "points": rows,
        "candidate_coverage": ratio(sum(r["candidate_covered"] for r in rows), len(rows)),
        "evidence_coverage": ratio(sum(r["evidence_covered"] for r in rows), len(rows)),
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
