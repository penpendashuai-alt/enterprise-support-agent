from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import settings
from service import health


def test_liveness_never_probes_dependencies(monkeypatch):
    monkeypatch.setattr(health, "repository", lambda: pytest.fail("liveness accessed storage"))
    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 503


@pytest.mark.asyncio
async def test_storage_failure_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        health,
        "repository",
        lambda: SimpleNamespace(tickets=AsyncMock(side_effect=RuntimeError("sensitive"))),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(support_ready=True)))
    result = await health.core(request)
    assert result == {"status": "unavailable", "reason": "storage_or_schema_unavailable"}


@pytest.mark.asyncio
async def test_redis_failure_keeps_core_and_approval(monkeypatch):
    monkeypatch.setattr(settings, "ADMISSION_ENABLED", True)
    monkeypatch.setattr(
        health, "core", AsyncMock(return_value={"status": "ok", "reason": "core_ready"})
    )
    monkeypatch.setattr(health, "client", lambda: None)
    monkeypatch.setattr(
        "rag.config.get_settings",
        lambda: SimpleNamespace(require_mode=lambda: (_ for _ in ()).throw(ValueError())),
    )
    result = await health.diagnose(None)
    assert result["core"]["status"] == "ok"
    assert result["question"]["status"] == "unavailable"
    assert result["approval_and_read"]["status"] == "degraded"
    assert result["retrieval"]["reason"] == "rag_not_configured"


def test_default_agent_scope_and_explicit_ci_contract(monkeypatch):
    from agents.agents import get_agent, get_all_agent_info
    from rag.config import RAGSettings

    monkeypatch.setattr(settings, "ENABLED_AGENTS", ["support-agent"])
    assert [a.key for a in get_all_agent_info()] == ["support-agent"]
    with pytest.raises(KeyError):
        get_agent("chatbot")
    ci = RAGSettings(
        CI_TEST_MODE=True,
        QDRANT_LOCAL=True,
        QDRANT_URL="http://qdrant:6333",
        QDRANT_COLLECTION="enterprise_support_dense_ci_test",
    )
    ci.require_dense()
    assert ci.index_contract()["provider"] == "deterministic-ci"
    with pytest.raises(ValueError):
        ci.model_copy(update={"QDRANT_COLLECTION": "enterprise_support_dense_v2"}).require_dense()
    with pytest.raises(ValueError):
        ci.model_copy(update={"QDRANT_URL": "https://external.example"}).require_dense()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "used,excluded,blocked", [(110, 0, True), (110, 20, False), (90, 0, False)]
)
async def test_redis_memory_pressure_diagnostic(monkeypatch, used, excluded, blocked):
    monkeypatch.setattr(settings, "ADMISSION_ENABLED", True)
    monkeypatch.setattr(health, "core", AsyncMock(return_value={"status": "ok"}))
    connection = SimpleNamespace(
        ping=AsyncMock(),
        info=AsyncMock(
            return_value={
                "maxmemory": 100,
                "used_memory": used,
                "mem_not_counted_for_evict": excluded,
                "maxmemory_policy": "noeviction",
            }
        ),
    )
    monkeypatch.setattr(health, "client", lambda: connection)
    monkeypatch.setattr(
        "rag.config.get_settings",
        lambda: SimpleNamespace(require_mode=lambda: (_ for _ in ()).throw(ValueError())),
    )
    result = await health.diagnose(None)
    assert result["core"]["status"] == "ok"
    assert (result["question"]["status"] == "unavailable") is blocked
    assert (result["approval_and_read"]["status"] == "degraded") is blocked
    assert result["cache"]["reason"] == ("redis_memory_limit" if blocked else "reachable")
