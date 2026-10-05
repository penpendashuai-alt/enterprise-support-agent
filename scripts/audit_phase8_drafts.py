"""Apply signed post-experiment draft reviews without replacing frozen scores."""

import argparse
import json
from pathlib import Path

from eval_support.schema import digest
from eval_support.scoring import ratio


def apply(summary, audits):
    tasks = {t["case_id"]: dict(t) for t in summary["tasks"]}
    seen = set()
    for audit in audits:
        cid = audit["case_id"]
        if cid in seen or cid not in tasks:
            raise ValueError("Duplicate or unrelated draft audit")
        seen.add(cid)
        record = json.loads(Path(audit["record_path"]).read_text(encoding="utf-8"))
        if digest(record) != audit["record_sha256"] or record["case_id"] != cid:
            raise ValueError("Draft review no longer matches raw record")
        if audit["status"] != "reviewed" or not audit["rationale"] or not audit["reviewed_at"]:
            raise ValueError("Draft audit requires explicit completed review")
        if audit["semantic_success"] not in {"yes", "no"}:
            raise ValueError("Uncertain drafts require further review, not an automatic failure")
        if audit["semantic_success"] != "yes":
            tasks[cid]["outcome"] = "draft_semantic_failure"
            tasks[cid]["primary_cause"] = "draft_generation"
    return {
        "scope": "Post-experiment supplemental audit; not preregistered heldout metric",
        "frozen_contract_success": summary["confirmed_task_success_lower_bound"],
        "audited_task_success_lower_bound": ratio(
            sum(t["outcome"] == "confirmed_success" for t in tasks.values()), len(tasks)
        ),
        "drafts_reviewed": len(audits),
        "tasks": list(tasks.values()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--audits", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior audit reports")
    audits = json.loads(args.audits.read_text(encoding="utf-8"))
    result = apply(
        json.loads(args.summary.read_text(encoding="utf-8")),
        [a for a in audits if a["group"] == args.group],
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
