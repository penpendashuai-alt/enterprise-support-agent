import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Priority = Literal["P1", "P2", "P3", "P4"]


class TicketDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    description: Text
    service_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None
    ) = None
    impact: Text
    priority: Priority = "P3"


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_id: Annotated[str, StringConstraints(pattern=r"^draft-[0-9a-f]{32}$")]
    draft_version: int = Field(ge=1)
    action: Literal["approve", "cancel", "edit"]
    draft: TicketDraft | None = None

    @model_validator(mode="after")
    def validate_edit(self):
        if (self.action == "edit") != (self.draft is not None):
            raise ValueError("Only edit requires a complete draft")
        return self


class TicketRecord(BaseModel):
    ticket_id: str
    draft_id: str
    draft_version: int
    thread_id: str
    idempotency_key: str
    fingerprint: str
    draft: TicketDraft
    state: Literal["open"] = "open"
    created_at: str


def fingerprint(draft: TicketDraft) -> str:
    data = json.dumps(draft.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def request_key(draft_id: str, version: int) -> str:
    ApprovalInput(draft_id=draft_id, draft_version=version, action="approve")
    return f"{draft_id}:v{version}"


def approval_payload(state: dict) -> dict:
    draft = TicketDraft.model_validate(state["ticket_draft"])
    return {
        "kind": "ticket_approval",
        "draft_id": state["draft_id"],
        "draft_version": state["draft_version"],
        "draft": draft.model_dump(),
        "approval_status": state["approval_status"],
        "ticket_result": state.get("ticket_result"),
        "is_demo": True,
    }


def approval_summary(payload: dict) -> str:
    d = payload["draft"]
    result = payload.get("ticket_result") or {}
    warning = f"上次执行：{result['message']}\n" if result.get("message") else ""
    return (
        f"{warning}请确认本地演示工单草稿（版本 {payload['draft_version']}）：\n"
        f"标题：{d['title']}\n描述：{d['description']}\n服务：{d['service_name'] or '未指定'}\n"
        f"影响范围：{d['impact']}\n优先级：{d['priority']}（演示优先级，无 SLA 承诺）\n"
        "请使用确认创建、修改草稿或取消操作。工单尚未确认创建；不会提交到真实企业系统。"
    )


def ticket_result(record: TicketRecord) -> dict:
    return {
        "kind": "ticket_result",
        "status": "success",
        "ticket": record.model_dump(),
        "is_demo": True,
    }


def result_summary(result: dict) -> str:
    ticket = result["ticket"]
    return f"本地演示工单已创建：{ticket['ticket_id']}，状态：{ticket['state']}。标题：{ticket['draft']['title']}。未提交到真实企业系统。"
