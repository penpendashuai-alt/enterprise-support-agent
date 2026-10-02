from unittest.mock import AsyncMock

import pytest
from langgraph.store.memory import InMemoryStore
from test_ticket_approval import approval, pending, send
from test_ticket_approval import environment as environment

from agents.support_tools import query_existing_ticket
from core import settings
from support_storage.preferences import (
    Preferences,
    preference_message,
    read_preferences,
    save_preferences,
)


@pytest.mark.asyncio
async def test_identity_is_required_and_ownership_precedes_checkpoint_read(
    environment, monkeypatch
):
    async with environment() as (client, graph, _):
        missing = await client.post(
            "/support-agent/invoke", json={"message": "hello", "thread_id": "unowned"}
        )
        assert missing.status_code == 422
        draft = (await send(client, text="建工单")).json()["custom_data"]
        original = graph.aget_state
        spy = AsyncMock(wraps=original)
        monkeypatch.setattr(graph, "aget_state", spy)
        for endpoint in ["invoke", "history"]:
            body = {"thread_id": "one", "user_id": "other"}
            if endpoint == "invoke":
                body["message"] = "read"
            assert (await client.post("/support-agent/" + endpoint, json=body)).status_code == 403
        assert (
            await client.get(
                "/support-agent/approval", params={"thread_id": "one", "user_id": "other"}
            )
        ).status_code == 403
        assert (
            await client.post("/chatbot/history", json={"thread_id": "one", "user_id": "test-user"})
        ).status_code == 403
        streamed = await client.post(
            "/support-agent/stream",
            json={"thread_id": "one", "user_id": "other", "message": "read"},
        )
        assert '"type": "error"' in streamed.text and '"type": "message"' not in streamed.text
        agui_body = {
            "threadId": "one",
            "runId": "probe",
            "messages": [],
            "tools": [],
            "context": [],
            "state": {},
            "forwardedProps": {"configurable": {"user_id": "test-user"}},
        }
        assert (await client.post("/agui/chatbot/run", json=agui_body)).status_code == 403
        assert (await client.post("/agui/support-agent/run", json=agui_body)).status_code == 422
        spy.assert_not_awaited()
        assert (await send(client, decision=approval(draft))).status_code == 200
        own = (await client.get("/support-agent/tickets", params={"user_id": "test-user"})).json()[
            "tickets"
        ]
        assert len(own) == 1 and own[0]["user_id"] == "test-user"
        assert (await client.get("/support-agent/tickets", params={"user_id": "other"})).json()[
            "tickets"
        ] == []
        result = await query_existing_ticket.ainvoke(
            {"ticket_id": own[0]["ticket_id"]}, {"configurable": {"user_id": "other"}}
        )
        assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_preferences_explicit_namespace_overwrite_delete_and_failures(environment):
    async with environment() as (client, graph, _):
        graph.store = InMemoryStore()
        url = "/support-agent/preferences"
        params = {"user_id": "test-user"}
        assert (
            await client.put(
                url, params=params, json={"language": "zh", "detail": "concise", "approved": True}
            )
        ).status_code == 422
        assert (
            await client.put(url, params=params, json={"language": "zh", "detail": "concise"})
        ).json()["status"] == "saved"
        assert (await client.get(url, params={"user_id": "other"})).json()["status"] == "default"
        await send(client, text="建工单", thread="first")
        await send(client, text="建工单", thread="second")
        assert (await pending(client, "first"))["draft_id"] != (await pending(client, "second"))[
            "draft_id"
        ]
        threads = (await client.get("/support-agent/threads", params=params)).json()["threads"]
        assert {r["thread_id"] for r in threads} == {"first", "second"}
        assert (await client.get(url, params=params)).json()["preferences"] == {
            "language": "zh",
            "detail": "concise",
        }
        assert (
            await client.put(url, params=params, json={"language": "en", "detail": "detailed"})
        ).status_code == 200
        message = await preference_message(graph.store, "test-user")
        assert "英文" in message[0].content and "本轮" in message[0].content
        assert (await client.delete(url, params=params)).json()["status"] == "deleted"
        assert await preference_message(graph.store, "test-user") == []
        assert await pending(client, "first") is not None
        graph.store = AsyncMock()
        graph.store.aget.side_effect = OSError("offline")
        graph.store.aput.side_effect = OSError("offline")
        graph.store.adelete.side_effect = OSError("offline")
        assert (await client.get(url, params=params)).json()["status"] == "unavailable"
        assert (
            await client.put(url, params=params, json={"language": "zh", "detail": "concise"})
        ).status_code == 503
        assert (await client.delete(url, params=params)).status_code == 503


@pytest.mark.asyncio
async def test_closed_demo_sample_path_and_model_cannot_supply_user(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_DEMO_SAMPLES", False)
    result = await query_existing_ticket.ainvoke(
        {"ticket_id": "INC-1001"}, {"configurable": {"user_id": "test-user"}}
    )
    assert result["status"] == "not_found"
    assert "user_id" not in query_existing_ticket.args_schema.model_fields
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await query_existing_ticket.ainvoke(
            {"ticket_id": "INC-1001", "user_id": "other"},
            {"configurable": {"user_id": "test-user"}},
        )


@pytest.mark.asyncio
async def test_preference_schema_and_corrupt_store_are_not_instructions():
    store = InMemoryStore()
    await save_preferences(store, "A", Preferences(language="en", detail="concise"))
    await store.aput(
        ("support", "B", "preferences"),
        "explicit",
        {"schema_version": 1, "source": "model_guess", "preferences": {"language": "en"}},
    )
    assert (await read_preferences(store, "A"))["status"] == "saved"
    assert (await read_preferences(store, "B"))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_graph_reads_preferences_at_execution_time(environment, monkeypatch):
    import importlib

    from langchain_core.messages import AIMessage

    from agents.support_agent import SupportEntities

    module = importlib.import_module("agents.support_agent")
    captured = []

    class GeneralModel:
        def with_structured_output(self, schema):
            return AsyncMock(
                ainvoke=AsyncMock(
                    return_value={
                        "intent": "general_question",
                        "entities": SupportEntities().model_dump(),
                        "needs_clarification": False,
                        "knowledge_required": False,
                        "retrieval_query": None,
                        "clarification_question": None,
                    }
                )
            )

        async def ainvoke(self, messages, config):
            captured.append(messages)
            return AIMessage(content="General IT explanation")

    async with environment() as (client, graph, _):
        graph.store = InMemoryStore()
        monkeypatch.setattr(module, "get_support_model", lambda _: GeneralModel())
        await save_preferences(graph.store, "test-user", Preferences(language="en"))
        await send(client, text="DNS 是什么？")
        assert any("英文" in str(m.content) for m in captured[-1])
        from support_storage.preferences import delete_preferences

        await delete_preferences(graph.store, "test-user")
        await send(client, text="DHCP 是什么？")
        assert not any("用户明确保存的表达偏好" in str(m.content) for m in captured[-1])
