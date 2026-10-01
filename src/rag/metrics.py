import math
import statistics


def matches(item, locator):
    return (
        item["doc_id"] == locator["doc_id"]
        and item["document_version"] == locator["document_version"]
        and locator["section"] in item["location"]
        and locator["contains"] in item["text"]
    )


def coverage(items, alternatives):
    return max(
        (
            sum(any(matches(item, loc) for item in items) for loc in group) / len(group)
            for group in alternatives
        ),
        default=0.0,
    )


def score_case(case, result):
    candidates, evidence = result["candidates"], result["evidence"]
    if not case["answerable"]:
        return {"evidence_accepted_without_answer_label": bool(evidence)}
    alternatives = case["evidence_alternatives"]
    docs = list(dict.fromkeys(c["doc_id"] for c in candidates[:5]))
    relevant = [{loc["doc_id"] for loc in group} for group in alternatives]
    return {
        "candidate_evidence_recall": coverage(candidates, alternatives),
        "evidence_recall_at_5": coverage(candidates[:5], alternatives),
        "evidence_mrr_at_5": next(
            (
                1 / rank
                for rank, item in enumerate(candidates[:5], 1)
                if any(matches(item, loc) for group in alternatives for loc in group)
            ),
            0,
        ),
        "document_recall_at_5_chunks": max(
            len(set(docs) & group) / len(group) for group in relevant
        ),
        "document_mrr_at_5_chunks": next(
            (
                1 / rank
                for rank, doc in enumerate(docs, 1)
                if any(doc in group for group in relevant)
            ),
            0,
        ),
        "selected_evidence_coverage": coverage(evidence, alternatives),
        "all_required_evidence": coverage(evidence, alternatives) == 1,
        "false_refusal_proxy": not evidence,
    }


def latency(values):
    values = sorted(values)
    return (
        {
            "n": len(values),
            "p50": statistics.median(values),
            "p95_nearest_rank": values[math.ceil(0.95 * len(values)) - 1],
        }
        if values
        else {"n": 0}
    )


def aggregate(records):
    healthy = [
        r
        for r in records
        if r["result"]["status"] in {"ok", "empty", "insufficient"}
        and not r["result"].get("failures")
    ]
    groups = {}
    for record in healthy:
        for key, value in record["metrics"].items():
            groups.setdefault(key, []).append(value)
    stages = {key for r in records for key in r["result"]["timings"]}
    return {
        "n": len(records),
        "normal_n": len(healthy),
        "error_n": sum(
            r["result"]["status"] not in {"ok", "empty", "insufficient"} for r in records
        ),
        "degraded_n": sum(bool(r["result"].get("failures")) for r in records),
        "metrics": {
            key: {"n": len(values), "mean": statistics.mean(values)}
            for key, values in groups.items()
        },
        "latency_all_requests": {
            key: latency(
                [r["result"]["timings"][key] for r in records if key in r["result"]["timings"]]
            )
            for key in stages
        },
        "actual_modes": {
            mode: sum(r["result"]["actual_mode"] == mode for r in records)
            for mode in {r["result"]["actual_mode"] for r in records}
        },
        "usage": {
            key: sum(r["result"]["usage"].get(key, 0) for r in records)
            for key in ["embedding_tokens", "rerank_tokens"]
        },
    }
