import os
from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean for each test."""
    with patch.dict(os.environ, {}, clear=True):
        yield


@pytest.fixture(autouse=True)
def isolate_support_storage(monkeypatch, tmp_path):
    from core import settings
    from core.settings import DatabaseType

    monkeypatch.setattr(settings, "DATABASE_TYPE", DatabaseType.SQLITE)
    monkeypatch.setattr(settings, "TICKET_DB_PATH", str(tmp_path / "support-business.db"))
    monkeypatch.setattr(settings, "SUPPORT_DEMO_SAMPLES", True)
    monkeypatch.setattr(settings, "RAG_CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "ADMISSION_ENABLED", False)


@pytest.fixture(autouse=True)
def isolate_rag_network(monkeypatch):
    from unittest.mock import AsyncMock

    from rag.models import RetrievalResult

    monkeypatch.setattr(
        "rag.retriever.retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                status="unavailable", query="test", error_code="test_network_disabled"
            )
        ),
    )
