import csv
import importlib.util
import io
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "phase10_summary", ROOT / "scripts/summarize_phase10.py"
)
assert spec and spec.loader
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def test_offline_rebuild_denominators_and_separate_scoring(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline summary must not access network")

    monkeypatch.setattr(socket, "socket", forbidden)
    built = summary.build(ROOT)
    records = list(csv.DictReader(io.StringIO(built["agent_tasks.csv"])))
    heldout = {(r["group"], r["metric"]): r for r in records if r["split"] == "heldout"}
    assert heldout["baseline", "confirmed_task_success_lower_bound"]["numerator"] == "20"
    assert heldout["candidate", "supplemental_draft_audited_success"]["numerator"] == "18"
    assert heldout["baseline", "supplemental_draft_audited_success"]["denominator"] == "24"
    regressions = [r for r in records if r["split"] == "regression"]
    assert regressions and all(r["dataset"] == "phase8_regressions" for r in regressions)
    assert all("manifest.json" in r["config_source"] for r in regressions)
    draft = next(
        r for r in records if r["group"] == "drafts-v2" and r["metric"] == "faithful_generated"
    )
    assert (draft["sample_n"], draft["numerator"], draft["denominator"]) == ("12", "11", "11")
    retrieval = list(csv.DictReader(io.StringIO(built["retrieval.csv"])))
    evidence = next(
        r
        for r in retrieval
        if r["group"] == "dense20"
        and r["split"] == "heldout"
        and r["metric"] == "all_required_evidence"
    )
    assert evidence["denominator"] == "52"
    assert float(evidence["value"]) == pytest.approx(44 / 52)
    assert "heldout 60" in built["results.md"]
    assert built == summary.build(ROOT)


def test_rejections_not_successful_throughput_and_no_p95_pooling():
    batch = {
        "size": 3,
        "success": 2,
        "seconds": 2,
        "backend_calls": {"model": 2},
        "requests": [
            {"success": True, "status": 200, "seconds": t, "retrieval": {}} for t in (1, 4)
        ]
        + [{"success": False, "status": 429, "seconds": 0.001, "retrieval": {}}],
    }
    metrics = summary.batch_metrics(batch)
    assert metrics["successful_rps"] == (1, "requests/second")
    assert metrics["rejected_429"] == (1, "requests")
    assert metrics["p95_success"] == (1, "seconds")
    batch["size"] = 4
    with pytest.raises(ValueError, match="denominator"):
        summary.batch_metrics(batch)


@pytest.mark.parametrize("numerator,denominator", [(1, 0), (2, 1), (None, 3), (1, -1)])
def test_invalid_ratios_fail(numerator, denominator):
    with pytest.raises(ValueError):
        summary.ratio(numerator, denominator)


def test_unknown_usage_is_not_zero():
    assert summary.strict_sum([{"tokens": 0}, {}], "tokens") is None
    assert summary.strict_sum([{"tokens": 0}, {"tokens": 0}], "tokens") == 0
    assert summary.quantile([], 0.95) is None


def test_missing_or_corrupt_input_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        summary.build(tmp_path)
    (tmp_path / "invalid.json").write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        summary.Sources(tmp_path).read("invalid.json")
    (tmp_path / "journal.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        summary.Sources(tmp_path).binary("journal.gz", "0" * 64)


def test_incompatible_retrieval_denominator_fails(monkeypatch):
    original = summary.Sources.read

    def corrupted(self, name):
        value = original(self, name)
        if name.endswith("comparison.json"):
            value["splits"]["heldout"]["dense20"]["metrics"]["all_required_evidence"]["n"] = 60
        return value

    monkeypatch.setattr(summary.Sources, "read", corrupted)
    with pytest.raises(ValueError, match="denominator"):
        summary.build(ROOT)
