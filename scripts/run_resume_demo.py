"""Final browser demo: real metered model, formal corpus, authenticated isolated service."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".cache/resume-20261022/demo.json"
CAMPAIGN = ROOT / "evaluation/resume-improvement/20261022-r1"
REQUEST = ContextVar("demo_request", default={"case_id": "startup", "turn": -1})


def prepare():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    if PRIVATE.exists():
        raise ValueError("Reuse saved demo identity for recovery; never overwrite")
    connection = json.loads(
        (ROOT / ".cache/resume-20261022/connection.json").read_text(encoding="utf-8")
    )
    database = "resume_demo_" + uuid4().hex
    with psycopg.connect(make_conninfo(**connection), autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    value = {
        "connection": {**connection, "dbname": database},
        "auth_secret": secrets.token_urlsafe(32),
        "namespace": "esa:resume:demo:" + uuid4().hex,
        "service_port": 18881,
        "ui_port": 18851,
    }
    PRIVATE.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print("Private demo connection saved; no paid calls")


def configure(private):
    for key, value in private["connection"].items():
        os.environ[
            "POSTGRES_"
            + {
                "host": "HOST",
                "port": "PORT",
                "user": "USER",
                "password": "PASSWORD",
                "dbname": "DB",
            }[key]
        ] = str(value)
    os.environ.update(
        PYTHONPATH=str(ROOT / "src"),
        DATABASE_TYPE="postgres",
        AUTH_SECRET=private["auth_secret"],
        DEPLOYMENT_MODE="true",
        ENABLED_AGENTS='["support-agent"]',
        CI_TEST_MODE="false",
        USE_FAKE_MODEL="false",
        REDIS_URL="redis://127.0.0.1:16422/0",
        REDIS_NAMESPACE=private["namespace"],
        RAG_CACHE_ENABLED="true",
        QDRANT_LOCAL="true",
        QDRANT_URL="http://127.0.0.1:16433",
        QDRANT_API_KEY="",
        QDRANT_COLLECTION="enterprise_support_dense_resume20261022",
        SUPPORT_DEMO_SAMPLES="true",
        LANGFUSE_TRACING="false",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        AGENT_URL=f"http://127.0.0.1:{private['service_port']}",
        POSTGRES_MAX_CONNECTIONS_PER_POOL="2",
    )


async def serve(private):
    import importlib

    import uvicorn
    from resume_support.runtime import PhysicalObserver

    from memory.postgres import postgres_pool
    from rag.config import get_settings
    from rag.vector_store import VectorStore
    from support_storage.migrations import migrate

    class Observer(PhysicalObserver):
        def context(self):
            return {"run_id": self.run_id, **REQUEST.get()}

    output = CAMPAIGN / "demo/browser"
    output.mkdir(parents=True, exist_ok=True)
    observer = Observer(CAMPAIGN, "demo", uuid4().hex)
    restore = observer.patch_embeddings()
    async with postgres_pool("resume_demo_migration", autocommit=False) as pool:
        await migrate(pool)
    vector = VectorStore(get_settings())
    try:
        index = await vector.manifest()
    finally:
        await vector.close()
    support = importlib.import_module("agents.support_agent")
    service = importlib.import_module("service.service")
    model = observer.model()
    support.get_support_model = lambda _: model
    manifest = {
        "run_id": observer.run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "database": private["connection"]["dbname"],
        "index": index,
        "model": "deepseek-v4-pro",
        "temperature": 0,
        "max_output_tokens": 1800,
        "thinking": "disabled",
        "real_model": True,
        "auth": "Bearer enabled; secret private",
        "source_sha256": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "src").rglob("*.py")
        },
    }
    (output / f"server-{observer.run_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    class Recorder:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not scope["path"].startswith("/support-agent"):
                await service.app(scope, receive, send)
                return
            cid = "browser-" + uuid4().hex
            token = REQUEST.set({"case_id": cid, "turn": 0})
            incoming, outgoing, response_headers = [], [], {}
            status = None

            async def received():
                event = await receive()
                if event["type"] == "http.request":
                    incoming.append(event.get("body", b""))
                return event

            async def sent(event):
                nonlocal status
                if event["type"] == "http.response.start":
                    status = event["status"]
                    response_headers.update(
                        {
                            k.decode(): v.decode()
                            for k, v in event.get("headers", [])
                            if k in {b"x-request-id", b"server-timing", b"content-type"}
                        }
                    )
                if event["type"] == "http.response.body":
                    outgoing.append(event.get("body", b""))
                await send(event)

            try:
                await service.app(scope, received, sent)
            finally:
                record = {
                    "case_id": cid,
                    "run_id": observer.run_id,
                    "path": scope["path"],
                    "query": scope.get("query_string", b"").decode(errors="replace"),
                    "method": scope["method"],
                    "status": status,
                    "request_body": b"".join(incoming).decode(errors="replace"),
                    "response_body": b"".join(outgoing).decode(errors="replace"),
                    "response_headers": response_headers,
                    "at": datetime.now(UTC).isoformat(),
                }
                (output / (cid + ".json")).write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                observer.export()
                REQUEST.reset(token)

    try:
        await uvicorn.Server(
            uvicorn.Config(
                Recorder(), host="127.0.0.1", port=private["service_port"], log_level="warning"
            )
        ).serve()
    finally:
        observer.export()
        restore()
        await model.http_async_client.aclose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["prepare", "serve", "ui"])
    p.add_argument("--paid", action="store_true")
    args = p.parse_args()
    if args.mode == "prepare":
        prepare()
        return
    if not args.paid:
        p.error("Browser actions can call providers: --paid required")
    private = json.loads(PRIVATE.read_text(encoding="utf-8"))
    configure(private)
    sys.path.insert(0, str(ROOT / "src"))
    if args.mode == "ui":
        subprocess.run(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "src/streamlit_app.py",
                "--server.address=127.0.0.1",
                f"--server.port={private['ui_port']}",
                "--server.headless=true",
                "--browser.gatherUsageStats=false",
                "--server.runOnSave=false",
            ],
            check=True,
        )
    else:
        asyncio.run(serve(private), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
