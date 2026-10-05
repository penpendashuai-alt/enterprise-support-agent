import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from eval_support.ledger import BudgetExceeded, Ledger, write  # noqa: E402


def test_unknown_usage_stops_retries_and_future_runs(tmp_path):
    ledger = Ledger(tmp_path, "run", 30, 1200)
    call = ledger.reserve("chat")
    ledger.finish(call, error="TimeoutError")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("chat")
    with pytest.raises(BudgetExceeded):
        Ledger(tmp_path, "new", 30, 1200).reserve("chat")
    assert len(ledger.events()) == 1
    assert ledger.events()[0]["estimated_cny"] is None


def test_headroom_and_physical_call_limit(tmp_path):
    ledger = Ledger(tmp_path, "run", 1.1, 1200)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("chat", reservation=0.5)
    ledger = Ledger(tmp_path, "run", 30, 1)
    key = ledger.reserve("chat")
    ledger.finish(key, {"input_tokens": 100, "output_tokens": 10})
    with pytest.raises(BudgetExceeded):
        ledger.reserve("embedding")
    assert len(ledger.events()) == 1


def test_transient_windows_reader_lock_does_not_lose_journal(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []

    def locked_once(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("Reader temporarily holds destination")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", locked_once)
    write(tmp_path / "record.json", {"status": "complete"})
    assert len(attempts) == 2
    assert '"status": "complete"' in (tmp_path / "record.json").read_text()
