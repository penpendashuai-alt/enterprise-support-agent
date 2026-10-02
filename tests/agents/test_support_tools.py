import pytest
from pydantic import ValidationError

from agents.support_tools import SUPPORT_TOOLS


@pytest.mark.parametrize(
    "name,args",
    [
        ("query_service_status", {"service_name": "GitHub"}),
        ("query_service_status", {"service_name": "邮箱"}),
        ("query_existing_ticket", {"ticket_id": " inc-1001 "}),
        ("get_device_information", {"device_id": " dev-001 "}),
        ("search_known_issue", {"query": "VPN 错误 809"}),
    ],
)
@pytest.mark.asyncio
async def test_stable_mock_results(name, args):
    tool = SUPPORT_TOOLS[name]
    result = await tool.ainvoke(args, {"configurable": {"user_id": "test-user"}})
    assert result == await tool.ainvoke(args, {"configurable": {"user_id": "test-user"}})
    assert result["is_mock"] is True
    assert result["status"] == "success"
    assert result["data"]


@pytest.mark.parametrize("service", ["邮箱服务", "邮件服务", "email service"])
def test_service_alias_with_suffix(service):
    result = SUPPORT_TOOLS["query_service_status"].invoke({"service_name": service})
    assert result["status"] == "success"
    assert result["data"]["state"] == "degraded"


@pytest.mark.parametrize(
    "name,args",
    [
        ("query_service_status", {"service_name": "unknown"}),
        ("query_existing_ticket", {"ticket_id": "INC-9999"}),
        ("get_device_information", {"device_id": "DEV-999"}),
        ("search_known_issue", {"query": "打印机缺纸"}),
    ],
)
@pytest.mark.asyncio
async def test_not_found(name, args):
    result = await SUPPORT_TOOLS[name].ainvoke(args, {"configurable": {"user_id": "test-user"}})
    assert result["status"] == "not_found"
    assert result["is_mock"] is True
    assert not result["data"]


@pytest.mark.parametrize(
    "name,field,bad",
    [
        ("query_service_status", "service_name", "   "),
        ("query_service_status", "service_name", 123),
        ("query_existing_ticket", "ticket_id", "INC-1"),
        ("query_existing_ticket", "ticket_id", ""),
        ("get_device_information", "device_id", "001"),
        ("search_known_issue", "query", ""),
        ("search_known_issue", "query", "x" * 501),
    ],
)
def test_invalid_arguments(name, field, bad):
    with pytest.raises(ValidationError):
        SUPPORT_TOOLS[name].invoke({field: bad})


def test_extra_arguments_rejected():
    with pytest.raises(ValidationError):
        SUPPORT_TOOLS["query_service_status"].invoke({"service_name": "vpn", "admin": True})
