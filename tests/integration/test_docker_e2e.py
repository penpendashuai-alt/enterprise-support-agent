import os

import pytest

from client import AgentClient


@pytest.mark.docker
def test_support_agent_container():
    client = AgentClient(os.getenv("AGENT_URL", "http://127.0.0.1:18080"), agent="support-agent")
    response = client.invoke("CI_KNOWLEDGE", user_id="ci-integration", model="fake")
    assert response.type == "ai"
    assert "MFA" in response.content and "[1]" in response.content


@pytest.mark.docker
def test_support_sse_container():
    client = AgentClient(os.getenv("AGENT_URL", "http://127.0.0.1:18080"), agent="support-agent")
    events = list(
        client.stream("CI_KNOWLEDGE", user_id="ci-integration", model="fake", stream_tokens=False)
    )
    assert any(getattr(event, "type", None) == "ai" for event in events)
