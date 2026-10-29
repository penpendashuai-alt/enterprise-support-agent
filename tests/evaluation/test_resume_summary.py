import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from summarize_resume_campaign import (  # noqa: E402
    audit_business,
    corrected_structure,
    paired,
    wilson,
)


def test_business_audit_does_not_call_http_200_success():
    case = {
        "final_ticket_count": 0,
        "turns": [
            {"operation": "user", "expected_ticket_count": 0},
            {"operation": "approve", "user": "bob", "expected_ticket_count": 0},
        ],
    }
    record = {
        "turns": [{"http_status": 200, "ticket_count": 1}, {"http_status": 200, "ticket_count": 1}]
    }
    result = audit_business(case, record)
    assert result["unapproved_write"] == 1
    assert result["ownership_violation"] == 1
    assert result["unexpected_or_duplicate_ticket"] == 2


def test_pairing_uses_identity_and_keeps_discordant_outcomes():
    b = {
        "tasks": [
            {"case_id": "one", "family_id": "f1", "full_success": True},
            {"case_id": "two", "family_id": "f2", "full_success": False},
        ]
    }
    c = {
        "tasks": [
            {"case_id": "two", "family_id": "f2", "full_success": True},
            {"case_id": "one", "family_id": "f1", "full_success": False},
        ]
    }
    result = paired(b, c)
    assert result["pairs"] == {"candidate_only": 1, "baseline_only": 1}
    assert result["delta"] == 0
    assert result["family_bootstrap_95"] == [-1, 1]


def test_perfect_small_sample_has_uncertainty():
    assert wilson(10, 10)[0] == pytest.approx(0.7224672)
    assert wilson(10, 10)[1] == pytest.approx(1)
    assert wilson(0, 0) is None


def test_repeat_approval_checks_ticket_identity_as_well_as_count():
    case = {
        "final_ticket_count": 1,
        "turns": [{"operation": "approve", "expected_ticket_count": 1}] * 2,
    }
    record = {
        "turns": [
            {
                "http_status": 200,
                "ticket_count": 1,
                "response": {
                    "custom_data": {"kind": "ticket_result", "ticket": {"ticket_id": tid}}
                },
            }
            for tid in ("one", "two")
        ]
    }
    assert audit_business(case, record)["duplicate_ticket_identity"] == 1


def test_plain_text_confirmation_exception_requires_unchanged_unapproved_draft():
    from copy import deepcopy

    case = {
        "family_id": "free-text-not-approval",
        "turns": [{}, {"operation": "user", "expected_http": 409}],
    }
    payload = {
        "kind": "ticket_approval",
        "draft_id": "d",
        "draft_version": 1,
        "draft": {"impact": "本人"},
    }
    row = {
        "http_status": 200,
        "ticket_count": 0,
        "response": {"custom_data": payload},
        "state": {
            "approval_status": "pending",
            "approved_fingerprint": None,
            "approved_request_key": None,
        },
    }
    record = {
        "structural": {"passed": False, "failures": [{"turn": 1, "reason": "http_status"}]},
        "turns": [deepcopy(row), deepcopy(row)],
    }
    assert corrected_structure(case, record)[0]
    for field, value in [
        ("approval_status", "approved"),
        ("approved_fingerprint", "x"),
        ("approved_request_key", "x"),
    ]:
        changed = deepcopy(record)
        changed["turns"][1]["state"][field] = value
        assert not corrected_structure(case, changed)[0]
    changed = deepcopy(record)
    changed["turns"][1]["response"]["custom_data"]["draft_version"] = 2
    assert not corrected_structure(case, changed)[0]
