import importlib
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from agents.support_agent import SupportEntities, builder, get_support_model

module = importlib.import_module("agents.support_agent")


@pytest.mark.asyncio
async def test_deepseek_non_thinking_router_and_tool_round_trip(monkeypatch):
    requests = []
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "route",
                    "type": "function",
                    "function": {
                        "name": "RouteDecision",
                        "arguments": json.dumps(
                            {
                                "intent": "service_status",
                                "entities": SupportEntities(service_name="GitHub").model_dump(),
                                "needs_clarification": False,
                                "knowledge_required": False,
                                "retrieval_query": None,
                                "clarification_question": None,
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "status",
                    "type": "function",
                    "function": {
                        "name": "query_service_status",
                        "arguments": '{"service_name":"GitHub"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "模拟 GitHub 正常"},
    ]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        message = responses.pop(0)
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        original = ChatOpenAI(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            http_async_client=client,
            extra_body={"thinking": {"type": "enabled"}},
        )
        monkeypatch.setattr(module, "get_model", lambda _: original)
        result = await builder.compile().ainvoke(
            {"messages": [HumanMessage(content="GitHub 状态？")]}
        )
        assert result["messages"][-1].content == "模拟 GitHub 正常"
        assert len(requests) == 3
        assert requests[0]["tool_choice"]["function"]["name"] == "RouteDecision"
        assert requests[-1]["messages"][-1]["tool_call_id"] == "status"
        assert original.extra_body == {"thinking": {"type": "enabled"}}


@pytest.mark.parametrize(
    "base_url,model_name",
    [
        ("https://example.com/v1", "deepseek-v4-pro"),
        ("https://api.openai.com/v1", "gpt-5-mini"),
    ],
)
def test_other_providers_keep_existing_configuration(monkeypatch, base_url, model_name):
    original = ChatOpenAI(model=model_name, base_url=base_url, api_key="test-key")
    monkeypatch.setattr(module, "get_model", lambda _: original)
    assert get_support_model({}) is original
