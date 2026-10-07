"""Explicit deterministic infrastructure fixture, never a model-quality evaluation."""

import asyncio
import json
import re

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda

from core.llm import FakeToolModel


class CIModel(FakeToolModel):
    def __init__(self):
        super().__init__(["CI fixture"])

    @staticmethod
    def text(messages):
        text = next(str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage))
        aliases = {
            "查询服务状态": "CI_CLARIFY",
            "VPN": "CI_STATUS",
            "VPN 服务现在正常吗？": "CI_STATUS",
            "演示一次模型故障": "CI_ERROR",
        }
        if text == "创建演示工单：VPN 连接失败，影响本人，尚未重启，优先级 P3。":
            return "CI_DRAFT:" + json.dumps(
                {
                    "title": "VPN 连接失败",
                    "description": "VPN 连接失败，尚未重启。",
                    "service_name": "VPN",
                    "impact": "本人",
                    "priority": "P3",
                },
                ensure_ascii=False,
            )
        return aliases.get(text, text)

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
            ticket = re.fullmatch(r"查询工单 (DEMO-[0-9A-Fa-f]{32})", text)
            clarify = text == "CI_CLARIFY"
            return {
                "intent": "ticket_request"
                if draft or ticket
                else "service_status"
                if status
                else "general_question",
                "entities": {
                    "service_name": "VPN" if status else None,
                    "ticket_id": ticket[1] if ticket else None,
                    "ticket_action": "create" if draft else "query" if ticket else None,
                    "issue_description": "演示故障" if draft else None,
                    "device_id": None,
                },
                "needs_clarification": clarify,
                "clarification_question": "请提供需要查询的服务名，例如 VPN。" if clarify else None,
                "knowledge_required": not (draft or status or ticket or clarify),
                "retrieval_query": text if not (draft or status or ticket or clarify) else None,
            }

        return RunnableLambda(extract)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        text = self.text(messages)
        ticket = re.fullmatch(r"查询工单 (DEMO-[0-9A-Fa-f]{32})", text)
        if (text == "CI_STATUS" or ticket) and not isinstance(messages[-1], ToolMessage):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_existing_ticket" if ticket else "query_service_status",
                        "args": {"ticket_id": ticket[1]} if ticket else {"service_name": "VPN"},
                        "id": "ci-status",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            message = AIMessage(
                content="演示业务数据库查询结果：" + str(messages[-1].content)
                if ticket and isinstance(messages[-1], ToolMessage)
                else "根据模拟数据，VPN 服务状态为 operational（正常），不代表实时监控。"
                if text == "CI_STATUS"
                else "演示制度要求启用 MFA [1]。"
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        result = await self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        assert isinstance(message, AIMessage)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", tool_calls=message.tool_calls)
            )
        else:
            for offset in range(0, len(message.content), 6):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=message.content[offset : offset + 6])
                )
                await asyncio.sleep(0.03)
