import json
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

import httpx
import pytest

from client import AgentClient, AgentClientError
from schema import ChatMessage, UserInput
from tickets.models import ApprovalInput


@pytest.mark.asyncio
async def test_sync_async_approval_and_pending_contract():
    client = AgentClient(base_url="http://test", get_info=False)
    client.update_agent("support-agent", verify=False)
    approval = ApprovalInput(draft_id="draft-" + "a" * 32, draft_version=2, action="approve")
    response = httpx.Response(
        200,
        json=ChatMessage(
            type="ai", content="created", custom_data={"kind": "ticket_result"}
        ).model_dump(),
        request=httpx.Request("POST", "http://test"),
    )
    for method in ["invoke", "ainvoke"]:
        with patch(
            "httpx.Client.post" if method == "invoke" else "httpx.AsyncClient.post",
            return_value=response,
        ) as post:
            result = (
                client.invoke(approval=approval, thread_id="one")
                if method == "invoke"
                else await client.ainvoke(approval=approval, thread_id="one")
            )
            body = post.call_args.kwargs["json"]
            assert body["message"] is None
            assert UserInput.model_validate(body).approval == approval
            assert result.custom_data == {"kind": "ticket_result"}
    response = httpx.Response(
        200,
        json={"pending": {"draft_id": approval.draft_id}},
        request=httpx.Request("GET", "http://test"),
    )
    with (
        patch("httpx.Client.get", return_value=response),
        patch("httpx.AsyncClient.get", return_value=response),
    ):
        assert (
            client.get_pending_approval("one")
            == await client.aget_pending_approval("one")
            == {"draft_id": approval.draft_id}
        )
    with pytest.raises(AgentClientError, match="stale_version"):
        client._parse_stream_line(
            'data: {"type":"error","content":{"code":"stale_version","message":"expired"}}'
        )


@pytest.mark.asyncio
async def test_stream_clients_send_structured_approval():
    client = AgentClient(base_url="http://test", get_info=False)
    client.update_agent("support-agent", verify=False)
    approval = ApprovalInput(draft_id="draft-" + "a" * 32, draft_version=1, action="cancel")
    event = {
        "type": "message",
        "content": {"type": "ai", "content": "cancelled", "custom_data": {"kind": "ticket_result"}},
    }
    response = httpx.Response(
        200,
        text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        request=httpx.Request("POST", "http://test"),
    )
    bodies = []

    @contextmanager
    def stream(*args, **kwargs):
        bodies.append(kwargs["json"])
        yield response

    @asynccontextmanager
    async def astream(*args, **kwargs):
        bodies.append(kwargs["json"])
        yield response

    with patch("httpx.Client.stream", stream), patch("httpx.AsyncClient.stream", astream):
        sync = list(client.stream(approval=approval, thread_id="one"))
        asynchronous = [item async for item in client.astream(approval=approval, thread_id="one")]
    assert sync == asynchronous
    assert sync[0].custom_data == {"kind": "ticket_result"}
    assert len(bodies) == 2
    assert all(UserInput.model_validate(body).approval == approval for body in bodies)
