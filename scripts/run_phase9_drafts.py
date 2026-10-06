"""Small paid draft-fidelity regression; no retrieval or database mutation."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from eval_support.ledger import Ledger, now, write
from langchain_core.messages import HumanMessage

from agents.ticket_flow import prepare_draft

CASES = [
    ("request", ["请创建VPN故障工单，只有我受影响，请安排人工检查。"], "人工检查仅为未来请求"),
    (
        "plan",
        ["我的邮箱无法收信，仅影响本人，请建工单。我计划今晚重启电脑，现在还没有重启。"],
        "重启为计划，明确未执行",
    ),
    ("done", ["VPN报809，仅影响本人，已重启电脑但没解决，请创建工单。"], "重启已执行但未解决"),
    (
        "not_done",
        ["VPN连不上，只有本人，我还没重启，也没联系管理员，请建工单。"],
        "两个否定操作都不得转为已尝试",
    ),
    (
        "mixed",
        ["邮箱发送失败，影响团队，已重新登录但无效，希望管理员检查权限，请建P2工单。"],
        "登录已执行，权限检查仅为请求",
    ),
    (
        "correction",
        ["VPN报错，只有本人，已重启，请建工单。", "更正：还没重启，刚才说错了，只准备下班后试。"],
        "新更正覆盖旧已执行状态",
    ),
    (
        "completion",
        ["VPN报错只有本人，尚未重启，请建工单。", "补充：现在已重启，仍失败。"],
        "最新操作已执行且失败",
    ),
    (
        "third_party",
        ["团队邮箱无法发送，同事建议重新登录，但我没试过，请建工单。"],
        "建议不等于尝试",
    ),
    (
        "negative",
        ["VPN无法连接，只有本人，没有关闭防火墙，也不希望关闭，请建工单。"],
        "禁止将否定操作描述为已执行",
    ),
    (
        "conditional",
        ["VPN故障影响本人，明天若还不好就联系管理员，目前没联系，请建工单。"],
        "条件计划尚未执行",
    ),
    (
        "request_after_action",
        ["VPN报809，只有本人，已重装客户端仍失败。请建立工单并安排人工检查。"],
        "重装已执行，人工检查未执行",
    ),
    (
        "correction_scope",
        [
            "邮箱收不到信影响整个团队，我已退出重登，请建P2工单。",
            "更正影响范围：只有本人。请帮忙重置密码，但目前没有重置。",
        ],
        "更正范围，保留登录事实，重置仅为请求",
    ),
]


class DraftLedger(Ledger):
    def on_llm_end(self, response, *, run_id, **kwargs):
        super().on_llm_end(response, run_id=run_id, **kwargs)
        path = self.root / f"{run_id}.json"
        event = json.loads(path.read_text(encoding="utf-8"))
        event["structured_tool_calls"] = response.generations[0][0].message.tool_calls
        write(path, event)


async def run(args):
    if not args.paid or not 1 < args.budget <= 20 or args.output.exists():
        raise ValueError("Explicit authorized budget and fresh output required")
    args.output.mkdir(parents=True)
    from langchain_openai import ChatOpenAI

    from agents import support_agent as support_module
    from core import settings

    model = ChatOpenAI(
        model=settings.COMPATIBLE_MODEL,
        openai_api_base=settings.COMPATIBLE_BASE_URL,
        openai_api_key=settings.COMPATIBLE_API_KEY,
        temperature=0.5,
        max_retries=0,
        max_tokens=1800,
        timeout=60,
        extra_body={"thinking": {"type": "disabled"}},
    )
    support_module.get_support_model = lambda config: model
    previous = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in args.output.parent.glob("drafts-*/calls/*.json")
    ]
    if any(e["estimated_cny"] is None for e in previous):
        raise ValueError("Unknown prior usage; resolve before further calls")
    spent = sum(e["estimated_cny"] for e in previous)
    ledger = DraftLedger(
        args.output / "calls", args.output.name, args.budget - spent, 40 - len(previous)
    )
    manifest = {
        "started_at": now(),
        "budget_cny": args.budget,
        "cases": len(CASES),
        "scope": "direct prepare_draft; previously seen failure families; non-independent semantic review",
        "prompt_source_sha256": hashlib.sha256(
            Path("src/agents/ticket_flow.py").read_bytes()
        ).hexdigest(),
    }
    write(args.output / "manifest.json", manifest)
    for case_id, inputs, criterion in CASES:
        ledger.case_id, ledger.turn = case_id, 0
        result = await prepare_draft(
            {"messages": [HumanMessage(content=t) for t in inputs]}, {"callbacks": [ledger]}
        )
        record = {
            "id": case_id,
            "inputs": inputs,
            "criterion": criterion,
            "draft": result.get("ticket_draft"),
            "status": result["approval_status"],
            "review": "pending",
            "messages": [m.content for m in result.get("messages", [])],
        }
        write(args.output / (case_id + ".json"), record)
        print(json.dumps({"id": case_id, "status": record["status"]}), flush=True)
        if ledger.exhausted:
            break
    events = ledger.events()
    write(
        args.output / "manifest.json",
        {
            **manifest,
            "finished_at": now(),
            "physical_calls": len(events),
            "estimated_cny": sum(e["estimated_cny"] or e["reserved_cny"] for e in events),
            "unknown_usage": sum(e["estimated_cny"] is None for e in events),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
