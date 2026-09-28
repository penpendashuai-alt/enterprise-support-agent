"""Run opt-in live-model scenarios through the API using an isolated SQLite checkpoint."""

import argparse
import asyncio
import hashlib
import json
import logging
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.agents import agents
from agents.support_agent import builder
from core import settings
from service import app

CASES = [
    ("P2-01", ["VPN 是什么？"], "general_question", [], "一般知识回答，无工具"),
    (
        "P2-02",
        ["我们公司规定 VPN 每天最多能用几个小时？"],
        "general_question",
        [],
        "未知企业政策说明缺少依据",
    ),
    (
        "P2-03",
        ["GitHub 现在有故障吗？"],
        "service_status",
        ["query_service_status"],
        "模拟 GitHub 正常",
    ),
    (
        "P2-04",
        ["邮箱服务现在正常吗？"],
        "service_status",
        ["query_service_status"],
        "模拟邮箱发送延迟",
    ),
    (
        "P2-05",
        ["Zoom 服务现在正常吗？"],
        "service_status",
        ["query_service_status"],
        "未覆盖服务，不猜测状态",
    ),
    (
        "P2-06",
        ["服务挂了吗？", "GitHub"],
        "service_status",
        ["query_service_status"],
        "先澄清，再查询 GitHub",
    ),
    (
        "P2-07",
        ["VPN 连不上，报错 809，怎么排查？"],
        "troubleshooting",
        ["search_known_issue"],
        "使用 VPN-809 固定条目",
    ),
    (
        "P2-08",
        ["设备 DEV-001 上 VPN 报错 809，帮我排查。"],
        "troubleshooting",
        ["get_device_information", "search_known_issue"],
        "使用 Windows 11 设备信息和故障条目",
    ),
    (
        "P2-09",
        ["设备 DEV-999 上 VPN 连不上，请查设备信息帮我排查。"],
        "troubleshooting",
        ["get_device_information"],
        "设备查无结果，不编造配置",
    ),
    (
        "P2-10",
        ["查一下 INC-1001 的进度。"],
        "ticket_request",
        ["query_existing_ticket"],
        "模拟工单处理中",
    ),
    (
        "P2-11",
        ["查一下 INC-9999 的进度。"],
        "ticket_request",
        ["query_existing_ticket"],
        "模拟工单查无结果",
    ),
    ("P2-12", ["帮我提工单。"], "ticket_request", [], "收集信息，明确尚未提交"),
    (
        "P2-13",
        ["VPN 连不上，帮我提工单。"],
        "ticket_request",
        [],
        "优先创建诉求，保留故障，尚未提交",
    ),
    (
        "P2-14",
        ["GitHub 现在正常吗？", "那邮箱呢？"],
        "service_status",
        ["query_service_status"],
        "切换邮箱，不沿用 GitHub 状态",
    ),
    (
        "P2-15",
        ["查 INC-1001 的进度。", "VPN 是什么？"],
        "general_question",
        [],
        "切换知识问答，清理工单实体",
    ),
]

EXPECTED_FACTS = {
    "P2-03": ("query_service_status", "success", "state", "operational"),
    "P2-04": ("query_service_status", "success", "state", "degraded"),
    "P2-05": ("query_service_status", "not_found", None, None),
    "P2-06": ("query_service_status", "success", "state", "operational"),
    "P2-07": ("search_known_issue", "success", "issue_id", "VPN-809"),
    "P2-08": ("get_device_information", "success", "os", "Windows 11"),
    "P2-09": ("get_device_information", "not_found", None, None),
    "P2-10": ("query_existing_ticket", "success", "state", "in_progress"),
    "P2-11": ("query_existing_ticket", "not_found", None, None),
    "P2-14": ("query_service_status", "success", "state", "degraded"),
}


def matches_expected_fact(case_id, turn):
    if case_id not in EXPECTED_FACTS:
        return True
    name, status, field, value = EXPECTED_FACTS[case_id]
    for call, result in zip(turn["tool_calls"], turn["tool_results"], strict=True):
        if call["name"] != name or result["status"] != status:
            continue
        if field is None:
            return True
        data = result["data"]
        records = data if isinstance(data, list) else [data]
        if any(record.get(field) == value for record in records):
            return True
    return False


def code_version():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in (
        "src/agents/support_agent.py",
        "src/agents/support_tools.py",
        "src/agents/agents.py",
        "scripts/run_phase2_scenarios.py",
    ):
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return {
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "implementation_sha256": digest.hexdigest(),
        "working_tree": "uncommitted changes may be present; fingerprint identifies implementation",
    }


