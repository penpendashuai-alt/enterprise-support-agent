import importlib
import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from agents.support_agent import SupportEntities, builder, execute_tools, handle_request

module = importlib.import_module("agents.support_agent")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def route(intent="general_question", **kwargs):
    kwargs["entities"] = SupportEntities(**kwargs.get("entities", {})).model_dump()
    kwargs.setdefault("knowledge_required", intent == "troubleshooting")
    kwargs.setdefault("retrieval_query", "VPN 故障排查" if intent == "troubleshooting" else None)
    kwargs.setdefault("needs_clarification", False)
    kwargs.setdefault("clarification_question", None)
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RouteDecision",
                "id": "route",
                "args": {"intent": intent, **kwargs},
            }
        ],
    )


def call(name="query_service_status", args=None, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args if args is not None else {"service_name": "GitHub"},
                "id": call_id,
            }
        ],
    )


def setup_graph(monkeypatch, responses):
    from rag.models import Evidence, RetrievalResult

    evidence = Evidence(
        doc_id="test",
        chunk_id="test:1",
        title="演示排障",
        source_type="synthetic",
        source_path="test.md",
        url=None,
        document_version="1",
        location="行 1",
        text="演示指南：检查网络与客户端配置。",
        content_hash="test",
        number=1,
        score=0.9,
        collection="test",
        index_version="test",
    )
    monkeypatch.setattr(
        "rag.retriever.retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                status="ok", query="VPN 故障排查", evidence=[evidence], candidates=[evidence]
            )
        ),
    )
    model = ScriptedModel(responses=responses)
    monkeypatch.setattr(module, "get_model", lambda _: model)
    return builder.compile(checkpointer=MemorySaver())


async def ask(graph, text, thread="one", **config):
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=text)]},
        {"configurable": {"thread_id": thread, "user_id": "test-user"}, **config},
    )


def assert_paired(messages):
    pending = set()
    for message in messages:
        if isinstance(message, AIMessage):
            assert not pending
            pending.update(c["id"] for c in message.tool_calls)
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
        elif isinstance(message, HumanMessage):
            assert not pending
    assert not pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent,entities,tool,args",
    [
        (
            "service_status",
            {"service_name": "GitHub"},
            "query_service_status",
            {"service_name": "GitHub"},
        ),
        (
            "troubleshooting",
            {"issue_description": "VPN 809"},
            "query_service_status",
            {"service_name": "VPN"},
        ),
        (
            "troubleshooting",
            {"device_id": "DEV-001"},
            "get_device_information",
            {"device_id": "DEV-001"},
        ),
        (
            "ticket_request",
            {"ticket_action": "query", "ticket_id": "INC-1001"},
            "query_existing_ticket",
            {"ticket_id": "INC-1001"},
        ),
    ],
)
async def test_tool_loop(monkeypatch, intent, entities, tool, args):
    graph = setup_graph(
        monkeypatch,
        [
            route(intent, entities=entities),
            call(tool, args),
            AIMessage(content="根据模拟数据和演示文档给出建议 [1]"),
        ],
    )
    result = await ask(graph, "请查询")
    assert result["intent"] == intent
    assert json.loads(result["messages"][-2].content)["status"] == "success"
    assert_paired(result["messages"])
    assert len(result["messages"]) == 4


@pytest.mark.asyncio
async def test_clarification_followup_and_topic_switch(monkeypatch):
    graph = setup_graph(
        monkeypatch,
        [
            route("service_status"),
            route("service_status", entities={"service_name": "GitHub"}),
            call(),
            AIMessage(content="模拟 GitHub 正常"),
            route("service_status", entities={"service_name": "邮箱"}),
            call(args={"service_name": "邮箱"}),
            AIMessage(content="模拟邮箱延迟"),
            route(),
            AIMessage(content="VPN 是虚拟专用网络"),
        ],
    )
    first = await ask(graph, "服务挂了吗？")
    assert first["needs_clarification"]
    second = await ask(graph, "GitHub")
    assert second["entities"].service_name == "GitHub"
    assert not second["needs_clarification"]
    assert second["clarification_question"] is None
    third = await ask(graph, "邮箱呢？")
    assert third["entities"].service_name == "邮箱"
    fourth = await ask(graph, "VPN 是什么？")
    assert fourth["entities"] == SupportEntities()
    assert fourth["intent"] == "general_question"
    assert_paired(fourth["messages"])


