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
    monkeypatch.setattr(settings, "TICKET_DB_PATH", str(tmp_path / "tickets.db"))
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


def test_browser_demo_clarification_and_created_ticket_query(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CI_TEST_MODE", True)
    monkeypatch.setattr(settings, "USE_FAKE_MODEL", True)
    monkeypatch.setattr(settings, "DEFAULT_MODEL", FakeModelName.FAKE)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "checkpoint.db"))
    monkeypatch.setattr(settings, "TICKET_DB_PATH", str(tmp_path / "tickets.db"))
    with TestClient(app) as client:

        def send(**body):
            response = client.post(
                "/support-agent/invoke",
                json={"user_id": "demo", "thread_id": "demo", **body},
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert "请提供" in send(message="查询服务状态")["content"]
        assert "模拟" in send(message="VPN")["content"]
        draft = send(message="创建演示工单：VPN 连接失败，影响本人，尚未重启，优先级 P3。")[
            "custom_data"
        ]
        decision = {
            "draft_id": draft["draft_id"],
            "draft_version": draft["draft_version"],
            "action": "approve",
        }
        send(approval=decision)
        tickets = client.get("/support-agent/tickets", params={"user_id": "demo"}).json()["tickets"]
        assert len(tickets) == 1
        ticket_id = tickets[0]["ticket_id"]
        assert ticket_id == ticket_id.upper()
        result = send(message=f"查询工单 {ticket_id}")
        assert ticket_id in result["content"]
        assert "尚未重启" in result["content"]
        missing = send(message="查询工单 DEMO-" + "F" * 32)
        assert "未找到" in missing["content"]
