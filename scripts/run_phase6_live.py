"""Opt-in paid Phase 6 regression (CNY 10 maximum authorization), isolated PostgreSQL database."""

import argparse
import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from run_phase4_scenarios import Usage
from run_phase6_storage import configure

import agents.support_agent as support
from core import settings
from memory.postgres import postgres_pool
from rag.config import get_settings
from schema import AgentInfo
from support_storage.migrations import migrate


async def run(args):
    if args.output.exists():
        raise ValueError("Preserve prior reports and billing evidence")
    prior = sum(
        json.loads(p.read_text(encoding="utf-8")).get("estimated_cny", 0)
        for p in args.output.parent.glob("live*.json")
    )
    tracker = Usage(10, prior)
    if prior >= 5 or settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
        raise ValueError("Budget or model pricing contract mismatch")
    config = get_settings()
    if (
        config.RAG_RETRIEVAL_MODE != "dense"
        or config.RAG_DENSE_LEGACY
        or config.RAG_DENSE_CANDIDATES != 20
        or config.RAG_DENSE_THRESHOLD != 0.65
    ):
        raise ValueError("Preserve Phase 5 Dense20 selection")
    connection = json.loads(args.connection_json.read_text(encoding="utf-8"))
    database = "phase6_live_" + uuid4().hex
    with psycopg.connect(make_conninfo(**connection), autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    configure({**connection, "dbname": database})
    async with postgres_pool("migration", autocommit=False) as pool:
        await migrate(pool)
    service = importlib.import_module("service.service")
    service.get_all_agent_info = lambda: [
        AgentInfo(key="support-agent", description="live validation")
    ]
    settings.AUTH_SECRET = None
    settings.LANGFUSE_TRACING = False
    settings.SUPPORT_DEMO_SAMPLES = False
    model = support.get_support_model({}).model_copy(
        update={
            "callbacks": [tracker],
            "max_tokens": 1200,
            "max_retries": 0,
            "stream_usage": True,
            "temperature": 0,
        }
    )
    support.get_support_model = lambda _: model
    report = {
        "date": datetime.now(UTC).isoformat(),
        "database": database,
        "budget_cny": 10,
        "model": settings.COMPATIBLE_MODEL,
        "pricing": {
            "input_cny_per_million": 9,
            "output_cny_per_million": 27,
            "embedding_cny_per_million": 0.5,
            "note": "Conservative peak uncached estimate; not provider invoice",
            "sources": [
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "https://help.aliyun.com/zh/model-studio/embedding",
            ],
        },
        "cases": [],
        "rag": {
            "candidate_k": config.RAG_DENSE_CANDIDATES,
            "final_k": config.RAG_FINAL_K,
            "min_score": config.RAG_DENSE_THRESHOLD,
            "collection": config.QDRANT_COLLECTION,
        },
    }
    embeddings = 0
    try:
        async with (
            service.lifespan(service.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=service.app), base_url="http://test", timeout=120
            ) as client,
        ):

            async def prefs(method, **kwargs):
                r = await client.request(
                    method, "/support-agent/preferences", params={"user_id": "live-alice"}, **kwargs
                )
                r.raise_for_status()
                return r.json()

            async def ask(case, message, thread=None):
                nonlocal embeddings
                thread = thread or "live-" + uuid4().hex
                r = await client.post(
                    "/support-agent/invoke",
                    json={"message": message, "thread_id": thread, "user_id": "live-alice"},
                )
                r.raise_for_status()
                snapshot = await support.support_agent.aget_state(
                    {"configurable": {"thread_id": thread}}
                )
                retrieval = snapshot.values.get("retrieval")
                embeddings += (retrieval or {}).get("usage", {}).get("embedding_tokens", 0)
                report["cases"].append(
                    {
                        "case": case,
                        "question": message,
                        "response": r.json(),
                        "intent": snapshot.values.get("intent"),
                        "retrieval": retrieval,
                        "thread_id": thread,
                    }
                )
                print(
                    json.dumps(
                        {"completed_case": case, "estimated_chat_cny": tracker.estimated_cny}
                    ),
                    flush=True,
                )
                return r.json(), thread

            await prefs("PUT", json={"language": "en", "detail": "concise"})
            _, old_thread = await ask("saved_english", "DNS 是什么？")
            await ask("same_user_new_thread", "VPN 是什么？")
            await ask("current_request_overrides", "请用中文详细解释 DNS 的作用，列出三个要点。")
            await prefs("DELETE")
            await ask("deleted_not_revived", "请解释 DHCP 的用途。", old_thread)
            if args.preferences_only:
                report["execution_status"] = "completed; preference repair regression"
                return
            draft, thread = await ask(
                "approval_boundary", "VPN 报错 809，只有我不能办公，重启无效，请创建 P2 工单。"
            )
            payload = draft["custom_data"]
            assert payload["kind"] == "ticket_approval"
            approved = await client.post(
                "/support-agent/invoke",
                json={
                    "thread_id": thread,
                    "user_id": "live-alice",
                    "approval": {
                        "draft_id": payload["draft_id"],
                        "draft_version": payload["draft_version"],
                        "action": "approve",
                    },
                },
            )
            approved.raise_for_status()
            report["approved"] = approved.json()
            ticket = approved.json()["custom_data"]["ticket"]["ticket_id"]
            await ask("business_ticket_query", f"请查询工单 {ticket}。")
            await ask("dense20_vpn809", "VPN 报错 809，设备 LAPTOP-001，应该如何排查？")
            await ask("known_p1_short_question", "公司 P1 工单如何判定？必须在几分钟内解决？")
            await ask("mock_service_source", "GitHub 当前运行正常吗？")
            report["execution_status"] = "completed; semantic results require individual review"
    finally:
        report["usage"] = {
            "calls": len(tracker.calls),
            "chat_input_tokens": sum(r["input_tokens"] for r in tracker.calls),
            "chat_output_tokens": sum(r["output_tokens"] for r in tracker.calls),
            "embedding_tokens": embeddings,
        }
        report["estimated_cny"] = tracker.estimated_cny + embeddings * 0.5 / 1_000_000
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"cases": len(report["cases"]), "estimated_cny": report["estimated_cny"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preferences-only", action="store_true")
    asyncio.run(run(parser.parse_args()), loop_factory=asyncio.SelectorEventLoop)