@pytest.mark.asyncio
async def test_ticket_to_general_and_new_thread(monkeypatch):
    graph = setup_graph(
        monkeypatch,
        [
            route("ticket_request", entities={"ticket_action": "query", "ticket_id": "INC-1001"}),
            call("query_existing_ticket", {"ticket_id": "INC-1001"}),
            AIMessage(content="模拟处理中"),
            route(entities={"ticket_id": "INC-1001", "ticket_action": "query"}),
            AIMessage(content="知识回答"),
            route("service_status"),
        ],
    )
    await ask(graph, "查 INC-1001")
    result = await ask(graph, "VPN 是什么？")
    assert result["entities"] == SupportEntities()
    fresh = await ask(graph, "服务挂了吗？", thread="two")
    assert len(fresh["messages"]) == 2
    assert fresh["entities"] == SupportEntities()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entities",
    [{"ticket_action": "create"}, {"ticket_action": "create", "issue_description": "VPN 连不上"}],
)
async def test_create_never_submits(monkeypatch, entities):
    graph = setup_graph(monkeypatch, [route("ticket_request", entities=entities)])
    result = await ask(graph, "帮我提工单")
    assert "尚未提交" in result["messages"][-1].content
    assert not any(isinstance(m, ToolMessage) for m in result["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        AIMessage(content="not JSON"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "RouteDecision", "id": "x", "args": {"intent": "delete_everything"}}
            ],
        ),
    ],
)
async def test_bad_router_clears_state(monkeypatch, bad):
    graph = setup_graph(monkeypatch, [bad])
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="? ")],
            "entities": SupportEntities(service_name="GitHub"),
        },
        {"configurable": {"thread_id": "one"}},
    )
    assert result["intent"] is None
    assert result["needs_clarification"]
    assert result["entities"] == SupportEntities()
    assert len(result["messages"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("create_ticket", {}),
        ("query_existing_ticket", {"ticket_id": "INC-1001"}),
        ("query_service_status", {"service_name": ""}),
        ("query_service_status", {"service_name": "GitHub", "extra": True}),
    ],
)
async def test_disallowed_or_invalid_tools(monkeypatch, name, args):
    graph = setup_graph(
        monkeypatch,
        [
            route("service_status", entities={"service_name": "GitHub"}),
            call(name, args),
            AIMessage(content="查询失败"),
        ],
    )
    result = await ask(graph, "查询 GitHub")
    tool_message = result["messages"][-2]
    assert tool_message.status == "error"
    assert json.loads(tool_message.content)["status"] == "error"
    assert_paired(result["messages"])


@pytest.mark.asyncio
async def test_execution_failure_is_paired(monkeypatch):
    monkeypatch.setitem(
        module.SUPPORT_TOOLS,
        "query_service_status",
        Mock(ainvoke=AsyncMock(side_effect=RuntimeError("private detail"))),
    )
    result = await execute_tools(
        {
            "intent": "service_status",
            "entities": SupportEntities(service_name="GitHub"),
            "messages": [call()],
        },
        {},
    )
    assert result["messages"][0].tool_call_id == "call-1"
    assert "private detail" not in result["messages"][0].content
    assert result["messages"][0].status == "error"


