"""Destructive fault tests confined to a fresh, explicitly named CI Compose project."""

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def run(args):
    if not args.project.startswith("esa-p9-test-") or args.output.exists():
        raise ValueError("Fresh esa-p9-test-* project and output required")
    args.output.mkdir(parents=True)
    env = {**os.environ, "SERVICE_PORT": str(args.port), "STREAMLIT_PORT": str(args.ui_port)}
    base = [
        "docker",
        "compose",
        "--env-file",
        "docker/ci.env",
        "-p",
        args.project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.local.yaml",
        "-f",
        "compose.ci.yaml",
    ]
    results = []
    diagnostics = Path(".cache") / f"{args.project}-commands.log"
    diagnostics.parent.mkdir(exist_ok=True)

    def compose(*command, check=True, binary=False):
        value = subprocess.run([*base, *command], env=env, capture_output=True, timeout=600)
        if check and value.returncode:
            with diagnostics.open("ab") as handle:
                handle.write(value.stdout + b"\n" + value.stderr + b"\n")
            raise RuntimeError("Compose command failed: " + command[0])
        return value.stdout if binary else value.stdout.decode(errors="replace")

    def record(name, **data):
        results.append({"check": name, "at": datetime.now(UTC).isoformat(), **data})
        (args.output / "checks.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(name, flush=True)

    def wait_ready():
        for _ in range(90):
            try:
                if client.get("/health/ready").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("Readiness deadline")

    client = httpx.Client(
        base_url=f"http://127.0.0.1:{args.port}",
        headers={"Authorization": "Bearer ci-only-shared-key"},
        timeout=15,
        trust_env=False,
    )
    user, thread = "ci-owner", "ci-" + uuid4().hex
    draft = {
        "title": "VPN故障",
        "description": "尚未重启，希望安排人工检查",
        "service_name": "VPN",
        "impact": "只有本人",
        "priority": "P2",
    }

    def invoke(**extra):
        response = client.post(
            "/support-agent/invoke", json={"user_id": user, "thread_id": thread, **extra}
        )
        response.raise_for_status()
        return response.json()

    def pending():
        response = client.get(
            "/support-agent/approval", params={"user_id": user, "thread_id": thread}
        )
        response.raise_for_status()
        return response.json()["pending"]

    def approve(payload, action, **extra):
        return invoke(
            approval={
                "draft_id": payload["draft_id"],
                "draft_version": payload["draft_version"],
                "action": action,
                **extra,
            }
        )

    def tickets():
        return client.get("/support-agent/tickets", params={"user_id": user}).json()["tickets"]

    def independent_retrieval():
        for _ in range(20):
            answer = client.post(
                "/support-agent/invoke",
                json={"user_id": user, "thread_id": uuid4().hex, "message": "CI_KNOWLEDGE"},
            )
            if answer.status_code == 200 and "MFA" in answer.json()["content"]:
                return
            time.sleep(1)
        raise AssertionError("Retrieval did not recover")

    def sql(statement, database="enterprise_support"):
        return compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "support_demo",
            "-d",
            database,
            "-Atc",
            statement,
        ).strip()

    if compose("ps", "-aq").strip():
        client.close()
        raise ValueError("Project already exists; refuse destructive test")
    try:
        compose("up", "-d", "--wait", "--wait-timeout", "180")
        wait_ready()
        assert compose("exec", "-T", "agent_service", "id", "-u").strip() == "10001"
        assert compose("exec", "-T", "streamlit_app", "id", "-u").strip() == "10001"
        compose(
            "exec",
            "-T",
            "agent_service",
            "python",
            "-c",
            "from pathlib import Path; p=Path('/app/data/permission-probe'); p.write_text('ok'); p.unlink()",
        )
        compose("exec", "-T", "postgres", "createdb", "-U", "support_demo", "phase9_unmigrated")
        refused = subprocess.run(
            [
                *base,
                "run",
                "--rm",
                "--no-deps",
                "-e",
                "POSTGRES_DB=phase9_unmigrated",
                "agent_service",
            ],
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert refused.returncode != 0
        record("nonroot_permissions_unmigrated_schema_refused")
        assert client.get("/info", headers={"Authorization": ""}).status_code == 401
        assert [a["key"] for a in client.get("/info").json()["agents"]] == ["support-agent"]
        compose("run", "--rm", "migrate")
        record("startup_auth_migration")
        for _ in range(2):
            answer = invoke(message="CI_KNOWLEDGE")
            assert "MFA" in answer["content"] and "[1]" in answer["content"]
        answer = invoke(message="CI_STATUS")
        assert "模拟" in answer["content"]
        response = client.post(
            "/support-agent/stream",
            json={
                "user_id": user,
                "thread_id": uuid4().hex,
                "message": "CI_KNOWLEDGE",
                "stream_tokens": False,
            },
        )
        assert '"type": "message"' in response.text and "[DONE]" in response.text
        response = client.post(
            "/support-agent/stream",
            json={
                "user_id": user,
                "thread_id": uuid4().hex,
                "message": "CI_TIMEOUT",
                "stream_tokens": False,
            },
        )
        assert '"type": "error"' in response.text and "[DONE]" in response.text
        response = client.post(
            "/support-agent/invoke",
            json={"user_id": user, "thread_id": uuid4().hex, "message": "CI_ERROR"},
        )
        assert response.status_code == 200
        assert "暂时无法可靠识别请求" in response.json()["content"]
        with client.stream(
            "POST",
            "/support-agent/stream",
            json={"user_id": user, "thread_id": uuid4().hex, "message": "CI_TIMEOUT"},
        ) as response:
            assert response.status_code == 200
            time.sleep(0.25)
        record("retrieval_cache_tools_sse")
        invoke(message="CI_DRAFT:" + json.dumps(draft))
        payload = pending()
        assert payload["draft"] == draft and not tickets()
        denied = client.get(
            "/support-agent/approval", params={"user_id": "other", "thread_id": thread}
        )
        assert denied.status_code in {403, 404}
        approve(payload, "cancel")
        assert not tickets()
        invoke(message="CI_DRAFT:" + json.dumps(draft))
        old = pending()
        edited = {**draft, "description": "已重启但无效，希望人工检查"}
        approve(old, "edit", draft=edited)
        payload = pending()
        assert payload["draft_version"] == old["draft_version"] + 1 and not tickets()
        approve(payload, "approve")
        approve(payload, "approve")
        assert len(tickets()) == 1 and tickets()[0]["draft"] == edited
        prefs = {"language": "en", "detail": "detailed"}
        assert (
            client.put(
                "/support-agent/preferences", params={"user_id": user}, json=prefs
            ).status_code
            == 200
        )
        thread = uuid4().hex
        invoke(message="CI_DRAFT:" + json.dumps(draft))
        payload = pending()
        record("approval_cancel_edit_idempotency_ownership")
        for service in ["agent_service", "postgres", "redis", "qdrant"]:
            compose("restart", service)
            wait_ready()
            assert pending()["draft_id"] == payload["draft_id"]
            assert len(tickets()) == 1
            assert (
                client.get("/support-agent/preferences", params={"user_id": user}).json()[
                    "preferences"
                ]
                == prefs
            )
            record("restart_" + service)
        independent_retrieval()
        record("qdrant_persisted_index_query")
        compose("stop", "redis")
        assert client.get("/health/ready").status_code == 200
        denied = client.post(
            "/support-agent/invoke",
            json={"message": "CI_KNOWLEDGE", "user_id": user, "thread_id": uuid4().hex},
        )
        assert denied.status_code == 503 and pending()
        approve(payload, "approve")
        assert len(tickets()) == 2
        compose("start", "redis")
        compose("stop", "qdrant")
        capabilities = client.get("/health/capabilities").json()
        assert capabilities["retrieval"]["status"] == "unavailable"
        assert client.get("/health/ready").status_code == 200
        compose("start", "qdrant")
        compose("stop", "postgres")
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        compose("start", "postgres")
        wait_ready()
        independent_retrieval()
        record("dependency_outage_and_recovery")
        compose(
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "support_demo",
            "-d",
            "enterprise_support",
            "-Fc",
            "-f",
            "/tmp/phase9.dump",
        )
        compose("exec", "-T", "postgres", "createdb", "-U", "support_demo", "phase9_restore")
        compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "support_demo",
            "-d",
            "phase9_restore",
            "--exit-on-error",
            "/tmp/phase9.dump",
        )
        tables = sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        counts = {}
        for table in tables:
            if not table.replace("_", "").isalnum():
                raise ValueError("Unexpected table name")
            query = f'SELECT count(*), md5(string_agg(row_to_json(t)::text, chr(10) ORDER BY row_to_json(t)::text)) FROM "{table}" t'
            original, restored = sql(query), sql(query, "phase9_restore")
            assert original == restored
            counts[table] = original
        record("backup_restore_new_database", table_checksums=counts)
        assert (
            client.delete("/support-agent/preferences", params={"user_id": user}).status_code == 200
        )
        assert (
            client.get("/support-agent/preferences", params={"user_id": user}).json()["status"]
            == "default"
        )
        output = compose("exec", "-T", "streamlit_app", "python", "scripts/phase9_ui_smoke.py")
        assert '"passed"' in output
        record("streamlit_script_service_interaction")
    except BaseException as exc:
        record("failure", error_type=type(exc).__name__)
        raise
    finally:
        logs = compose("logs", "--no-color", "agent_service", check=False)
        # Only our JSON metadata events enter the public artifact.
        summaries = []
        for line in logs.splitlines():
            try:
                value = json.loads(line[line.index("{") :])
                if value.get("event") == "support_request":
                    summaries.append(value)
            except (ValueError, json.JSONDecodeError):
                pass
        (args.output / "request-summaries.json").write_text(
            json.dumps(summaries, indent=2), encoding="utf-8"
        )
        if not args.keep:
            compose("down", "--volumes", "--remove-orphans", check=False)
        client.close()

    assert summaries and any(row.get("cache_status") == "hit" for row in summaries)
    assert any(row.get("outcome") == "request_timeout" for row in summaries)
    assert any(row.get("outcome") in {"disconnected", "cancelled"} for row in summaries)
    assert any(row.get("outcome") == "router_unavailable" for row in summaries)
    for marker in [
        "CI_SYNTHETIC_SENSITIVE_ERROR",
        "ci-only-shared-key",
        "ci-only-database-password",
        user,
        draft["description"],
    ]:
        assert marker not in logs
    record("runtime_logs_cache_error_timeout_disconnect_filtered", requests=len(summaries))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--ui-port", type=int, default=18501)
    parser.add_argument("--keep", action="store_true")
    run(parser.parse_args())
