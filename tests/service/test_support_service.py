import importlib
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents import get_all_agent_info
from agents.support_agent import SupportEntities, builder
from service import app


class StreamingScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self._generate(messages).generations[0].message
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=message.tool_calls)
        )


def router_message(service=None):
    return AIMessage(
        content="INTERNAL_ROUTER_JSON",
        tool_calls=[
            {
                "name": "RouteDecision",
                "id": "router",
                "args": {
                    "intent": "service_status",
                    "entities": SupportEntities(service_name=service).model_dump(),
                    "needs_clarification": False,
                    "knowledge_required": False,
                    "retrieval_query": None,
                    "clarification_question": None,
                },
            }
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_support_endpoints_and_router_privacy(monkeypatch, tmp_path, endpoint):
    model = StreamingScriptedModel(
        responses=[
            router_message(),
            router_message("GitHub"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_service_status",
                        "args": {"service_name": "GitHub"},
                        "id": "status",
                    }
                ],
            ),
            AIMessage(content="根据模拟数据，GitHub 运行正常。"),
            router_message(),
        ]
    )
    monkeypatch.setattr(
        importlib.import_module("agents.support_agent"), "get_model", lambda _: model
    )
    service = importlib.import_module("service.service")
    monkeypatch.setattr(service.settings, "AUTH_SECRET", None)
    monkeypatch.setattr(service.settings, "LANGFUSE_TRACING", False)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "support.db")) as saver:
        graph = builder.compile(checkpointer=saver)
        original = service.get_agent
        monkeypatch.setattr(
            service, "get_agent", lambda name: graph if name == "support-agent" else original(name)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            info = (await client.get("/info")).json()
            assert "support-agent" in [a["key"] for a in info["agents"]]
            for text in ["服务挂了吗？", "GitHub"]:
                response = await client.post(
                    f"/support-agent/{endpoint}", json={"message": text, "thread_id": "thread"}
                )
                assert response.status_code == 200
                assert "INTERNAL_ROUTER_JSON" not in response.text
                assert "RouteDecision" not in response.text
            if endpoint == "invoke":
                assert response.json()["content"] == "根据模拟数据，GitHub 运行正常。"
            else:
                assert response.text.endswith("data: [DONE]\n\n")
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                assert not any(e["type"] == "error" for e in events)
                assert any(e["type"] == "token" and "模拟数据" in e["content"] for e in events)
                messages = [e["content"] for e in events if e["type"] == "message"]
                assert any(m["type"] == "tool" and m["tool_call_id"] == "status" for m in messages)
                assert messages[-1]["content"] == "根据模拟数据，GitHub 运行正常。"
            fresh = await client.post(
                "/support-agent/invoke", json={"message": "服务挂了吗？", "thread_id": "fresh"}
            )
            assert "哪个服务" in fresh.json()["content"]
        state = await graph.aget_state({"configurable": {"thread_id": "thread"}})
        assert state.values["entities"].service_name == "GitHub"


def test_existing_agents_remain_registered():
    keys = {agent.key for agent in get_all_agent_info()}
    assert {"support-agent", "research-assistant", "chatbot", "rag-assistant"} <= keys
