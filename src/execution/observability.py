"""Metadata-only logging and filtering at the final Langfuse export boundary."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Status

from core import settings

_salt = secrets.token_bytes(32)
_client = None
_exporter = None
_provider = None
FIELDS = {
    "request_id",
    "run_id",
    "category",
    "outcome",
    "status",
    "total_seconds",
    "timings",
    "code_version",
    "prompt_sha256",
    "config_sha256",
    "session_hash",
    "draft_hash",
    "draft_version",
    "intent",
    "retrieval_mode",
    "index_hash",
    "candidates",
    "evidence",
    "cache_status",
    "embedding_tokens",
    "embedding_requests",
    "model_calls",
    "trace_id",
    "approval_action",
    "nodes",
    "tools",
}


def pseudonym(value):
    key = settings.TELEMETRY_HASH_KEY or settings.AUTH_SECRET
    salt = key.get_secret_value().encode() if key else _salt
    return hmac.new(salt, str(value).encode(), hashlib.sha256).hexdigest()[:32]


@lru_cache
def versions():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in ["agents/support_agent.py", "agents/ticket_flow.py", "rag/answers.py"]:
        digest.update((root / name).read_bytes().replace(b"\r\n", b"\n"))
    config = {
        k: getattr(settings, k)
        for k in [
            "ENABLED_AGENTS",
            "ADMISSION_ENABLED",
            "RAG_CACHE_ENABLED",
            "EXECUTION_LIMIT",
            "MODEL_LIMIT",
        ]
    }
    return {
        "code_version": settings.CODE_VERSION,
        "prompt_sha256": digest.hexdigest(),
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
    }


class MetadataFormatter(logging.Formatter):
    def format(self, record):
        value = {"level": record.levelname, "event": "runtime_event"}
        if hasattr(record, "support_record"):
            value.update(
                event="support_request",
                **{k: v for k, v in record.support_record.items() if k in FIELDS},
            )
        else:
            # SDK errors and access logs may contain URLs, queries, input or credentials.
            value["component"] = record.name.split(".")[0]
            value["reason"] = (
                "runtime_error" if record.levelno >= logging.ERROR else "runtime_notice"
            )
        return json.dumps(value, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(MetadataFormatter())
    logging.basicConfig(handlers=[handler], level=settings.LOG_LEVEL.to_logging_level(), force=True)
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


class MetadataExporter(SpanExporter):
    def __init__(self, delegate):
        self.delegate = delegate
        self.last_result = "unverified"

    def export(self, spans):
        try:
            clean = []
            for span in spans:
                attrs = {}
                kind = (span.attributes or {}).get("langfuse.observation.type")
                if kind in {
                    "span",
                    "generation",
                    "agent",
                    "chain",
                    "tool",
                    "retriever",
                    "embedding",
                }:
                    attrs["langfuse.observation.type"] = kind
                raw_usage = (span.attributes or {}).get("langfuse.observation.usage_details")
                if isinstance(raw_usage, str):
                    try:
                        usage = json.loads(raw_usage)
                        if isinstance(usage, dict):
                            safe_usage = {
                                k: v
                                for k, v in usage.items()
                                if k in {"input", "output", "total"}
                                and isinstance(v, int)
                                and not isinstance(v, bool)
                                and v >= 0
                            }
                            attrs["langfuse.observation.usage_details"] = json.dumps(safe_usage)
                    except ValueError:
                        pass
                for key, value in (span.attributes or {}).items():
                    if (
                        key.startswith("langfuse.observation.metadata.")
                        and key.rsplit(".", 1)[-1] in FIELDS
                    ):
                        attrs[key] = value
                clean.append(
                    ReadableSpan(
                        name="support-observation",
                        context=span.context,
                        parent=span.parent,
                        resource=Resource({"service.name": "enterprise-support-agent"}),
                        attributes=attrs,
                        events=(),
                        links=(),
                        kind=span.kind,
                        status=Status(span.status.status_code),
                        start_time=span.start_time,
                        end_time=span.end_time,
                        instrumentation_scope=InstrumentationScope("langfuse-sdk"),
                    )
                )
            result = self.delegate.export(clean)
            self.last_result = "ok" if result == SpanExportResult.SUCCESS else "failed"
            return result
        except Exception:
            self.last_result = "failed"
            return SpanExportResult.FAILURE

    def shutdown(self):
        self.delegate.shutdown()


def initialize_tracing():
    global _client, _exporter, _provider
    if not settings.LANGFUSE_TRACING:
        return
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        raise ValueError("Tracing requires explicit receiver credentials")
    from base64 import b64encode

    from langfuse import Langfuse
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider

    os.environ["OTEL_BSP_MAX_QUEUE_SIZE"] = "256"
    credentials = (
        settings.LANGFUSE_PUBLIC_KEY.get_secret_value()
        + ":"
        + settings.LANGFUSE_SECRET_KEY.get_secret_value()
    )
    _exporter = MetadataExporter(
        OTLPSpanExporter(
            endpoint=str(settings.LANGFUSE_HOST).rstrip("/") + "/api/public/otel/v1/traces",
            headers={
                "Authorization": "Basic " + b64encode(credentials.encode()).decode(),
                "x-langfuse-ingestion-version": "4",
            },
            timeout=2,
        )
    )
    _provider = TracerProvider(shutdown_on_exit=False)
    _client = Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
        secret_key=settings.LANGFUSE_SECRET_KEY.get_secret_value(),
        base_url=str(settings.LANGFUSE_HOST),
        timeout=2,
        flush_at=32,
        flush_interval=2,
        mask=lambda **kwargs: "[omitted]",
        span_exporter=_exporter,
        tracer_provider=_provider,
        should_export_span=lambda span: (
            span.instrumentation_scope is not None
            and span.instrumentation_scope.name in {"enterprise-support", "langfuse-sdk"}
        ),
    )


@contextmanager
def request_observation(trace):
    if _client is None or _provider is None:
        yield
        return
    with _provider.get_tracer("enterprise-support").start_as_current_span(
        "support-request"
    ) as span:
        trace.metadata["trace_id"] = format(span.get_span_context().trace_id, "032x")
        try:
            yield
        finally:
            span.set_attribute("langfuse.observation.type", "span")
            for key, value in trace.record().items():
                if key in FIELDS and value is not None:
                    span.set_attribute("langfuse.observation.metadata." + key, json.dumps(value))


async def close_tracing():
    global _client
    if _client is None:
        return
    client, _client = _client, None
    done = threading.Event()

    def shutdown():
        try:
            client.shutdown()
        finally:
            done.set()

    threading.Thread(target=shutdown, daemon=True).start()
    for _ in range(30):
        if done.is_set():
            break
        await asyncio.sleep(0.1)


def tracing_status():
    if not settings.LANGFUSE_TRACING:
        return {"status": "disabled", "reason": "not_enabled"}
    result = _exporter.last_result if _exporter else "unverified"
    return {
        "status": "degraded" if result == "failed" else "configured",
        "reason": "export_" + result,
    }


def callbacks():
    items: list[BaseCallbackHandler] = [BusinessCallback()]
    if _client and settings.LANGFUSE_PUBLIC_KEY:
        from langfuse.langchain import CallbackHandler

        items.append(CallbackHandler(public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value()))
    return items


class BusinessCallback(BaseCallbackHandler):
    def on_chat_model_start(self, serialized, messages, **kwargs):
        from execution.telemetry import current

        if trace := current.get():
            params = kwargs.get("invocation_params") or {}
            candidate = params.get("model_name") or params.get("model")
            allowed = {str(m) for m in settings.AVAILABLE_MODELS} | {settings.COMPATIBLE_MODEL}
            model = candidate if candidate and candidate in allowed else "unknown"
            if params.get("_type") == "fake-list-chat-model":
                model = "deterministic-fixture"
            trace.model_calls[str(kwargs.get("run_id"))] = {"usage_known": False, "model": model}

    def on_llm_end(self, response, **kwargs):
        from execution.telemetry import current

        if trace := current.get():
            usage = (
                getattr(response.generations[0][0].message, "usage_metadata", None)
                if response.generations and response.generations[0]
                else None
            )
            trace.model_calls[str(kwargs.get("run_id"))] = {
                "usage_known": usage is not None,
                "model": trace.model_calls.get(str(kwargs.get("run_id")), {}).get(
                    "model", "unknown"
                ),
                "usage": {
                    k: v
                    for k, v in (usage or {}).items()
                    if k in {"input_tokens", "output_tokens", "total_tokens"} and isinstance(v, int)
                },
            }

    def on_chain_start(self, serialized, inputs, **kwargs):
        from execution.telemetry import current

        if trace := current.get():
            node = (kwargs.get("metadata") or {}).get("langgraph_node")
            if node in {
                "router",
                "clarify",
                "retrieve",
                "handler",
                "tools",
                "draft",
                "approval",
                "create",
            }:
                trace.metadata.setdefault("nodes", []).append(node)

    def on_tool_start(self, serialized, input_str, **kwargs):
        from execution.telemetry import current

        if trace := current.get():
            name = serialized.get("name")
            if name in {
                "query_service_status",
                "query_device_info",
                "query_existing_ticket",
                "search_known_issue",
            }:
                trace.metadata.setdefault("tools", []).append(name)
