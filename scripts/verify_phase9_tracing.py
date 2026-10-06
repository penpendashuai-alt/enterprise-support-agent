"""Exercise the locked Langfuse SDK and real OTLP HTTP serialization locally."""

import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.tools import tool
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic import SecretStr

from core import settings
from execution import observability as obs
from execution.telemetry import RequestTrace, current

MARKER = "SYNTHETIC_SENSITIVE_INPUT_OUTPUT_TOOL_EXCEPTION"


async def run(output):
    if output.exists():
        raise ValueError("Preserve prior trace verification")
    output.mkdir(parents=True)
    batches = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            batches.append(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    settings.LANGFUSE_TRACING = True
    settings.LANGFUSE_HOST = f"http://127.0.0.1:{receiver.server_port}"
    settings.LANGFUSE_PUBLIC_KEY = SecretStr("pk-lf-local-contract")
    settings.LANGFUSE_SECRET_KEY = SecretStr("sk-lf-local-contract")
    obs.configure_logging()
    obs.initialize_tracing()
    model = FakeListChatModel(responses=[MARKER])

    @tool
    def synthetic_tool(value: str) -> str:
        """Return a synthetic marker for privacy validation."""
        return MARKER + value

    records = []
    try:
        for category in ["normal", "cache", "approval", "dependency_failure"]:
            trace = RequestTrace(
                deadline=perf_counter() + 5,
                category="approval" if category == "approval" else "model",
            )
            trace.metadata["session_hash"] = obs.pseudonym("synthetic-session")
            trace.metadata["cache_status"] = "hit" if category == "cache" else "miss"
            token = current.set(trace)
            try:
                with obs.request_observation(trace):
                    await model.ainvoke(MARKER, {"callbacks": obs.callbacks()})
                    await synthetic_tool.ainvoke({"value": MARKER}, {"callbacks": obs.callbacks()})
                    if category == "dependency_failure":
                        trace.outcome = "dependency_unavailable"
                        trace.status = 503
                records.append(trace.record())
            finally:
                current.reset(token)
        await asyncio.to_thread(obs._client.flush)
        decoded = [ExportTraceServiceRequest.FromString(data) for data in batches]
        assert decoded and all(MARKER.encode() not in data for data in batches)
        spans = [
            span
            for batch in decoded
            for resource in batch.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        ids = {span.span_id.hex() for span in spans}
        assert all(not span.parent_span_id or span.parent_span_id.hex() in ids for span in spans)
        assert len({span.trace_id.hex() for span in spans}) == 4
        summary = {
            "status": "passed",
            "transport": "real local OTLP HTTP; locked Langfuse SDK",
            "remote_langfuse_receipt": "unverified",
            "synthetic_trace_categories": 4,
            "spans": len(spans),
            "parent_links_valid": True,
            "sensitive_marker_absent": True,
            "paid_calls": 0,
            "records": records,
        }
        receiver.shutdown()
        receiver.server_close()
        started = perf_counter()
        for _ in range(600):
            with obs._provider.get_tracer("enterprise-support").start_as_current_span(
                "queued-fixture"
            ):
                pass
        summary["enqueue_600_seconds"] = perf_counter() - started
        summary["queue_max_spans"] = 256
        started = perf_counter()
        await obs.close_tracing()
        summary["unreachable_shutdown_seconds"] = perf_counter() - started
        summary["unreachable_cleanup_bounded"] = summary["unreachable_shutdown_seconds"] < 4
        assert summary["unreachable_cleanup_bounded"]
        (output / "report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in summary.items() if k != "records"}))
    finally:
        start = perf_counter()
        await obs.close_tracing()
        receiver.server_close()
        print(json.dumps({"shutdown_seconds": perf_counter() - start}))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
