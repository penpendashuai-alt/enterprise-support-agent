import importlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.support_agent import SupportEntities, builder
from rag.models import Candidate, RetrievalResult
from service import app


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self._generate(messages).generations[0].message
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=message.tool_calls)
        )


def route(intent="general_question", knowledge=True):
    return AIMessage(
        content="INTERNAL_ROUTING",
        tool_calls=[
            {
                "name": "RouteDecision",
                "id": "route",
                "args": {
                    "intent": intent,
                    "entities": SupportEntities(
                        service_name="GitHub" if intent == "service_status" else None
                    ).model_dump(),
                    "needs_clarification": False,
                    "clarification_question": None,
                    "knowledge_required": knowledge,
                    "retrieval_query": "公司 VPN 使用规定" if knowledge else None,
                },
            }
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_rag_citations_history_no_token_leak_and_evidence_reset(
    monkeypatch, tmp_path, endpoint
):
    service = importlib.import_module("service.service")
    monkeypatch.setattr(service.settings, "AUTH_SECRET", None)
    model = Model(
        responses=[
            route(),
            AIMessage(
                content="未经验证的企业规定",
                tool_calls=[
                    {
                        "name": "create_ticket",
                        "args": {},
                        "id": "forbidden",
                    }
                ],
            ),
            AIMessage(content="演示制度要求开启 MFA [1]"),
            route(knowledge=False),
            AIMessage(content="VPN 是虚拟专用网络"),
        ]
    )
    monkeypatch.setattr("agents.support_agent.get_support_model", lambda _: model)
    evidence = Candidate(
        doc_id="vpn",
        chunk_id="vpn:1",
        title="VPN",
        source_type="synthetic",
        source_path="vpn.md",
        url=None,
        document_version="1",
        location="行 3-5",
        text="演示制度要求开启 MFA",
        content_hash="h",
        number=1,
        score=0.8,
        collection="dense",
        index_version="v1",
        snapshot_id="snapshot-v2",
        score_type="rerank",
        rerank_score=0.8,
        dense_score=0.7,
    )
    retrieve = AsyncMock(
        return_value=RetrievalResult(
            status="ok",
            query="公司 VPN 使用规定",
            evidence=[evidence],
            candidates=[evidence],
            index_version="v1",
            requested_mode="hybrid_rerank",
            actual_mode="hybrid_rerank",
            snapshot_id="snapshot-v2",
            backend_versions={"dense": "v1", "bm25": "lex-v1"},
        )
    )
    monkeypatch.setattr("rag.retriever.retrieve", retrieve)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as saver:
        graph = builder.compile(checkpointer=saver)
        monkeypatch.setattr(service, "get_agent", lambda _: graph)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/support-agent/{endpoint}",
                json={"thread_id": "rag", "message": "公司 VPN 要求？"},
            )
            assert response.status_code == 200
            assert "未经验证" not in response.text
            if endpoint == "stream":
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                assert not any(e["type"] in {"token", "error"} for e in events)
                answer = [e["content"] for e in events if e["type"] == "message"][-1]
            else:
                answer = response.json()
            assert answer["custom_data"]["citations"][0]["location"] == "行 3-5"
            assert answer["custom_data"]["actual_mode"] == "hybrid_rerank"
            assert answer["custom_data"]["snapshot_id"] == "snapshot-v2"
            assert answer["custom_data"]["citations"][0]["score_type"] == "rerank"
            assert answer["custom_data"]["citations"][0]["dense_score"] == 0.7
            history = (
                await client.post("/support-agent/history", json={"thread_id": "rag"})
            ).json()
            assert history["messages"][-1]["custom_data"] == answer["custom_data"]
            assert all("未经验证" not in m["content"] for m in history["messages"])
            await client.post(
                "/support-agent/invoke", json={"thread_id": "rag", "message": "VPN 是什么？"}
            )
            snapshot = await graph.aget_state({"configurable": {"thread_id": "rag"}})
            assert (
                snapshot.values["retrieval"] is None and not snapshot.values["knowledge_required"]
            )
            assert snapshot.values["retrieval_query"] is None
            retrieve.assert_awaited_once()


@pytest.mark.asyncio
async def test_rag_failure_prevents_unfounded_policy_reply(monkeypatch):
    from langchain_core.messages import HumanMessage

    from agents.support_agent import handle_request

    model = AsyncMock()
    monkeypatch.setattr("agents.support_agent.get_support_model", model)
    response = await handle_request(
        {
            "knowledge_required": True,
            "retrieval": RetrievalResult(
                status="unavailable", query="公司政策", error_code="embedding_auth"
            ).model_dump(),
            "messages": [HumanMessage(content="公司政策？")],
        },
        {},
    )
    assert "暂不可用" in response["messages"][0].content
    model.assert_not_called()
