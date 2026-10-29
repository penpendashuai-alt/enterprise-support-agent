import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from review_resume_performance import apply  # noqa: E402


def test_review_cannot_silently_pass_unreviewed_or_unexecuted_tasks():
    packet = {
        "unexecuted": [],
        "groups": [
            {"group_id": "answer", "records": [{"case_id": "one", "record_sha256": "digest"}]}
        ],
    }
    with pytest.raises(ValueError, match="exactly one"):
        apply(packet, [])
    ruling = {
        "group_id": "answer",
        "semantic_success": False,
        "reason": "Unsupported assertion despite HTTP 200",
    }
    result = apply(packet, [ruling])
    assert result[0]["record_sha256"] == "digest"
    assert result[0]["semantic_success"] is False
    packet["unexecuted"] = ["missing"]
    with pytest.raises(ValueError, match="unexecuted"):
        apply(packet, [ruling])
