"""Explicit deterministic infrastructure fixture, never a model-quality evaluation."""

import asyncio
import json

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda

from core.llm import FakeToolModel


class CIModel(FakeToolModel):
    def __init__(self):
        super().__init__(["CI fixture"])

    @staticmethod
    def text(messages):
        return next(str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage))

    def with_structured_output(self, schema, **kwargs):
        async def extract(messages):
            text = self.text(messages)
            if text == "CI_TIMEOUT":
                await asyncio.sleep(180)
            if text == "CI_ERROR":
                raise RuntimeError("CI_SYNTHETIC_SENSITIVE_ERROR")
            if schema.__name__ == "DraftExtraction":
                return json.loads(text.removeprefix("CI_DRAFT:"))
            draft = text.startswith("CI_DRAFT:")
            status = text == "CI_STATUS"
            return {
                "intent": "ticket_request"
                if draft
                else "service_status"
                if status
                else "general_question",
                "entities": {
                    "service_name": "VPN" if status else None,
                    "ticket_id": None,
                    "ticket_action": "create" if draft else None,
                    "issue_description": "演示故障" if draft else None,
                    "device_id": None,
                },
                "needs_clarification": False,
                "clarification_question": None,
                "knowledge_required": not (draft or status),
                "retrieval_query": text if not (draft or status) else None,
            }

        return RunnableLambda(extract)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        text = self.text(messages)
        if text == "CI_STATUS" and not isinstance(messages[-1], ToolMessage):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_service_status",
                        "args": {"service_name": "VPN"},
                        "id": "ci-status",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            message = AIMessage(
                content="根据模拟数据，服务查询已完成。"
                if text == "CI_STATUS"
                else "演示制度要求启用 MFA [1]。"
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        result = await self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        assert isinstance(message, AIMessage)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=message.tool_calls)
        )
