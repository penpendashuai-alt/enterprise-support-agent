"""Rebuild Phase 7 aggregates from raw records without external calls."""

import hashlib
import json
import platform
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evaluation/phase7"


def percentile(values, p):
    values = sorted(values)
    return values[int((len(values) - 1) * p)] if values else None


def summary():
    result = {"service": {}, "real_retrieval": {}, "real_agent": {}, "cost_cny_estimate": 0}
    for name in [
        "baseline-matrix-v2",
        "cache-matrix-v2",
        "control-matrix-v2",
        "full-matrix",
        "baseline-repeat",
        "cache-repeat",
        "control-repeat",
        "full-repeat",
    ]:
        path = OUT / "final" / f"service-{name}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for batch in data["batches"]:
            requests = batch["requests"]
            failed = [r["seconds"] for r in requests if not r["success"]]
            cache = [
                r["retrieval"]["cache"]
                for r in requests
                if r["success"] and r["retrieval"].get("cache")
            ]
            row = {
                k: batch[k]
                for k in [
                    "size",
                    "concurrency",
                    "workload",
                    "seconds",
                    "success",
                    "successful_rps",
                    "p50_success",
                    "p95_success",
                    "backend_calls",
                ]
            }
            row.update(
                outcomes=dict(
                    Counter(
                        "success"
                        if r["success"]
                        else (r.get("detail") or {}).get("code", str(r["status"]))
                        if isinstance(r.get("detail"), dict)
                        else str(r["status"])
                        for r in requests
                    )
                ),
                p50_failure=percentile(failed, 0.5),
                p95_failure=percentile(failed, 0.95),
                cache=dict(Counter(c.get("status") for c in cache)),
                shared_waiters=sum(bool(c.get("shared")) for c in cache),
                cache_denominator=len(cache),
                peak_model=batch["stats_after"].get("peak_model"),
            )
            controls = batch["stats_after"].get("control", {})
            assert all(v["present"] == v["active"] == 0 for v in controls.values()), "Capacity leak"
            if data["mode"] in {"control", "full"}:
                assert row["peak_model"] <= 3
            rows.append(row)
        result["service"][name] = rows
    retrieval = json.loads((OUT / "live-retrieval.json").read_text(encoding="utf-8"))
    for mode in ["baseline", "miss", "hit"]:
        rows = [
            r
            for r in retrieval["cases"]
            if (r["result"].get("cache", {}).get("status") or "baseline") == mode
        ]
        result["real_retrieval"][mode] = {
            "n": len(rows),
            "mean_seconds": statistics.mean(r["seconds"] for r in rows),
            "p50_seconds": percentile([r["seconds"] for r in rows], 0.5),
            "p95_seconds": percentile([r["seconds"] for r in rows], 0.95),
            "embedding_requests": sum(
                r["result"].get("usage", {}).get("requests", 0) for r in rows
            ),
            "embedding_tokens": sum(
                r["result"].get("usage", {}).get("embedding_tokens", 0) for r in rows
            ),
        }
    live = json.loads((OUT / "live-agent.json").read_text(encoding="utf-8"))
    for enabled in (False, True):
        rows = [
            r
            for r in live["cases"]
            if r["case"].startswith("paired_") and r["cache_enabled"] == enabled
        ]
        result["real_agent"]["full" if enabled else "baseline_flags"] = {
            "n": len(rows),
            "mean_seconds": statistics.mean(r["seconds"] for r in rows),
            "min_seconds": min(r["seconds"] for r in rows),
            "max_seconds": max(r["seconds"] for r in rows),
            "cache": dict(
                Counter(r["retrieval"].get("cache", {}).get("status", "disabled") for r in rows)
            ),
            "citation_number_checks_passed": sum(
                r["response"]["custom_data"].get("citation_check") == "valid_numbers" for r in rows
            ),
        }
    result["cost_cny_estimate"] = sum(
        json.loads(p.read_text(encoding="utf-8")).get("estimated_cny", 0)
        for p in OUT.glob("live*.json")
    )
    result["limits"] = [
        "Single worker; deterministic service throughput is not LLM throughput",
        "Closed-loop finite batches; sample quantiles use floor((n-1)*p), not SLA",
        "Final matrices run sequentially after code freeze; reverse-order selected repeats reduce but do not remove host noise",
        "Buckets persist across consecutive batches; burst depletion and previous batch history affect refusals",
        "Four real Agent samples per arm; inconclusive end-to-end speedup",
        "baseline_flags uses current source with controls/cache disabled, unlike archived Phase 6 service baseline",
    ]
    (OUT / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    environment = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "postgres": "17.11 native Windows",
        "redis": "7.0.15 Ubuntu 5:7.0.15-1ubuntu0.24.04.4 in WSL Ubuntu-24.04",
        "redis_py": "6.4.0",
        "workers": 1,
        "pool_size_per_role": 1,
        "three_postgres_pools_max_total": 3,
        "paid_budget_cny": 10,
        "source_sha256": {
            str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ["src", "scripts"]
            for p in (ROOT / folder).rglob("*.py")
        },
    }
    (OUT / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "service"}, ensure_ascii=False))


if __name__ == "__main__":
    summary()