async def run(output: Path, selected: set[str]):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    report = {
        "run_date": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "model": str(settings.DEFAULT_MODEL),
        "provider_model": settings.COMPATIBLE_MODEL
        if str(settings.DEFAULT_MODEL) == "openai-compatible"
        else str(settings.DEFAULT_MODEL),
        "mode": "real configured model; in-process FastAPI; temporary SQLite; fixed mock tools",
        "assessment": "Automated checks cover intent, tool choice and state only. Read responses for semantic acceptance.",
        "cases": [],
    }
    headers = {}
    if settings.AUTH_SECRET:
        headers["Authorization"] = f"Bearer {settings.AUTH_SECRET.get_secret_value()}"
    with tempfile.TemporaryDirectory(prefix="phase2-") as directory:
        async with AsyncSqliteSaver.from_conn_string(
            str(Path(directory) / "checkpoints.db")
        ) as saver:
            graph = builder.compile(checkpointer=saver)
            original = agents["support-agent"].graph_like
            agents["support-agent"].graph_like = graph
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://phase2-test",
                    headers=headers,
                ) as client:
                    for index, (case_id, turns, intent, expected_tools, expected) in enumerate(
                        CASES
                    ):
                        if selected and case_id not in selected:
                            continue
                        endpoint = "stream" if index % 2 else "invoke"
                        record = {
                            "id": case_id,
                            "endpoint": endpoint,
                            "expected": expected,
                            "turns": [],
                        }
                        try:
                            for question in turns:
                                async with asyncio.timeout(180):
                                    response = await client.post(
                                        f"/support-agent/{endpoint}",
                                        json={
                                            "message": question,
                                            "thread_id": case_id,
                                            "user_id": "phase2-demo",
                                        },
                                    )
                                response.raise_for_status()
                                if endpoint == "stream":
                                    events = [
                                        json.loads(line[6:])
                                        for line in response.text.splitlines()
                                        if line.startswith("data: ") and line != "data: [DONE]"
                                    ]
                                    if (
                                        any(e["type"] == "error" for e in events)
                                        or "data: [DONE]" not in response.text
                                    ):
                                        raise RuntimeError("SSE protocol error")
                                    leaked = any(
                                        e["type"] == "token"
                                        and '"needs_clarification"' in e["content"]
                                        for e in events
                                    )
                                    if leaked:
                                        raise RuntimeError("Router output leaked")
                                snapshot = await graph.aget_state(
                                    {"configurable": {"thread_id": case_id}}
                                )
                                state = snapshot.values
                                messages = state["messages"]
                                start = max(
                                    i for i, m in enumerate(messages) if isinstance(m, HumanMessage)
                                )
                                current = messages[start:]
                                calls = [
                                    c
                                    for m in current
                                    if isinstance(m, AIMessage)
                                    for c in m.tool_calls
                                ]
                                record["turns"].append(
                                    {
                                        "input": question,
                                        "intent": state["intent"],
                                        "entities": state["entities"].model_dump(),
                                        "needs_clarification": state["needs_clarification"],
                                        "tool_calls": calls,
                                        "tool_results": [
                                            json.loads(m.content)
                                            for m in current
                                            if isinstance(m, ToolMessage)
                                        ],
                                        "response": current[-1].content,
                                    }
                                )
                            final = record["turns"][-1]
                            actual_tools = {c["name"] for c in final["tool_calls"]}
                            checks = {
                                "expected_fact": matches_expected_fact(case_id, final),
                                "no_tool_error": all(
                                    result["status"] != "error"
                                    for turn in record["turns"]
                                    for result in turn["tool_results"]
                                ),
                                "turns_completed": all(
                                    turn["intent"] is not None
                                    and (
                                        not turn["needs_clarification"]
                                        or case_id == "P2-12"
                                        or (case_id == "P2-06" and i == 0)
                                    )
                                    for i, turn in enumerate(record["turns"])
                                ),
                                "intent": final["intent"] == intent,
                                "tools": set(expected_tools) <= actual_tools
                                if expected_tools
                                else not actual_tools,
                                "mock_disclosure": "模拟" in final["response"]
                                if actual_tools
                                else True,
                                "no_submission": "尚未提交" in final["response"]
                                if case_id in {"P2-12", "P2-13"}
                                else True,
                            }
                            if case_id == "P2-06":
                                checks["clarification"] = (
                                    record["turns"][0]["needs_clarification"]
                                    and not final["needs_clarification"]
                                )
                            if case_id == "P2-14":
                                checks["topic_switch"] = final["entities"][
                                    "service_name"
                                ].casefold() in {"邮箱", "邮件", "email", "mail"}
                            if case_id == "P2-15":
                                checks["ticket_cleared"] = final["entities"]["ticket_id"] is None
                            record["checks"] = checks
                            record["automated_pass"] = all(checks.values())
                        except Exception as exc:
                            record["automated_pass"] = False
                            record["error_type"] = type(exc).__name__
                        report["cases"].append(record)
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        print(case_id, "PASS" if record["automated_pass"] else "FAIL", flush=True)
            finally:
                agents["support-agent"].graph_like = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Calls the configured real model and consumes API credits."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="*", default=[])
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new file to preserve earlier runs.")
    asyncio.run(run(args.output, set(args.cases)))
