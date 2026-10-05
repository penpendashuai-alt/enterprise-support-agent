import asyncio
import hashlib
import importlib
import json
import socket
import traceback
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from langchain_core.messages import AIMessage, ToolMessage
from psycopg import sql
from psycopg.conninfo import make_conninfo

from eval_support.ledger import Ledger, now, write
from eval_support.schema import digest
from eval_support.scoring import export_reviews, structural


async def run(args, dataset, cases):
    from run_phase6_storage import configure

    from core import settings
    from memory.postgres import postgres_pool
    from rag.config import get_settings
    from rag.models import RetrievalResult
    from rag.vector_store import VectorStore
    from schema import AgentInfo
    from support_storage.migrations import migrate

    if args.output.exists():
        raise ValueError("Use a fresh run directory; no automatic task replay")
    args.output.mkdir(parents=True)
    run_id = uuid4().hex
    ledger = Ledger(args.ledger, run_id, args.budget, args.max_calls)
    if settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
        raise ValueError("Pricing contract covers only deepseek-v4-pro")
    config = get_settings()
    assert config.RAG_RETRIEVAL_MODE == "dense" and not config.RAG_DENSE_LEGACY
    assert config.RAG_DENSE_CANDIDATES == 20 and config.RAG_DENSE_THRESHOLD == 0.65
    settings.RAG_CACHE_ENABLED = args.cache
    settings.ADMISSION_ENABLED = True
    settings.REDIS_NAMESPACE = "esa:p8:" + run_id
    settings.AUTH_SECRET = None
    settings.LANGFUSE_TRACING = False
    settings.SUPPORT_DEMO_SAMPLES = False
    settings.REQUEST_TIMEOUT = 120
    connection = json.loads(args.connection_json.read_text(encoding="utf-8"))
    db = "phase8_" + run_id
    with psycopg.connect(make_conninfo(**connection), autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    configure({**connection, "dbname": db})
    settings.POSTGRES_MAX_CONNECTIONS_PER_POOL = 1
    async with postgres_pool("phase8_migration", autocommit=False) as pool:
        await migrate(pool)
    support = importlib.import_module("agents.support_agent")
    answers = importlib.import_module("rag.answers")
    retrieval = importlib.import_module("rag.retriever")
    service = importlib.import_module("service.service")
    service.get_all_agent_info = lambda: [
        AgentInfo(key="support-agent", description="Phase 8 isolated evaluation")
    ]
    model = support.get_support_model({}).model_copy(
        update={
            "temperature": 0,
            "max_tokens": 1800,
            "max_retries": 0,
            "stream_usage": True,
            "callbacks": [ledger],
        }
    )
    support.get_support_model = lambda _: model
    finalize = answers.finalize
    captured = []

    def observe(response, result, *a, **kw):
        final = finalize(response, result, *a, **kw)
        captured.append(
            {"before": response.content, "after": final.content, "retrieval": result.model_dump()}
        )
        return final

    support.finalize = answers.finalize = observe
    restore_embeddings = ledger.patch_embeddings()
    original_retrieve = retrieval.retrieve
    vector = VectorStore(config)
    try:
        manifest = await vector.manifest()
    finally:
        await vector.close()
    source_hashes = {
        str(p.relative_to(args.source)).replace("\\", "/"): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in (args.source / "src").rglob("*.py")
    }
    evaluator_root = Path(__file__).resolve().parent
    report = {
        "run_id": run_id,
        "created_at": now(),
        "variant": args.variant,
        "split": args.split,
        "layer": args.layer,
        "database": db,
        "dataset_sha256": digest(dataset.model_dump()),
        "planned_cases": [c.case_id for c in cases],
        "source_sha256": source_hashes,
        "evaluator_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in evaluator_root.glob("*.py")
        },
        "prompt_sha256": digest(
            {
                "router": support.ROUTER_PROMPT,
                "handler": support.HANDLER_PROMPT,
                "evidence_formatter_source": Path(answers.__file__).read_text(encoding="utf-8"),
            }
        ),
        "index_manifest": manifest,
        "config": {
            "model": settings.COMPATIBLE_MODEL,
            "temperature": 0,
            "max_output_tokens": 1800,
            "max_retries": 0,
            "thinking": "disabled",
            "cache": args.cache,
            "admission": True,
            "worker_count": 1,
            "pool_per_role": 1,
            "retrieval": "Dense20/v2/threshold0.65/final5",
        },
        "execution_order": [c.case_id for c in cases],
        "records": [],
        "status": "running",
        "transport": "real loopback HTTP; in-process evaluator reads checkpoint and PostgreSQL",
    }
    write(args.output / "manifest.json", report)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(service.app, host="127.0.0.1", port=port, log_level="error")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("Service failed to start")
        await asyncio.sleep(0.05)
    try:
        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=130
            ) as client,
            postgres_pool("phase8_audit", autocommit=True) as audit,
        ):
            for case in cases:
                if ledger.exhausted:
                    break
                ledger.case_id = case.case_id
                users = {
                    role: f"p8-{run_id[:8]}-{case.case_id}-{role}" for role in ("alice", "bob")
                }
                threads = {slot: uuid4().hex for slot in ("primary", "secondary")}
                record = {
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "attempt": 1,
                    "variant": args.variant,
                    "layer": args.layer,
                    "status": "running",
                    "started_at": now(),
                    "turns": [],
                    "users": users,
                    "threads": threads,
                }
                draft = None
                started = perf_counter()

                async def ticket_count():
                    async with audit.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT count(*) AS count FROM tickets WHERE user_id=ANY(%s)",
                                (list(users.values()),),
                            )
                        ).fetchone()
                        return row["count"]

                async def unavailable(query):
                    return RetrievalResult(
                        status="unavailable",
                        query=query,
                        error_code="phase8_injected_dependency_unavailable",
                    )

                retrieval.retrieve = (
                    unavailable if case.fault == "retrieval_unavailable" else original_retrieve
                )
                try:
                    for index, turn in enumerate(case.turns):
                        ledger.turn = index
                        captured.clear()
                        thread, user_id = threads[turn.thread], users[turn.user]
                        before = await support.support_agent.aget_state(
                            {"configurable": {"thread_id": thread}}
                        )
                        prior_ids = (
                            {m.id for m in before.values.get("messages", [])}
                            if before.values
                            else set()
                        )
                        request = {"user_id": user_id, "thread_id": thread}
                        if turn.operation == "user":
                            request["message"] = turn.text
                            method, path, kwargs = (
                                "POST",
                                "/support-agent/stream" if turn.stream else "/support-agent/invoke",
                                {"json": request},
                            )
                        elif turn.operation in {"approve", "cancel", "edit"}:
                            if draft is None:
                                record["status"], record["failure_stage"] = (
                                    "protocol_failure",
                                    "approval_storage",
                                )
                                break
                            decision = {
                                "draft_id": draft["draft_id"],
                                "draft_version": draft["draft_version"],
                                "action": turn.operation,
                            }
                            if turn.operation == "edit":
                                decision["draft"] = {**draft["draft"], **turn.values}
                            request["approval"] = decision
                            method, path, kwargs = (
                                "POST",
                                "/support-agent/invoke",
                                {"json": request},
                            )
                        elif turn.operation == "history":
                            method, path, kwargs = (
                                "POST",
                                "/support-agent/history",
                                {"json": request},
                            )
                        else:
                            method = {
                                "prefs_put": "PUT",
                                "prefs_get": "GET",
                                "prefs_delete": "DELETE",
                            }[turn.operation]
                            path, kwargs = (
                                "/support-agent/preferences",
                                {"params": {"user_id": user_id}},
                            )
                            if method == "PUT":
                                kwargs["json"] = turn.values
                        point = perf_counter()
                        response = await client.request(method, path, **kwargs)
                        events = []
                        sse_error = None
                        if turn.stream and response.status_code == 200:
                            for line in response.text.splitlines():
                                if line.startswith("data: ") and line != "data: [DONE]":
                                    events.append(json.loads(line[6:]))
                            sse_error = next((e for e in events if e["type"] == "error"), None)
                            if "data: [DONE]" not in response.text:
                                sse_error = {"code": "missing_done"}
                            body = next(
                                (e["content"] for e in reversed(events) if e["type"] == "message"),
                                {},
                            )
                        else:
                            body = response.json()
                        snapshot = await support.support_agent.aget_state(
                            {"configurable": {"thread_id": thread}}
                        )
                        state = snapshot.values or {}
                        messages = [m for m in state.get("messages", []) if m.id not in prior_ids]
                        tools = []
                        for message in messages:
                            if isinstance(message, AIMessage):
                                for call in message.tool_calls:
                                    tool_message = next(
                                        (
                                            m
                                            for m in messages
                                            if isinstance(m, ToolMessage)
                                            and m.tool_call_id == call["id"]
                                        ),
                                        None,
                                    )
                                    result = (
                                        json.loads(str(tool_message.content))
                                        if tool_message
                                        else {}
                                    )
                                    tools.append(
                                        {
                                            **call,
                                            "result": result,
                                            "validation_error": "工具参数无效"
                                            in result.get("message", ""),
                                        }
                                    )
                        serial = {
                            k: v.model_dump() if hasattr(v, "model_dump") else v
                            for k, v in state.items()
                            if k not in {"messages", "retrieval"}
                        }
                        row = {
                            "index": index,
                            "operation": turn.operation,
                            "user_message": turn.text,
                            "http_status": response.status_code,
                            "seconds": perf_counter() - point,
                            "request_id": response.headers.get("x-request-id"),
                            "response": body,
                            "state": serial,
                            "retrieval": state.get("retrieval"),
                            "tools": tools,
                            "validation_records": list(captured),
                            "sse_events": events,
                            "sse_error": sse_error,
                            "ticket_count": await ticket_count(),
                        }
                        if body.get("custom_data", {}).get("kind") == "ticket_approval":
                            draft = body["custom_data"]
                        record["turns"].append(row)
                        if args.cache and (row.get("retrieval") or {}).get("status") == "ok":
                            cold = row["retrieval"]
                            warm = await original_retrieve(cold["query"])
                            row["cache_probe"] = {
                                "mode": "exact-query retrieval replay; no repeated business action",
                                "cache": warm.cache,
                                "evidence_equal": cold["evidence"]
                                == [e.model_dump() for e in warm.evidence],
                                "usage": warm.usage,
                            }
                        write(args.output / f"{case.case_id}.json", record)
                        if ledger.exhausted:
                            record["status"] = "budget_stop"
                            break
                        await asyncio.sleep(0.15)
                    else:
                        record["status"] = "completed"
                except Exception as exc:
                    traceback.print_exc()
                    record.update(
                        status="infrastructure_error"
                        if isinstance(exc, (httpx.HTTPError, psycopg.Error))
                        else "evaluator_error",
                        error_type=type(exc).__name__,
                        failure_stage="execution_control"
                        if isinstance(exc, httpx.HTTPError)
                        else "evaluator",
                    )
                    print(
                        json.dumps({"failed_case": case.case_id, "error_type": type(exc).__name__}),
                        flush=True,
                    )
                finally:
                    record.update(
                        seconds=perf_counter() - started,
                        final_ticket_count=await ticket_count(),
                        final_draft_version=draft.get("draft_version") if draft else None,
                    )
                    record["structural"] = structural(case, record)
                    record["model_calls"] = [
                        e["call_id"]
                        for e in ledger.events()
                        if e["run_id"] == run_id and e["case_id"] == case.case_id
                    ]
                    write(args.output / f"{case.case_id}.json", record)
                    report["records"].append(case.case_id)
                    write(args.output / "manifest.json", report)
                print(
                    json.dumps(
                        {
                            "case": case.case_id,
                            "status": record["status"],
                            "structural": record["structural"]["passed"],
                        }
                    ),
                    flush=True,
                )
                if record["status"] == "evaluator_error":
                    break
    finally:
        retrieval.retrieve = original_retrieve
        restore_embeddings()
        server.should_exit = True
        await serving
        report["status"] = (
            "complete"
            if len(report["records"]) == len(cases) and not ledger.exhausted
            else "incomplete"
        )
        report["finished_at"] = now()
        write(args.output / "manifest.json", report)
        records = [
            json.loads((args.output / f"{case_id}.json").read_text(encoding="utf-8"))
            for case_id in report["records"]
        ]
        write(args.output / "reviews.pending.json", export_reviews(records))
