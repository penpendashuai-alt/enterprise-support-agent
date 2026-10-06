"""Isolated Compose lifecycle shared by the Phase 9 closeout checks."""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx


class Stack:
    def __init__(self, args, *, environment=None, overrides=()):
        if not re.fullmatch(r"esa-p9-test-[a-z0-9-]+", args.project) or args.output.exists():
            raise ValueError("Fresh esa-p9-test-* project and output required")
        self.output = args.output
        self.output.mkdir(parents=True)
        self.project = args.project
        self.env = {
            **os.environ,
            "SERVICE_PORT": str(args.port),
            "STREAMLIT_PORT": str(args.port + 1),
            **(environment or {}),
        }
        self.base = ["docker", "compose", "--env-file", "docker/ci.env", "-p", args.project]
        for file in ["compose.yaml", "compose.local.yaml", "compose.ci.yaml", *overrides]:
            self.base.extend(["-f", str(file)])
        self.records = []
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{args.port}",
            headers={"Authorization": "Bearer ci-only-shared-key"},
            timeout=20,
            trust_env=False,
        )
        self.private_log = Path(".cache") / f"{args.project}-diagnostic.log"
        self.private_log.parent.mkdir(exist_ok=True)

    def command(self, *args, check=True, input=None):
        result = subprocess.run(
            args,
            env=self.env,
            input=input,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=600,
        )
        if result.returncode:
            with self.private_log.open("a", encoding="utf-8") as handle:
                handle.write(result.stdout + result.stderr)
            if check:
                raise RuntimeError("Command failed; private diagnostic retained")
        return result.stdout

    def compose(self, *args, **kwargs):
        return self.command(*self.base, *args, **kwargs)

    def record(self, name, **data):
        self.records.append({"check": name, "at": datetime.now(UTC).isoformat(), **data})
        self.save("checks.json", self.records)
        print(name, flush=True)

    def save(self, name, data):
        (self.output / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def invoke(self, user, thread, **data):
        return self.client.post(
            "/support-agent/invoke", json={"user_id": user, "thread_id": thread, **data}
        )

    def logs(self):
        raw = self.compose("logs", "--no-color", "agent_service", check=False)
        records = []
        for line in raw.splitlines():
            try:
                record = json.loads(line[line.index("{") :])
                if record.get("event") == "support_request":
                    records.append(record)
            except ValueError:
                pass
        return raw, records

    def __enter__(self):
        existing_volumes = self.command(
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
        )
        if self.compose("ps", "-aq").strip() or existing_volumes.strip():
            self.client.close()
            raise ValueError("Refuse to use an existing project")
        try:
            self.compose("up", "-d", "--wait", "--wait-timeout", "180")
        except BaseException:
            self.__exit__(RuntimeError, None, None)
            raise
        return self

    def __exit__(self, kind, value, tb):
        try:
            _, records = self.logs()
            self.save("request-summaries.json", records)
            if kind:
                self.record("failure", error_type=kind.__name__)
        finally:
            self.compose("down", "--volumes", "--remove-orphans", check=False)
            self.client.close()
