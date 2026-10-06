"""Exercise real Service traces, with local OTLP evidence or an explicitly designated remote project."""

import argparse
import gzip
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from phase9_closeout_support import Stack

MARKER = "CI_SYNTHETIC_PRIVATE_BODY"
FIELDS = "core,basic,io,metadata,model,usage,time,prompt,metrics,trace_context"
META_FIELDS = "request_id,run_id,trace_id,session_hash,draft_hash,draft_version,approval_action,outcome,status,cache_status,embedding_tokens,embedding_requests,model_calls,nodes,tools,timings,evidence"


class Receiver:
    def __init__(self):
        self.payloads = []
        payloads = self.payloads

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 4 * 1024 * 1024:
                    self.send_error(413)
                    return
                data = self.rfile.read(length)
                if self.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                payloads.append(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def rows(self):
        rows = []
        for data in list(self.payloads):
            batch = ExportTraceServiceRequest.FromString(data)
            for resource in batch.resource_spans:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        meta = {
                            attr.key.removeprefix(
                                "langfuse.observation.metadata."
                            ): attr.value.string_value
                            for attr in span.attributes
                            if attr.key.startswith("langfuse.observation.metadata.")
                        }
                        rows.append(
                            {
                                "id": span.span_id.hex(),
                                "traceId": span.trace_id.hex(),
                                "parentObservationId": span.parent_span_id.hex() or None,
                                "metadata": meta,
                                "startTimeNs": span.start_time_unix_nano,
                                "endTimeNs": span.end_time_unix_nano,
                            }
                        )
        return rows

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def metadata(row):
    data = row.get("metadata") or {}
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        result[key] = value
    return result


def scan(rows, forbidden):
    serialized = json.dumps(rows, ensure_ascii=False)
    assert all(marker not in serialized for marker in forbidden if marker)


def query_remote(client, trace_ids, started, ended):
    rows = []
    for trace_id in trace_ids:
        cursor = None
        for _ in range(10):
            params = {
                "traceId": trace_id,
                "fromStartTime": started,
                "toStartTime": ended,
                "fields": FIELDS,
                "expandMetadata": META_FIELDS,
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            response = client.get("/api/public/v2/observations", params=params)
            response.raise_for_status()
            body = response.json()
            rows.extend(body["data"])
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                break
        else:
            raise RuntimeError("Observation pagination limit exceeded")
    return rows


def validate(rows, calls, forbidden):
    scan(rows, forbidden)
    ids = {row["id"] for row in rows}
    assert all(
        not row.get("parentObservationId") or row["parentObservationId"] in ids for row in rows
    )
    roots = {}
    for category, call in calls.items():
        matches = [row for row in rows if metadata(row).get("request_id") == call["request_id"]]
        if len(matches) != 1:
            raise AssertionError("Request root not yet uniquely queryable: " + category)
        root = matches[0]
        value = metadata(root)
        assert root["traceId"] == call["trace_id"] == value["trace_id"]
        if call.get("run_id"):
            assert value["run_id"] == call["run_id"]
        roots[category] = value
    assert roots["question"]["cache_status"] == "miss"
    assert roots["cached_question"]["cache_status"] == "hit"
    assert roots["cached_question"]["embedding_requests"] == 0
    for key in ["session_hash", "draft_hash", "draft_version"]:
        assert roots["draft"][key] == roots["approval"][key]
    assert roots["draft"]["request_id"] != roots["approval"]["request_id"]
    assert roots["approval"]["model_calls"] == []
    assert roots["dependency_failure"]["outcome"] == "rate_dependency_unavailable"
    assert roots["router_failure"]["outcome"] == "router_unavailable"
    assert roots["router_failure"]["status"] == 200
    assert all(
        call["model"] == "deterministic-fixture" and not call["usage_known"]
        for key in ["question", "cached_question", "tool"]
        for call in roots[key]["model_calls"]
    )
    assert "query_service_status" in roots["tool"]["tools"]
    return roots


def run(args):
    receiver, remote = None, None
    if args.receiver == "remote":
        config = dotenv_values(args.credentials)
        keys = ["LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
        if any(not config.get(key) for key in keys):
            raise ValueError("Configure the designated remote project in the private env file")
        environment = {key: config[key] for key in keys}
        if not environment["LANGFUSE_HOST"].startswith("https://"):
            raise ValueError("Remote receiver must use HTTPS")
        remote = httpx.Client(
            base_url=environment["LANGFUSE_HOST"].rstrip("/"),
            auth=(environment["LANGFUSE_PUBLIC_KEY"], environment["LANGFUSE_SECRET_KEY"]),
            timeout=15,
        )
        remote.get("/api/public/projects").raise_for_status()
    else:
        receiver = Receiver()
        environment = {
            "LANGFUSE_HOST": f"http://host.docker.internal:{receiver.server.server_port}",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-synthetic-local",
            "LANGFUSE_SECRET_KEY": "sk-lf-synthetic-local",
        }
    user, thread = "synthetic-trace-user-" + uuid4().hex, uuid4().hex
    forbidden = [
        MARKER,
        user,
        thread,
        "CI_SYNTHETIC_SENSITIVE_ERROR",
        "ci-only-shared-key",
        "ci-only-database-password",
        *environment.values(),
        "postgresql://",
        "redis://",
        "qdrant:6333",
    ]
    started = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    calls = {}
    try:
        with Stack(args, environment=environment, overrides=["compose.tracing.yaml"]) as stack:

            def call(category, **data):
                response = stack.invoke(
                    user, thread if category in {"draft", "approval"} else uuid4().hex, **data
                )
                expected = 503 if category == "dependency_failure" else 200
                assert response.status_code == expected
                calls[category] = {
                    "request_id": response.headers["x-request-id"],
                    "trace_id": response.headers["x-trace-id"],
                    "run_id": response.headers.get("x-run-id"),
                    "http_status": response.status_code,
                }
                return response.json()

            assert "MFA" in call("question", message="CI_KNOWLEDGE")["content"]
            assert "MFA" in call("cached_question", message="CI_KNOWLEDGE")["content"]
            call("tool", message="CI_STATUS")
            draft = {
                "title": "VPN故障",
                "description": MARKER,
                "service_name": "VPN",
                "impact": "本人",
                "priority": "P3",
            }
            pending = call("draft", message="CI_DRAFT:" + json.dumps(draft))["custom_data"]
            time.sleep(1)
            call(
                "approval",
                approval={
                    "action": "approve",
                    "draft_id": pending["draft_id"],
                    "draft_version": pending["draft_version"],
                },
            )
            assert (
                len(
                    stack.client.get("/support-agent/tickets", params={"user_id": user}).json()[
                        "tickets"
                    ]
                )
                == 1
            )
            call("router_failure", message="CI_ERROR")
            stack.compose("stop", "redis")
            call("dependency_failure", message="CI_KNOWLEDGE")
            stack.compose("start", "redis")
            stack.record(
                "real_service_business_scenarios",
                requests=calls,
                receiver=args.receiver,
                paid_calls=0,
            )
            ended = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            deadline = time.monotonic() + args.wait_seconds
            while True:
                rows = (
                    query_remote(remote, [c["trace_id"] for c in calls.values()], started, ended)
                    if remote
                    else receiver.rows()
                )
                try:
                    roots = validate(rows, calls, forbidden)
                    break
                except AssertionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(5)
            if receiver:
                assert all(
                    marker.encode() not in data
                    for marker in forbidden
                    for data in receiver.payloads
                )
            raw, records = stack.logs()
            scan(records, forbidden)
            assert all(marker not in raw for marker in forbidden)
            stack.save(
                "trace-evidence.json",
                {
                    "receiver": args.receiver,
                    "remote_receipt": "verified_via_v2_observations" if remote else "unverified",
                    "observation_count": len(rows),
                    "parent_links_valid": True,
                    "sensitive_scan_passed": True,
                    "queried_field_groups": FIELDS if remote else "complete_OTLP_payload",
                    "roots": roots,
                    "observations": [
                        {key: row.get(key) for key in ["id", "traceId", "parentObservationId"]}
                        for row in rows
                    ],
                },
            )
            stack.record(
                "received_queried_and_filtered", observations=len(rows), receiver=args.receiver
            )
            stack.env["LANGFUSE_HOST"] = "http://127.0.0.1:1"
            stack.compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "agent_service")
            response = stack.invoke(user, uuid4().hex, message="CI_KNOWLEDGE")
            assert response.status_code == 200 and "MFA" in response.json()["content"]
            assert stack.client.get("/health/ready").status_code == 200
            before = time.monotonic()
            stack.compose("stop", "-t", "10", "agent_service")
            elapsed = time.monotonic() - before
            container = stack.compose("ps", "-aq", "agent_service").strip()
            exit_code = stack.command(
                "docker", "inspect", container, "--format", "{{.State.ExitCode}}"
            ).strip()
            assert elapsed < 10 and exit_code == "0"
            stack.record(
                "receiver_unreachable_business_and_shutdown",
                http_status=200,
                shutdown_seconds=elapsed,
                exit_code=0,
            )
    finally:
        if receiver:
            receiver.close()
        if remote:
            remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument("--receiver", choices=["local", "remote"], default="local")
    parser.add_argument("--credentials", type=Path, default=Path(".env"))
    parser.add_argument("--wait-seconds", type=int, default=90)
    run(parser.parse_args())
