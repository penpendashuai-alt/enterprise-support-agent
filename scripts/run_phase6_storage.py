"""Real PostgreSQL integration, separate service processes, and optional owned-cluster outage.

Requires CREATEDB on the configured development instance. Creates and retains a fresh
phase6_test_* database for inspection; never clears the configured application database.
"""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from core import settings
from memory.postgres import postgres_pool
from support_storage.import_sqlite import import_records, read_source
from support_storage.migrations import migrate, verify_schema
from support_storage.postgres import PostgresRepository
from tickets.models import TicketDraft
from tickets.repository import TicketConflict, TicketRepository

ROOT = Path(__file__).resolve().parents[1]
DRAFT = TicketDraft(
    title="VPN 故障",
    description="VPN 报错 809，已重启",
    service_name="VPN",
    impact="只有本人",
    priority="P2",
)


def configure(connection):
    from pydantic import SecretStr

    from core.settings import DatabaseType

    settings.DATABASE_TYPE = DatabaseType.POSTGRES
    for key, value in connection.items():
        name = {
            "user": "USER",
            "password": "PASSWORD",
            "host": "HOST",
            "port": "PORT",
            "dbname": "DB",
        }[key]
        setattr(settings, "POSTGRES_" + name, SecretStr(value) if key == "password" else value)
    settings.POSTGRES_MAX_CONNECTIONS_PER_POOL = 4
    settings.POSTGRES_POOL_TIMEOUT = 3


