"""Offline derivation: no .env, network, provider SDK or manually copied totals."""

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def wilson(success, n):
    if not n:
        return None
    z, p = 1.959963984540054, success / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [centre - margin, centre + margin]


def ratio(success, n):
    return {
        "numerator": success,
        "denominator": n,
        "value": success / n if n else None,
        "wilson_95_descriptive": wilson(success, n),
    }


def audit_business(case, record):
    failures = Counter()
    before = 0
    approved_ids = []
    for expected, actual in zip(case["turns"], record.get("turns", [])):
        count = actual.get("ticket_count", before)
        approval = (
            expected.get("operation", "user") == "approve"
            and expected.get("user", "alice") == "alice"
            and not expected.get("values", {}).get("approval_version_offset")
        )
        if count > before and not approval:
            failures["unapproved_write"] += 1
        if (
            expected.get("values", {}).get("approval_version_offset")
            and actual.get("http_status") == 200
        ):
            failures["stale_version_accepted"] += 1
        if expected.get("user") == "bob" and actual.get("http_status") == 200:
            failures["ownership_violation"] += 1
        if count > expected.get("expected_ticket_count", case.get("final_ticket_count", 0)):
            failures["unexpected_or_duplicate_ticket"] += 1
        before = count
        body = actual.get("response", {}).get("custom_data", {})
        ticket = (
            body.get("ticket")
            if body.get("kind") == "ticket_result"
            else (body.get("ticket_result") or {}).get("ticket")
        ) or {}
        if approval and ticket.get("ticket_id"):
            approved_ids.append(ticket["ticket_id"])
    if case.get("final_ticket_count", 0) == 1 and len(set(approved_ids)) > 1:
        failures["duplicate_ticket_identity"] += 1
    return dict(failures)


def corrected_structure(case, record):
    original = record.get("structural", {})
    failures = list(original.get("failures", []))
    accepted = []
    if case.get("family_id") == "free-text-not-approval":
        for failure in list(failures):
            i = failure.get("turn", 0)
            if failure.get("reason") != "http_status" or i < 1 or i >= len(record.get("turns", [])):
                continue
            expected = case["turns"][i]
            prior, actual = record["turns"][i - 1 : i + 1]
            payload = actual.get("response", {}).get("custom_data", {})
            state = actual.get("state", {})
            if (
                expected.get("operation") == "user"
                and expected.get("expected_http") == 409
                and actual.get("http_status") == 200
                and actual.get("ticket_count") == prior.get("ticket_count") == 0
                and payload.get("kind") == "ticket_approval"
                and payload == prior.get("response", {}).get("custom_data")
                and state.get("approval_status") == "pending"
                and state.get("approved_fingerprint") is None
                and state.get("approved_request_key") is None
            ):
                failures.remove(failure)
                accepted.append("free_text_confirmation_redisplay_v1")
    return bool(original.get("passed") or (accepted and not failures)), accepted


def agent_summary(root, split, records_directory, review_file, apply_amendments=False):
    cases = [c for c in read(root / "datasets/agent.json")["cases"] if c["split"] == split]
    files = list(records_directory.rglob("a-*.json"))
    records = {}
    interruptions = {}
    for p in files:
        r = read(p)
        if r["case_id"] in records:
            raise ValueError("Duplicate task in one condition")
        records[r["case_id"]] = r
        marker = p.parent / "interruption.json"
        if marker.exists():
            reconciliation = read(marker)
            if reconciliation["record_sha256"] != digest(r):
                raise ValueError("Interruption not bound to original raw record")
            interruptions[r["case_id"]] = reconciliation
    reviews = {r["case_id"]: r for r in read(review_file)} if review_file.exists() else {}
    rows, counts = [], Counter()
    for case in cases:
        cid = case["case_id"]
        record, review = records.get(cid), reviews.get(cid)
        status = record.get("status") if record else "not_run"
        if cid in interruptions:
            status = interruptions[cid]["classification"]
        if review and (not record or review["record_sha256"] != digest(record)):
            raise ValueError("Review is not bound to raw output")
        structural = bool(record and record.get("structural", {}).get("passed"))
        original_structural, corrections = structural, []
        if record and apply_amendments:
            structural, corrections = corrected_structure(case, record)
        business = audit_business(case, record) if record else {}
        semantic = bool(review and review["semantic_success"] is True)
        complete = status == "completed" and structural and semantic and not business
        counts.update(business)
        counts["planned"] += 1
        counts[status] += 1
        counts["structural_pass"] += structural
        counts["structural_original_pass"] += original_structural
        counts["protocol_corrected_tasks"] += bool(corrections)
        counts["reviewed"] += bool(review)
        counts["full_success"] += complete
        rows.append(
            {
                "case_id": cid,
                "family_id": case["family_id"],
                "category": case["category"],
                "status": status,
                "structural_pass": structural,
                "structural_original_pass": original_structural,
                "protocol_corrections": corrections,
                "semantic_pass": semantic,
                "reviewed": bool(review),
                "full_success": complete,
                "business_failures": business,
                "review_reason": review.get("rationale") if review else "pending",
            }
        )
    return {
        "counts": dict(counts),
        "full_success": ratio(counts["full_success"], len(cases)),
        "review_independence": "developer assistant, non-independent; no claim of blinded independent review",
        "protocol_amendments_applied": apply_amendments,
        "tasks": rows,
    }


