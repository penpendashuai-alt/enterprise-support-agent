"""Subprocess fixture only. Never use this deterministic model server for normal operation."""

import importlib
import os
from unittest.mock import AsyncMock

import uvicorn

from agents.support_agent import SupportEntities
from schema import AgentInfo
from support_storage.postgres import PostgresRepository

service = importlib.import_module("service.service")
support = importlib.import_module("agents.support_agent")
DRAFT = {
    "title": "VPN 故障",
    "description": "VPN 报错 809，已重启",
    "service_name": "VPN",
    "impact": "只有本人",
    "priority": "P2",
}


class DraftModel:
    def with_structured_output(self, schema):
        value = (
            {
                "intent": "ticket_request",
                "entities": SupportEntities(
                    ticket_action="create", issue_description="VPN 报错 809"
                ).model_dump(),
                "needs_clarification": False,
                "knowledge_required": False,
                "retrieval_query": None,
                "clarification_question": None,
            }
            if schema.__name__ == "RouteDecision"
            else DRAFT
        )
        return AsyncMock(ainvoke=AsyncMock(return_value=value))


if __name__ == "__main__":
    support.get_support_model = lambda _: DraftModel()
    service.get_all_agent_info = lambda: [
        AgentInfo(key="support-agent", description="test fixture")
    ]
    if os.environ.get("PHASE6_TEST_CRASH_AFTER_COMMIT") == "1":
        original_create = PostgresRepository.create

        async def crash_after_commit(self, *args, **kwargs):
            await original_create(self, *args, **kwargs)
            os._exit(73)

        PostgresRepository.create = crash_after_commit
    uvicorn.run(
        service.app,
        host="127.0.0.1",
        port=int(os.environ["PHASE6_TEST_PORT"]),
        loop="memory.postgres:selector_loop_factory",
        log_level="warning",
    )
