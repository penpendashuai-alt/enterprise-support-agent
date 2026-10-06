"""Verify separate cache and admission OOM paths using real disposable infrastructure."""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from phase9_closeout_support import Stack


def run(args):
    user, thread = "synthetic-memory-user", uuid4().hex
    with Stack(
        args, environment={"REDIS_MAXMEMORY": "4mb", "REDIS_CONTAINER_MEMORY": "128m"}
    ) as stack:
        container = stack.compose("ps", "-q", "redis").strip()

        def probe(mode):
            return json.loads(
                stack.compose(
                    "exec", "-T", "agent_service", "python", "scripts/phase9_redis_probe.py", mode
                )
            )

        config = json.loads(stack.compose("config", "--format", "json"))["services"]["redis"]
        runtime_config = stack.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--json",
            "CONFIG",
            "GET",
            "maxmemory",
            "maxmemory-policy",
            "save",
            "appendonly",
        )
        hard_limit = int(
            stack.command("docker", "inspect", container, "--format", "{{.HostConfig.Memory}}")
        )
        before = probe("inspect")
        assert before["memory"]["maxmemory"] == 4 * 1024 * 1024 < hard_limit
        assert before["memory"]["maxmemory_policy"] == "noeviction"
        stack.record(
            "effective_memory_limits",
            compose_command=config["command"],
            hard_limit_bytes=hard_limit,
            runtime_config=json.loads(runtime_config),
            before=before,
        )
        assert "MFA" in stack.invoke(user, thread, message="CI_KNOWLEDGE").json()["content"]
        draft = {
            "title": "VPN故障",
            "description": "合成内存测试，尚未重启",
            "service_name": "VPN",
            "impact": "本人",
            "priority": "P3",
        }
        response = stack.invoke(user, thread, message="CI_DRAFT:" + json.dumps(draft))
        response.raise_for_status()
        pending = response.json()["custom_data"]
        filled = probe("fill")
        assert filled["evicted_keys"] == before["evicted_keys"]
        assert filled["sentinel"]["tokens"] == "7"
        stack.record("noeviction_oom", **filled)
        cached = probe("cache")
        stack.record("computed_retrieval_survives_cache_set_failure", **cached)
        rejected = stack.invoke(user, uuid4().hex, message="CI_KNOWLEDGE")
        assert rejected.status_code == 503
        _, records = stack.logs()
        record = next(
            row for row in records if row["request_id"] == rejected.headers["x-request-id"]
        )
        assert not record.get("nodes") and not record["model_calls"]
        assert record["outcome"] == "rate_dependency_unavailable"
        assert stack.client.get("/health/ready").status_code == 200
        capabilities = stack.client.get("/health/capabilities").json()
        assert capabilities["cache"]["reason"] == "redis_memory_limit"
        assert capabilities["question"]["status"] == "unavailable"
        assert capabilities["approval_and_read"]["status"] == "degraded"
        stack.record(
            "admission_fails_closed_without_model_call",
            http_status=rejected.status_code,
            request_id=record["request_id"],
            capabilities=capabilities,
        )
        approval = {
            "action": "approve",
            "draft_id": pending["draft_id"],
            "draft_version": pending["draft_version"],
        }
        for _ in range(2):
            assert stack.invoke(user, thread, approval=approval).status_code == 200
        tickets = stack.client.get("/support-agent/tickets", params={"user_id": user})
        assert tickets.status_code == 200 and len(tickets.json()["tickets"]) == 1
        count = stack.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "support_demo",
            "-d",
            "enterprise_support",
            "-Atc",
            "SELECT count(*) FROM tickets",
        ).strip()
        assert count == "1"
        stats = probe("inspect")
        killed = stack.command(
            "docker", "inspect", container, "--format", "{{.State.OOMKilled}} {{.State.Running}}"
        ).strip()
        assert killed == "false true" and stats["evicted_keys"] == before["evicted_keys"]
        stack.record(
            "approval_read_replay_under_oom",
            tickets=1,
            container_state=killed,
            memory=stats["memory"],
        )
        released = probe("release")
        for _ in range(2):
            answer = stack.invoke(user, uuid4().hex, message="CI_KNOWLEDGE")
            assert answer.status_code == 200 and "MFA" in answer.json()["content"]
        time.sleep(5.1)
        capabilities = stack.client.get("/health/capabilities").json()
        assert capabilities["cache"]["status"] == capabilities["question"]["status"] == "ok"
        _, records = stack.logs()
        assert records[-1]["cache_status"] == "hit"
        stack.record("release_and_recovery", **released, capabilities=capabilities, paid_calls=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18090)
    run(parser.parse_args())
