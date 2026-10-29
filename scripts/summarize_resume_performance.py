"""Recompute performance distributions from request records, preserving all failures."""

import argparse
import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from summarize_resume_campaign import digest


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean_seconds": statistics.mean(ordered) if ordered else None,
        "p50_seconds": statistics.median(ordered) if ordered else None,
        "p95_seconds_nearest_rank": ordered[math.ceil(len(ordered) * 0.95) - 1]
        if ordered
        else None,
    }


def intervals(groups):
    """Resample queries; repeated observations never become independent queries."""
    rng = random.Random(20261022)
    groups = list(groups.values())
    if not groups:
        return None
    estimates = defaultdict(list)
    for _ in range(2000):
        sample = [v for group in rng.choices(groups, k=len(groups)) for v in group]
        for key, value in distribution(sample).items():
            if key != "n":
                estimates[key].append(value)
    return {k: [sorted(v)[49], sorted(v)[1949]] for k, v in estimates.items()}


def paired_mean(conditions):
    """Preserve paired query clusters when resampling warm minus off latency."""
    grouped = {mode: defaultdict(list) for mode in ("off", "warm")}
    for mode in grouped:
        for row in conditions.get(mode, []):
            if row["seconds"] is not None:
                grouped[mode][row["query_cluster"]].append(row["seconds"])
    keys = sorted(set(grouped["off"]) & set(grouped["warm"]))
    differences = [
        statistics.mean(grouped["warm"][k]) - statistics.mean(grouped["off"][k]) for k in keys
    ]
    if not differences:
        return None
    rng = random.Random(20261022)
    samples = sorted(statistics.mean(rng.choices(differences, k=len(keys))) for _ in range(10000))
    return {
        "query_clusters": len(keys),
        "warm_minus_off_mean_seconds": statistics.mean(differences),
        "query_cluster_bootstrap_95": [samples[249], samples[9749]],
        "scope": "Equal weight per paired query; all HTTP responses including incorrect answers; repeated observations are not independent queries",
    }


def agent(directory, reviews):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    review = {r["case_id"]: r for r in reviews}
    conditions, warmups = defaultdict(list), []
    pending = []
    for cid in manifest["planned_cases"]:
        file = directory / (cid + ".json")
        if not file.exists():
            pending.append(cid)
            continue
        record = json.loads(file.read_text(encoding="utf-8"))
        if not record["measurement"]:
            warmups.append(cid)
            continue
        r = review.get(cid)
        if r and r["record_sha256"] != digest(record):
            raise ValueError("Review not bound to raw record")
        turn = record["turns"][0] if record["turns"] else {}
        custom = turn.get("response", {}).get("custom_data", {})
        stage = {
            k: float(v) / 1000
            for k, v in re.findall(r"([\w_]+);dur=([\d.]+)", turn.get("server_timing") or "")
        }
        seconds = turn.get("http_response_seconds")
        conditions[record["cache_condition"]].append(
            {
                "case_id": cid,
                "query_cluster": digest(turn.get("user_message")),
                "status": record["status"],
                "http_status": turn.get("http_status"),
                "seconds": seconds,
                "semantic_success": r["semantic_success"] if r else None,
                "success": bool(
                    r
                    and r["semantic_success"]
                    and record.get("structural", {}).get("passed")
                    and turn.get("http_status") == 200
                    and record["status"] == "completed"
                ),
                "citation_check": custom.get("citation_check"),
                "cache": custom.get("cache", {}).get("status", "none"),
                "stage_seconds": stage,
            }
        )
    result = {
        "planned": len(manifest["planned_cases"]),
        "unexecuted": pending,
        "warmup_count": len(warmups),
        "conditions": {},
        "ttft": "not applicable: complete response buffered and validated",
    }
    for condition, rows in conditions.items():
        grouped = defaultdict(list)
        for row in rows:
            if row["seconds"] is not None:
                grouped[row["query_cluster"]].append(row["seconds"])
        result["conditions"][condition] = {
            "attempted": len(rows),
            "reviewed": sum(r["semantic_success"] is not None for r in rows),
            "successful": sum(r["success"] for r in rows),
            "cache_states": dict(Counter(r["cache"] for r in rows)),
            "http_statuses": dict(Counter(r["http_status"] for r in rows)),
            "all_http_responses": distribution(
                [r["seconds"] for r in rows if r["seconds"] is not None]
            ),
            "successful_responses": distribution(
                [r["seconds"] for r in rows if r["success"] and r["seconds"] is not None]
            ),
            "query_cluster_95_all_responses": intervals(grouped),
            "unique_queries": len(grouped),
            "stages_all_responses": {
                key: distribution(
                    [r["stage_seconds"][key] for r in rows if key in r["stage_seconds"]]
                )
                for key in sorted({k for r in rows for k in r["stage_seconds"]})
            },
            "requests": rows,
        }
    result["scope"] = (
        "Single-worker serial synthetic questions; all response latency includes incorrect answers; successful latency requires explicit non-independent semantic review; intended warm condition may miss due to query rewrite or TTL. No production traffic inference."
    )
    result["paired_all_responses"] = paired_mean(conditions)
    return result


def cache(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    output = {
        "status": report["status"],
        "planned": len(report["planned_measurements"]),
        "measured": len(report["records"]),
        "conditions": {},
    }
    for mode in ("off", "cold", "warm"):
        rows = [r for r in report["records"] if r["condition"] == mode]
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["case_id"]].append(row["seconds"])
        output["conditions"][mode] = {
            **distribution([r["seconds"] for r in rows]),
            "query_cluster_95": intervals(grouped),
            "query_count": len(grouped),
            "evidence_equal": sum(r["evidence_equal"] for r in rows),
            "cache_state_matches": sum(r["cache_state_matches"] for r in rows),
            "actual_cache_states": dict(
                Counter(r["result"].get("cache", {}).get("status", "none") for r in rows)
            ),
            "actual_hit_latency": distribution(
                [r["seconds"] for r in rows if r["result"].get("cache", {}).get("status") == "hit"]
            ),
            "actual_hit_provider_attempts": sum(
                r["measurement_attempts"]
                for r in rows
                if r["result"].get("cache", {}).get("status") == "hit"
            ),
            "measurement_provider_attempts": sum(r["measurement_attempts"] for r in rows),
            "status_counts": dict(Counter(r["result"]["status"] for r in rows)),
            "stages": {
                k: distribution(
                    [r["result"]["timings"][k] for r in rows if k in r["result"]["timings"]]
                )
                for k in sorted({k for r in rows for k in r["result"]["timings"]})
            },
        }
    output["warmup_count"] = sum(r["warmup"] is not None for r in report["records"])
    output["paired_all_responses"] = paired_mean(
        {
            mode: [
                {"query_cluster": r["case_id"], "seconds": r["seconds"]}
                for r in report["records"]
                if r["condition"] == mode
            ]
            for mode in ("off", "warm")
        }
    )
    if output["paired_all_responses"]:
        output["paired_all_responses"]["scope"] = (
            "Equal weight per paired query; all component measurements including insufficient evidence; no chat; repeats are not independent queries"
        )
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["agent", "cache"])
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--reviews", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else []
    result = agent(args.input, reviews) if args.mode == "agent" else cache(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(args.output), "conditions": list(result["conditions"])}))


if __name__ == "__main__":
    main()