async def repository_checks(work):
    async with postgres_pool("integration", autocommit=False) as pool:
        first = await migrate(pool)
        assert first and await migrate(pool) == []
        await verify_schema(pool)
        repo = PostgresRepository(pool)
        await repo.register("alice", "concurrent", "parallel")
        draft_id = "draft-" + uuid4().hex
        # Hold four distinct connections simultaneously before competing writes.
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            conns = [await stack.enter_async_context(pool.connection()) for _ in range(4)]
            pids = [
                (await (await c.execute("SELECT pg_backend_pid() AS pid")).fetchone())["pid"]
                for c in conns
            ]
            assert len(set(pids)) == 4
        records = await asyncio.gather(
            *(repo.create(DRAFT, draft_id, 1, "concurrent", "alice") for _ in range(16))
        )
        assert len({r.ticket_id for r in records}) == 1
        for draft, version, user in [
            (DRAFT, 2, "alice"),
            (DRAFT.model_copy(update={"priority": "P1"}), 1, "alice"),
            (DRAFT, 1, "bob"),
        ]:
            try:
                await repo.create(draft, draft_id, version, "concurrent", user)
                raise AssertionError("Expected conflict")
            except TicketConflict:
                pass
        assert await repo.get(records[0].ticket_id, "bob") is None
        assert len(await repo.tickets("alice", 100)) == 1
        source = work / "source.sqlite"
        legacy = TicketRepository(str(source))
        for i in range(3):
            legacy.create(DRAFT, "draft-" + uuid4().hex, 1, f"legacy-{i}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        old = read_source(source)
        mapping = {r.ticket_id: "import-owner" for r in old[:2]}
        preflight = await import_records(pool, old, mapping)
        assert preflight["new"] == 2 and preflight["unprocessed"] == 1
        assert await repo.tickets("import-owner", 100) == []
        imported = await import_records(pool, old, mapping, apply=True)
        repeated = await import_records(pool, old, mapping, apply=True)
        assert imported["created"] == 2 and repeated["reused"] == 2
        assert repeated["created"] == 0 and repeated["unprocessed"] == 1
        conflict = await import_records(
            pool, old, {old[0].ticket_id: "wrong", old[2].ticket_id: "import-owner"}, apply=True
        )
        assert conflict["status"] == "rejected_conflicts"
        assert len(await repo.tickets("import-owner", 100)) == 2
        assert await repo.sessions("import-owner", 100) == []
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        return {
            "independent_connections": len(set(pids)),
            "concurrent_requests": 16,
            "created_rows": 1,
            "content_version_owner_conflicts": "passed",
            "migration_repeat": "passed",
            "sqlite_import": {
                k: {n: r[n] for n in ["created", "reused", "conflicts", "unprocessed"]}
                for k, r in [
                    ("preflight", preflight),
                    ("first", imported),
                    ("repeat", repeated),
                    ("conflict", conflict),
                ]
            },
            "source_unchanged": True,
            "import_does_not_create_sessions": True,
        }


def run(args):
    if args.output.exists():
        raise ValueError("Output exists; preserve previous evidence")
    if bool(args.pg_ctl) != bool(args.pg_data):
        raise ValueError(
            "Supply both pg-ctl and pg-data for the explicitly selected local instance"
        )
    connection = json.loads(args.connection_json.read_text(encoding="utf-8"))
    name = "phase6_test_" + uuid4().hex
    with psycopg.connect(make_conninfo(**connection), autocommit=True) as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    connection["dbname"] = name
    configure(connection)
    work = ROOT / ".cache" / name
    work.mkdir(parents=True)
    report = {
        "database": name,
        "postgres_version": version,
        "model": "deterministic test fixture; zero paid calls",
        "compose": "not executed",
    }
    report["repository"] = asyncio.run(
        repository_checks(work), loop_factory=asyncio.SelectorEventLoop
    )
    env = os.environ.copy()
    for key, value in connection.items():
        env[
            "POSTGRES_"
            + {
                "user": "USER",
                "password": "PASSWORD",
                "host": "HOST",
                "port": "PORT",
                "dbname": "DB",
            }[key]
        ] = str(value)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(
        DATABASE_TYPE="postgres",
        PYTHONPATH=str(ROOT / "src"),
        AUTH_SECRET="",
        LANGFUSE_TRACING="false",
        LANGSMITH_TRACING="false",
        SUPPORT_DEMO_SAMPLES="false",
        POSTGRES_POOL_TIMEOUT="3",
        POSTGRES_CONNECT_TIMEOUT="2",
        PHASE6_TEST_PORT=str(port),
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False)
    processes, logs = [], []

    def start(crash=False):
        log = (work / f"service-{len(processes)}.log").open("w", encoding="utf-8")
        logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/phase6_test_server.py")],
            env={**env, "PHASE6_TEST_CRASH_AFTER_COMMIT": "1" if crash else "0"},
            cwd=ROOT,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(proc)
        for _ in range(150):
            if proc.poll() is not None:
                raise RuntimeError(f"Fixture startup failed; inspect {work.name} local service log")
            try:
                if client.get("/info").status_code == 200:
                    return proc
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise TimeoutError("Service readiness timeout")

    def request(method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == expected, (path, response.status_code, response.text[:300])
        return response.json()

    def invoke(thread, **body):
        return request(
            "POST", "/support-agent/invoke", json={"thread_id": thread, "user_id": "alice", **body}
        )

    def pending(thread):
        return request(
            "GET", "/support-agent/approval", params={"thread_id": thread, "user_id": "alice"}
        )["pending"]

    def decision(p):
        return {"draft_id": p["draft_id"], "draft_version": p["draft_version"], "action": "approve"}

    try:
        proc = start()
        request(
            "PUT",
            "/support-agent/preferences",
            params={"user_id": "alice"},
            json={"language": "en", "detail": "concise"},
        )
        draft = invoke("restart-pending", message="建工单")["custom_data"]
        assert pending("restart-pending") == draft
        history_before = request(
            "POST",
            "/support-agent/history",
            json={"thread_id": "restart-pending", "user_id": "alice"},
        )
        with psycopg.connect(make_conninfo(**connection)) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM tickets WHERE draft_id=%s", (draft["draft_id"],)
                ).fetchone()[0]
                == 0
            )
        proc.terminate()
        proc.wait(15)
        proc = start()
        assert pending("restart-pending") == draft
        assert (
            request(
                "POST",
                "/support-agent/history",
                json={"thread_id": "restart-pending", "user_id": "alice"},
            )
            == history_before
        )
        assert (
            request("GET", "/support-agent/preferences", params={"user_id": "alice"})[
                "preferences"
            ]["language"]
            == "en"
        )
        assert (
            request("GET", "/support-agent/preferences", params={"user_id": "bob"})["preferences"]
            is None
        )
        request(
            "POST",
            "/support-agent/history",
            expected=403,
            json={"thread_id": "restart-pending", "user_id": "bob"},
        )
        request(
            "GET",
            "/support-agent/approval",
            expected=403,
            params={"thread_id": "restart-pending", "user_id": "bob"},
        )
        request(
            "POST",
            "/chatbot/history",
            expected=403,
            json={"thread_id": "restart-pending", "user_id": "alice"},
        )
        request(
            "POST",
            "/support-agent/invoke",
            expected=422,
            json={
                "thread_id": "restart-pending",
                "user_id": "alice",
                "message": "test",
                "agent_config": {"user_id": "bob"},
            },
        )
        request("DELETE", "/support-agent/preferences", params={"user_id": "alice"})
        result = invoke("restart-pending", approval=decision(draft))["custom_data"]
        ticket = result["ticket"]["ticket_id"]
        assert (
            invoke("restart-pending", approval=decision(draft))["custom_data"]["ticket"][
                "ticket_id"
            ]
            == ticket
        )
        assert (
            request("GET", "/support-agent/preferences", params={"user_id": "alice"})["preferences"]
            is None
        )
        new = invoke("new-thread", message="另建工单")["custom_data"]
        assert new["draft_id"] != draft["draft_id"]
        request(
            "POST",
            "/support-agent/invoke",
            json={
                "thread_id": "new-thread",
                "user_id": "alice",
                "approval": {**decision(new), "action": "cancel"},
            },
        )
        crash_draft = invoke("commit-crash", message="建工单")["custom_data"]
        proc.terminate()
        proc.wait(15)
        proc = start(crash=True)
        try:
            invoke("commit-crash", approval=decision(crash_draft))
            raise AssertionError("Expected abrupt connection loss")
        except httpx.TransportError:
            pass
        assert proc.wait(15) == 73
        with psycopg.connect(make_conninfo(**connection), row_factory=dict_row) as conn:
            committed = conn.execute(
                "SELECT ticket_id FROM tickets WHERE draft_id=%s", (crash_draft["draft_id"],)
            ).fetchall()
        assert len(committed) == 1
        proc = start()
        assert pending("commit-crash")["draft_id"] == crash_draft["draft_id"]
        recovered = invoke("commit-crash", approval=decision(crash_draft))["custom_data"]["ticket"][
            "ticket_id"
        ]
        assert recovered == committed[0]["ticket_id"]
        assert pending("commit-crash") is None
        report["process_recovery"] = {
            "pids": [p.pid for p in processes],
            "pending_preserved": True,
            "crash_exit_code": 73,
            "business_rows_before_recovery": 1,
            "same_ticket_after_recovery": True,
            "checkpoint_completed": True,
            "preferences_delete_not_revived": True,
            "ownership_checks": "passed",
        }
        if args.pg_ctl and args.pg_data:
            # This optional destructive-to-availability check is restricted to the explicitly supplied local test cluster.
            data = args.pg_data.resolve()
            if connection["host"] != "127.0.0.1" or not (data / "PG_VERSION").is_file():
                raise ValueError("Outage requires explicitly identified local cluster")
            with psycopg.connect(make_conninfo(**connection)) as conn:
                actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
                if actual != data:
                    raise ValueError("Outage target differs from connected cluster")
            ctl = [str(args.pg_ctl.resolve()), "-D", str(data)]
            request(
                "PUT",
                "/support-agent/preferences",
                params={"user_id": "alice"},
                json={"language": "zh", "detail": "detailed"},
            )
            subprocess.run([*ctl, "stop", "-m", "fast", "-w"], check=True, capture_output=True)
            try:
                assert (
                    request("GET", "/support-agent/preferences", params={"user_id": "alice"})[
                        "status"
                    ]
                    == "unavailable"
                )
                request(
                    "PUT",
                    "/support-agent/preferences",
                    expected=503,
                    params={"user_id": "alice"},
                    json={"language": "en", "detail": "concise"},
                )
                request("GET", "/support-agent/tickets", expected=503, params={"user_id": "alice"})
            finally:
                subprocess.run(
                    [*ctl, "start", "-w", "-l", str(work / "postgres-restart.log")],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            for _ in range(15):
                response = request("GET", "/support-agent/preferences", params={"user_id": "alice"})
                if response["status"] != "unavailable":
                    break
                time.sleep(0.5)
            assert response["preferences"] == {"language": "zh", "detail": "detailed"}
            assert (
                invoke("commit-crash", approval=decision(crash_draft))["custom_data"]["ticket"][
                    "ticket_id"
                ]
                == recovered
            )
            report["postgres_stop_restart"] = (
                "passed; Store read degraded, writes/business returned 503, original data recovered"
            )
        else:
            report["postgres_stop_restart"] = "not executed"
        with psycopg.connect(make_conninfo(**connection)) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM tickets WHERE draft_id=%s", (crash_draft["draft_id"],)
                ).fetchone()[0]
                == 1
            )
            report["business_ticket_rows"] = conn.execute(
                "SELECT count(*) FROM tickets"
            ).fetchone()[0]
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        client.close()
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(15)
        for log in logs:
            log.close()
        report["source_sha256"] = {
            str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ["src/support_storage", "src/memory", "src/tickets", "migrations"]
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and p.suffix in {".py", ".sql"}
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": report["status"], "database": name}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--connection-json",
        type=Path,
        required=True,
        help="Private object with host, port, user, password, dbname",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pg-ctl", type=Path)
    parser.add_argument("--pg-data", type=Path)
    run(parser.parse_args())
