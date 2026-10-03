import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from core import settings
from execution.telemetry import ControlError, current, measure
from schema import UserInput
from tickets.models import (
    TicketDraft,
    approval_payload,
    approval_summary,
    fingerprint,
    result_summary,
    ticket_result,
)
from tickets.repository import TicketConflict
from tickets.service import repository

_locks: dict[str, tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def execution_lock(agent_id: str, thread_id: str | None):
    if (trace := current.get()) and thread_id and trace.locked_thread == thread_id:
        yield
        return
    if not thread_id:
        yield
        return
    lock, count = _locks.get(thread_id, (asyncio.Lock(), 0))
    _locks[thread_id] = (lock, count + 1)
    try:
        acquired = False
        try:
            with measure("thread_wait"):
                try:
                    async with asyncio.timeout(
                        settings.THREAD_WAIT_TIMEOUT if settings.ADMISSION_ENABLED else None
                    ):
                        await lock.acquire()
                        acquired = True
                except TimeoutError:
                    raise ControlError("thread_busy") from None
            yield
        finally:
            if acquired:
                lock.release()
    finally:
        _, count = _locks[thread_id]
        if count == 1:
            del _locks[thread_id]
        else:
            _locks[thread_id] = (lock, count - 1)


def interrupt_message(value) -> AIMessage:
    if isinstance(value, dict) and value.get("kind") == "ticket_approval":
        return AIMessage(content=approval_summary(value), additional_kwargs={"custom_data": value})
    return AIMessage(content=value)


def pending_payload(snapshot) -> dict | None:
    values = snapshot.values
    if not isinstance(values, dict) or values.get("approval_status") not in {"pending", "approved"}:
        return None
    if not snapshot.next:
        return None
    return approval_payload(values)


def reject(code: str, message: str):
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


async def support_input(user_input: UserInput, snapshot, config: RunnableConfig) -> dict:
    pending = pending_payload(snapshot)
    approval = user_input.approval
    values = snapshot.values
    if approval is None:
        if pending:
            return {"support_response": interrupt_message(pending)}
        return {"input": {"messages": [HumanMessage(content=user_input.message or "")]}}

    try:
        existing = await repository().by_request(
            approval.draft_id,
            config["configurable"]["thread_id"],
            config["configurable"]["user_id"],
        )
    except TicketConflict:
        reject("request_conflict", "创建请求归属冲突。")
    except Exception:
        reject("storage_unavailable", "工单库暂不可用，无法核查该请求；请稍后重试。")
    if existing:
        if approval.draft_version != existing.draft_version:
            reject("stale_version", "草稿版本已过期。")
        if approval.action != "approve":
            reject("already_created", "该草稿已创建工单，不能修改或取消。")
        # Complete the checkpoint if the process stopped after committing the ticket.
        if pending and values.get("draft_id") == approval.draft_id:
            if existing.fingerprint != fingerprint(
                TicketDraft.model_validate(values["ticket_draft"])
            ):
                reject("content_conflict", "已保存工单与当前草稿内容不一致。")
            if values.get("approval_status") == "approved" and "create" in snapshot.next:
                return {"input": None}
            return {"input": Command(resume=approval.model_dump())}
        result = ticket_result(existing)
        return {
            "support_response": AIMessage(
                content=result_summary(result), additional_kwargs={"custom_data": result}
            )
        }
    if not pending:
        reject("no_pending_approval", "当前会话没有待审批工单。")
    if approval.draft_id != pending["draft_id"]:
        reject("draft_mismatch", "草稿与当前会话不匹配。")
    if approval.draft_version != pending["draft_version"]:
        reject("stale_version", "草稿已修改，请刷新并确认最新版本。")
    if values.get("approval_status") == "approved":
        if approval.action != "approve":
            reject("execution_pending", "该版本已批准执行，请重试确认以核查结果。")
        return {"input": None}
    if (values.get("ticket_result") or {}).get("status") in {
        "unknown",
        "conflict",
    } and approval.action != "approve":
        reject("reconciliation_required", "上次写入结果需核查，请先重试确认。")
    if not any(task.interrupts for task in snapshot.tasks):
        reject("approval_not_ready", "审批尚未就绪，请稍后刷新。")
    return {"input": Command(resume=approval.model_dump())}