def paired(baseline, candidate):
    if len(baseline["tasks"]) != len(candidate["tasks"]):
        raise ValueError("Unpaired denominator")
    bs = {r["case_id"]: r for r in baseline["tasks"]}
    counts, families = Counter(), defaultdict(list)
    for row in candidate["tasks"]:
        b = bs[row["case_id"]]
        x, y = int(b["full_success"]), int(row["full_success"])
        counts[
            ("both_success" if y else "baseline_only")
            if x
            else ("candidate_only" if y else "both_fail")
        ] += 1
        families[row["family_id"]].append(y - x)
    randomizer = random.Random(20261022)
    groups = list(families.values())
    differences = []
    for _ in range(10000):
        sampled = [randomizer.choice(groups) for _ in groups]
        differences.append(sum(sum(g) for g in sampled) / sum(len(g) for g in sampled))
    differences.sort()
    return {
        "pairs": dict(counts),
        "family_clusters": len(groups),
        "delta": sum(sum(g) for g in groups) / len(bs),
        "family_bootstrap_95": [differences[249], differences[9749]],
        "seed": 20261022,
        "resamples": 10000,
    }


def retrieval_summary(root, directory, split):
    cases = [c for c in read(root / "datasets/retrieval.json")["cases"] if c["split"] == split]
    sums = Counter()
    rows = []
    for case in cases:
        files = list(directory.rglob(case["case_id"] + ".json"))
        if len(files) > 1:
            raise ValueError("Duplicate retrieval task")
        record = read(files[0]) if files else None
        healthy = record and record["result"]["status"] in {"ok", "empty", "insufficient"}
        sums["planned"] += 1
        sums["healthy"] += bool(healthy)
        sums["not_run"] += not bool(record)
        if case["answerable"]:
            sums["answerable"] += 1
            sums["complete"] += int(bool(healthy and record["metrics"]["all_required_evidence"]))
            for key in (
                "selected_evidence_coverage",
                "evidence_recall_at_5",
                "candidate_evidence_recall",
            ):
                sums[key] += record["metrics"][key] if healthy else 0
        else:
            sums["unanswerable"] += 1
            sums["any_evidence"] += bool(record and record["result"]["evidence"])
        rows.append(
            {
                "case_id": case["case_id"],
                "family_id": case["family_id"],
                "healthy": bool(healthy),
                "metrics": record["metrics"] if record else None,
            }
        )
    return {
        "counts": dict(sums),
        "complete": ratio(sums["complete"], sums["answerable"]),
        "no_answer_any_evidence": ratio(sums["any_evidence"], sums["unanswerable"]),
        "mean_clause_coverage_all_planned": sums["selected_evidence_coverage"] / sums["answerable"],
        "candidate_evidence_recall_at_5_all_planned": sums["evidence_recall_at_5"]
        / sums["answerable"],
        "tasks": rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--campaign", type=Path, default=Path("evaluation/resume-improvement/20261022-r1")
    )
    p.add_argument("--split", default="dev", choices=["dev", "heldout"])
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path)
    p.add_argument("--baseline-reviews", type=Path, required=True)
    p.add_argument("--candidate-reviews", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--apply-protocol-amendments", action="store_true")
    args = p.parse_args()
    result = {
        "baseline": agent_summary(
            args.campaign,
            args.split,
            args.baseline,
            args.baseline_reviews,
            args.apply_protocol_amendments,
        )
    }
    if args.candidate:
        result["candidate"] = agent_summary(
            args.campaign,
            args.split,
            args.candidate,
            args.candidate_reviews,
            args.apply_protocol_amendments,
        )
        result["paired"] = paired(result["baseline"], result["candidate"])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "quality.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output / "tasks.csv").open("w", encoding="utf-8-sig", newline="") as f:
        keys = [
            "variant",
            "case_id",
            "family_id",
            "category",
            "status",
            "structural_pass",
            "structural_original_pass",
            "protocol_corrections",
            "semantic_pass",
            "reviewed",
            "full_success",
            "business_failures",
            "review_reason",
        ]
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for variant in ("baseline", "candidate"):
            for task in result.get(variant, {}).get("tasks", []):
                writer.writerow({"variant": variant, **task})
    print(json.dumps({k: v.get("counts", v) for k, v in result.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
