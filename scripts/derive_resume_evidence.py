"""Offline final evidence tables, from preserved records and explicit semantic reviews."""

import json
from collections import Counter
from pathlib import Path

from summarize_resume_campaign import paired, ratio, read, retrieval_summary
from summarize_resume_performance import distribution


def main():
    root = Path("evaluation/resume-improvement/20261022-r1")
    quality = read(root / "derived/heldout-corrected-protocol/quality.json")
    cases = [c for c in read(root / "datasets/agent.json")["cases"] if c["split"] == "heldout"]
    output = {
        "retrieval": {
            "dev": retrieval_summary(root, root / "baseline/retrieval-dev", "dev"),
            "heldout": retrieval_summary(root, root / "heldout/retrieval", "heldout"),
        },
        "agent": {},
    }
    normal_ids = {
        r["case_id"] for r in quality["baseline"]["tasks"] if r["status"] == "completed"
    } & {r["case_id"] for r in quality["candidate"]["tasks"] if r["status"] == "completed"}
    for variant in ("baseline", "candidate"):
        tasks = {r["case_id"]: r for r in quality[variant]["tasks"]}
        groups = {}
        for category in sorted({c["category"] for c in cases}):
            selected = [tasks[c["case_id"]] for c in cases if c["category"] == category]
            groups[category] = ratio(sum(t["full_success"] for t in selected), len(selected))
        expected = [c for c in cases if "draft_expected" in c.get("tags", [])]
        generated, faithful = [], []
        for case in expected:
            cid = case["case_id"]
            record = read(next((root / "heldout" / variant).rglob(cid + ".json")))
            if any(
                t.get("response", {}).get("custom_data", {}).get("kind") == "ticket_approval"
                for t in record["turns"]
            ):
                generated.append(cid)
                if tasks[cid]["semantic_pass"]:
                    faithful.append(cid)
        output["agent"][variant] = {
            "categories": groups,
            "draft_generation_completion": ratio(len(generated), len(expected)),
            "faithful_generated_drafts": ratio(len(faithful), len(generated)),
            "faithful_completion_all_expected": ratio(len(faithful), len(expected)),
            "clarification_tasks": [
                {"case_id": c["case_id"], "success": tasks[c["case_id"]]["full_success"]}
                for c in cases
                if "clarification_expected" in c.get("tags", [])
            ],
            "normal_dependency_paired_subset": ratio(
                sum(tasks[cid]["full_success"] for cid in normal_ids), len(normal_ids)
            ),
        }
    output["agent"]["normal_dependency_paired_sensitivity"] = paired(
        *(
            {"tasks": [r for r in quality[v]["tasks"] if r["case_id"] in normal_ids]}
            for v in ("baseline", "candidate")
        )
    )
    load = read(root / "performance/deterministic-balanced.json")
    output["deterministic_load"] = {}
    for level in (2, 20):
        batches = [b for b in load["batches"] if b["concurrency"] == level]
        rows = [r for b in batches for r in b["requests"]]
        output["deterministic_load"][str(level)] = {
            "attempted": len(rows),
            "successful": sum(r["success"] for r in rows),
            "statuses": dict(Counter(r["status"] for r in rows)),
            "successful_responses": distribution([r["seconds"] for r in rows if r["success"]]),
            "rejected_responses": distribution([r["seconds"] for r in rows if not r["success"]]),
            "successful_throughput_weighted_rps": sum(b["success"] for b in batches)
            / sum(b["seconds"] for b in batches),
            "all_buckets_full_before": all(
                b["effective_tokens_before"] == {"service": 40, "benchmark_user": 20}
                for b in batches
            ),
            "all_resources_zero_after": all(
                all(v["active"] == v["present"] == 0 for v in b["stats_after"]["control"].values())
                for b in batches
            ),
            "all_thread_locks_zero": all(b["stats_after"]["thread_locks"] == 0 for b in batches),
            "scope": "20 total batches, 50 requests each; two rounds/reversed order; one worker; deterministic model/retrieval; 5 workloads pooled within same concurrency; not real LLM capacity",
        }
    path = root / "derived/supporting-evidence.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "retrieval": {
                    k: {t: v[t] for t in ("complete", "no_answer_any_evidence")}
                    for k, v in output["retrieval"].items()
                },
                "agent": output["agent"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
