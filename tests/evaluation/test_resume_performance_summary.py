import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from summarize_resume_campaign import digest  # noqa: E402
from summarize_resume_performance import agent, distribution, paired_mean  # noqa: E402


def test_nearest_rank_and_median_do_not_average_batch_percentiles():
    result = distribution(list(range(1, 101)))
    assert result["p50_seconds"] == 50.5
    assert result["p95_seconds_nearest_rank"] == 95
    assert distribution([])["mean_seconds"] is None


def test_summary_retains_incorrect_answers_warm_misses_and_unexecuted(tmp_path):
    manifest = {"planned_cases": ["measured", "missing", "warmup"]}
    record = {
        "case_id": "measured",
        "measurement": True,
        "cache_condition": "warm",
        "status": "completed",
        "structural": {"passed": True},
        "turns": [
            {
                "http_status": 200,
                "http_response_seconds": 2,
                "user_message": "question",
                "response": {"custom_data": {"cache": {"status": "miss"}}},
            }
        ],
    }
    for name, data in [
        ("manifest", manifest),
        ("measured", record),
        ("warmup", {"measurement": False}),
    ]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    result = agent(
        tmp_path,
        [{"case_id": "measured", "record_sha256": digest(record), "semantic_success": False}],
    )
    assert result["unexecuted"] == ["missing"]
    assert result["warmup_count"] == 1
    condition = result["conditions"]["warm"]
    assert condition["all_http_responses"]["n"] == 1
    assert condition["successful_responses"]["n"] == 0
    assert condition["cache_states"] == {"miss": 1}


def test_latency_comparison_resamples_paired_queries_not_independent_repeats():
    conditions = {mode: [] for mode in ("off", "warm")}
    for query, baseline in (("short", 1), ("long", 100)):
        for _ in range(5):
            conditions["off"].append({"query_cluster": query, "seconds": baseline})
            conditions["warm"].append({"query_cluster": query, "seconds": baseline - 0.5})
    result = paired_mean(conditions)
    assert result["query_clusters"] == 2
    assert result["warm_minus_off_mean_seconds"] == -0.5
    assert result["query_cluster_bootstrap_95"] == [-0.5, -0.5]
