import asyncio

import pytest

from client.client import AgentClient, AgentClientError, _http_session


def test_http_session_does_not_cross_event_loops():
    agent = AgentClient(get_info=False)
    sessions = []

    async def rerun():
        async with AgentClient.session() as http:
            sessions.append(http)
            async with agent._async_http() as one, agent._async_http() as two:
                assert one is two is http
        assert http.is_closed and _http_session.get() is None

    asyncio.run(rerun())
    asyncio.run(rerun())
    assert sessions[0] is not sessions[1]


def test_sync_client_reuses_pool_and_closes():
    agent = AgentClient(get_info=False)
    first = agent.http
    assert first is agent.http
    agent.close()
    assert first.is_closed
    agent.close()


def test_sse_error_is_failure_and_progress_is_not_answer():
    agent = AgentClient(get_info=False)
    with pytest.raises(AgentClientError):
        agent._parse_stream_line('data: {"type":"error","content":{"code":"request_timeout"}}')
    assert agent._parse_stream_line(": heartbeat") is None
    assert agent._parse_stream_line('data: {"type":"progress","content":"waiting"}') is None
