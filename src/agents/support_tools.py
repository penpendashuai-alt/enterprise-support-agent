"""Deterministic, read-only fixtures for the Phase 2 support demonstration."""

from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, StringConstraints

from core import settings
from support_storage.identity import context_user
from tickets.service import repository

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
TicketId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_upper=True, pattern=r"(?i)^(INC-\d{4}|DEMO-[0-9a-f]{32})$"
    ),
]
DeviceId = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"(?i)^DEV-\d{3}$")
]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceInput(ToolInput):
    service_name: NonEmptyText


class TicketInput(ToolInput):
    ticket_id: TicketId


class DeviceInput(ToolInput):
    device_id: DeviceId


class IssueInput(ToolInput):
    query: NonEmptyText


def tool_result(
    status: Literal["success", "not_found", "error"],
    data: Any = None,
    message: str = "固定模拟数据，仅用于演示，不代表真实系统。",
) -> dict[str, Any]:
    return {"status": status, "data": data, "message": message, "is_mock": True}


SERVICES = {
    "github": {"service_name": "GitHub", "state": "operational", "detail": "模拟服务正常。"},
    "vpn": {"service_name": "VPN", "state": "operational", "detail": "模拟网关正常。"},
    "email": {"service_name": "邮箱", "state": "degraded", "detail": "模拟邮件发送延迟。"},
    "jira": {"service_name": "Jira", "state": "outage", "detail": "模拟服务中断，正在排查。"},
}
SERVICE_ALIASES = {"邮箱": "email", "邮件": "email", "mail": "email", "github enterprise": "github"}
TICKETS = {
    "INC-1001": {
        "ticket_id": "INC-1001",
        "title": "VPN 连接失败",
        "state": "in_progress",
        "progress": "模拟网络支持组正在检查 VPN 配置。",
    },
    "INC-1002": {
        "ticket_id": "INC-1002",
        "title": "邮箱发送延迟",
        "state": "resolved",
        "progress": "模拟问题已解决，请重试发送。",
    },
}
DEVICES = {
    "DEV-001": {"device_id": "DEV-001", "os": "Windows 11", "vpn_client": "5.0", "type": "laptop"},
    "DEV-002": {"device_id": "DEV-002", "os": "macOS 15", "vpn_client": "5.1", "type": "laptop"},
}
KNOWN_ISSUES = (
    {
        "issue_id": "VPN-809",
        "title": "Windows VPN 错误 809",
        "keywords": ["vpn", "809"],
        "steps": [
            "确认网络连接正常。",
            "核对 VPN 地址及协议。",
            "请管理员检查 UDP 500/4500 的网络策略。",
        ],
    },
    {
        "issue_id": "GITHUB-LOGIN",
        "title": "GitHub 登录异常",
        "keywords": ["github", "登录", "login"],
        "steps": ["检查服务状态。", "检查浏览器会话及企业 SSO 登录状态。", "确认双因素认证可用。"],
    },
    {
        "issue_id": "EMAIL-DELAY",
        "title": "邮件发送延迟",
        "keywords": ["邮箱", "邮件", "email", "mail"],
        "steps": ["查看邮箱服务状态。", "检查发件箱和退信提示。", "记录发生时间及错误信息。"],
    },
)


@tool(args_schema=ServiceInput)
def query_service_status(service_name: str) -> dict[str, Any]:
    """查询固定模拟服务状态。支持 GitHub、VPN、邮箱/email 和 Jira，不是实时监控。"""
    key = service_name.strip().casefold()
    candidate = key.removesuffix("服务").removesuffix(" service").strip()
    if candidate in SERVICES or candidate in SERVICE_ALIASES:
        key = candidate
    data = SERVICES.get(SERVICE_ALIASES.get(key, key))
    return (
        tool_result("success", data)
        if data
        else tool_result("not_found", message="模拟数据未覆盖该服务。")
    )


@tool(args_schema=TicketInput)
async def query_existing_ticket(ticket_id: str, config: RunnableConfig) -> dict[str, Any]:
    """按当前用户查询 DEMO 工单；INC 固定样例仅在显式演示开关启用时可用。不创建或修改。"""
    user_id = context_user(config)
    if ticket_id.upper().startswith("DEMO-"):
        record = await repository().get(ticket_id, user_id)
        return (
            {
                **tool_result(
                    "success", record.model_dump(), "演示业务数据库记录，未提交真实企业系统。"
                ),
                "source": "business_database",
                "is_mock": False,
                "is_demo": True,
            }
            if record
            else tool_result("not_found", message="未找到该本地演示工单。")
        )
    data = TICKETS.get(ticket_id.upper()) if settings.SUPPORT_DEMO_SAMPLES else None
    return (
        {**tool_result("success", data), "source": "fixed_mock_sample"}
        if data
        else tool_result("not_found", message="固定样例路径未启用或未找到该模拟工单。")
    )


@tool(args_schema=DeviceInput)
def get_device_information(device_id: str) -> dict[str, Any]:
    """按用户提供的 DEV-三位数字编号查询模拟设备信息，不猜测设备编号。"""
    data = DEVICES.get(device_id.upper())
    return (
        tool_result("success", data)
        if data
        else tool_result("not_found", message="未找到该模拟设备。")
    )


@tool(args_schema=IssueInput)
def search_known_issue(query: str) -> dict[str, Any]:
    """用简单关键词匹配固定模拟故障条目；不是知识库 RAG，也不访问网络。"""
    query = query.casefold()
    matches = [
        {key: value for key, value in issue.items() if key != "keywords"}
        for issue in KNOWN_ISSUES
        if any(word in query for word in issue["keywords"])
    ]
    return (
        tool_result("success", matches)
        if matches
        else tool_result("not_found", [], "没有匹配的模拟故障条目。")
    )


SUPPORT_TOOLS = {
    t.name: t
    for t in (
        query_service_status,
        query_existing_ticket,
        get_device_information,
        search_known_issue,
    )
}
