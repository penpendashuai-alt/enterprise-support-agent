import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from agents.support_agent import SupportEntities, handle_request
from agents.support_tools import query_existing_ticket
from core import settings
from tickets.models import TicketDraft
from tickets.repository import TicketRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [True, False])
async def test_local_ticket_reply_uses_database_facts(monkeypatch, tmp_path, exists):
    monkeypatch.setattr(settings, "TICKET_DB_PATH", str(tmp_path / "tickets.db"))
    repo = TicketRepository(settings.TICKET_DB_PATH)
    ticket_id = "DEMO-" + "0" * 32
    if exists:
        record = repo.create(
            TicketDraft(title="VPN", description="809", impact="本人", priority="P1"),
            "draft-" + "a" * 32,
            1,
            "thread",
            "test-user",
        )
        ticket_id = record.ticket_id
    result = await query_existing_ticket.ainvoke(
        {"ticket_id": ticket_id}, {"configurable": {"user_id": "test-user"}}
    )
    model = Mock(side_effect=AssertionError("A stored ticket result needs no model rewriting"))
    monkeypatch.setattr("agents.support_agent.get_support_model", model)
    response = await handle_request(
        {
            "intent": "ticket_request",
            "entities": SupportEntities(ticket_action="query", ticket_id=ticket_id),
            "messages": [
                HumanMessage(content="查工单"),
                ToolMessage(
                    name="query_existing_ticket",
                    tool_call_id="query",
                    content=json.dumps(result, ensure_ascii=False),
                ),
            ],
        },
        {},
    )
    content = response["messages"][0].content
    assert "演示工单" in content
    if exists:
        assert all(
            value in content for value in [ticket_id, "P1", "809", "open", "未提交到真实企业系统"]
        )
    else:
        assert "未找到" in content
    model.assert_not_called()
