import json

from fastapi.testclient import TestClient

from core import settings
from schema.models import FakeModelName
from service import app


def test_explicit_ci_model_routes_tools_and_drafts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CI_TEST_MODE", True)
    monkeypatch.setattr(settings, "USE_FAKE_MODEL", True)
    monkeypatch.setattr(settings, "DEFAULT_MODEL", FakeModelName.FAKE)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "checkpoint.db"))
    draft = {
        "title": "VPN故障",
        "description": "希望人工检查，尚未重启",
        "service_name": "VPN",
        "impact": "本人",
        "priority": "P3",
    }
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        status = client.post(
            "/support-agent/invoke",
            json={"user_id": "ci", "thread_id": "status", "message": "CI_STATUS"},
        )
        assert status.status_code == 200
        assert "模拟" in status.json()["content"]
        streamed = client.post(
            "/support-agent/stream",
            json={"user_id": "ci", "thread_id": "stream", "message": "CI_STATUS"},
        )
        assert streamed.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in streamed.text.splitlines()
            if line.startswith("data: {")
        ]
        assert "模拟" in json.dumps(events, ensure_ascii=False) and "[DONE]" in streamed.text
        assert "query_service_status" in streamed.text
        degraded = client.post(
            "/support-agent/invoke",
            json={"user_id": "ci", "thread_id": "error", "message": "CI_ERROR"},
        )
        assert degraded.status_code == 200
        assert "暂时无法可靠识别请求" in degraded.json()["content"]
        response = client.post(
            "/support-agent/invoke",
            json={
                "user_id": "ci",
                "thread_id": "draft",
                "message": "CI_DRAFT:" + json.dumps(draft),
            },
        )
        assert response.status_code == 200
        assert response.json()["custom_data"]["draft"] == draft