@pytest.mark.asyncio
async def test_repeated_tools_stop_and_next_turn_works(monkeypatch):
    responses = [
        route("service_status", entities={"service_name": "GitHub"}),
        *[call(call_id=f"c{i}") for i in range(20)],
    ]
    graph = setup_graph(monkeypatch, responses)
    result = await ask(graph, "查询 GitHub", recursion_limit=10)
    assert "预算" in result["messages"][-1].content or "上限" in result["messages"][-1].content
    assert_paired(result["messages"])
    model = ScriptedModel(responses=[route(), AIMessage(content="VPN 是虚拟专用网络")])
    monkeypatch.setattr(module, "get_model", lambda _: model)
    result = await ask(graph, "VPN 是什么？")
    assert result["messages"][-1].content == "VPN 是虚拟专用网络"
    assert_paired(result["messages"])


@pytest.mark.asyncio
async def test_handler_exception_after_tools(monkeypatch):
    model = Mock(ainvoke=AsyncMock(side_effect=TimeoutError))
    model.bind_tools.return_value = model
    monkeypatch.setattr(module, "get_model", lambda _: model)
    state = {
        "intent": "service_status",
        "entities": SupportEntities(service_name="GitHub"),
        "remaining_steps": 10,
        "messages": [
            HumanMessage(content="查 GitHub"),
            call(),
            ToolMessage(content="{}", tool_call_id="call-1"),
        ],
    }
    from execution.telemetry import ControlError

    with pytest.raises(ControlError, match="model_timeout"):
        await handle_request(state, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent,action", [("general_question", None), ("ticket_request", "create")]
)
async def test_executor_enforces_no_tools(monkeypatch, intent, action):
    spy = Mock(ainvoke=AsyncMock())
    monkeypatch.setitem(module.SUPPORT_TOOLS, "query_service_status", spy)
    result = await execute_tools(
        {
            "intent": intent,
            "entities": SupportEntities(ticket_action=action),
            "messages": [call()],
        },
        {},
    )
    spy.ainvoke.assert_not_called()
    assert result["messages"][0].status == "error"


@pytest.mark.asyncio
async def test_too_many_calls_are_all_paired_without_execution(monkeypatch):
    spy = Mock(ainvoke=AsyncMock())
    monkeypatch.setitem(module.SUPPORT_TOOLS, "query_service_status", spy)
    message = AIMessage(
        content="", tool_calls=[call(call_id=str(i)).tool_calls[0] for i in range(5)]
    )
    result = await execute_tools(
        {
            "intent": "service_status",
            "entities": SupportEntities(service_name="GitHub"),
            "messages": [message],
        },
        {},
    )
    spy.ainvoke.assert_not_called()
    assert_paired([message, *result["messages"]])
    assert all(m.status == "error" for m in result["messages"])


@pytest.mark.asyncio
async def test_router_missing_entities_is_not_silently_accepted(monkeypatch):
    bad = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RouteDecision",
                "id": "route",
                "args": {"intent": "service_status", "needs_clarification": False},
            }
        ],
    )
    graph = setup_graph(monkeypatch, [bad])
    result = await ask(graph, "查询 GitHub")
    assert result["intent"] is None
    assert "无法可靠识别" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_handler_receives_current_turn_tools_only(monkeypatch):
    model = Mock(ainvoke=AsyncMock(return_value=AIMessage(content="模拟邮箱延迟")))
    model.bind_tools.return_value = model
    monkeypatch.setattr(module, "get_model", lambda _: model)
    await handle_request(
        {
            "intent": "service_status",
            "entities": SupportEntities(service_name="邮箱"),
            "remaining_steps": 10,
            "messages": [
                HumanMessage(content="查 GitHub"),
                call(),
                ToolMessage(content="old-result", tool_call_id="call-1"),
                AIMessage(content="GitHub 正常"),
                HumanMessage(content="邮箱呢？"),
            ],
        },
        {},
    )
    prompt = model.ainvoke.call_args.args[0]
    assert len(prompt) == 3
    assert prompt[-1].content == "邮箱呢？"
    assert not any(isinstance(m, ToolMessage) for m in prompt)
