import json
import logging
from time import perf_counter

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from execution.observability import BusinessCallback, MetadataExporter, MetadataFormatter
from execution.telemetry import RequestTrace, current

MARKER = "SYNTHETIC_PRIVATE_MESSAGE_KEY_PASSWORD"


class Sink(SpanExporter):
    def export(self, spans):
        self.spans = spans
        return SpanExportResult.SUCCESS


def test_export_boundary_discards_content_and_exception():
    sink = Sink()
    exporter = MetadataExporter(sink)
    span = ReadableSpan(
        name=MARKER,
        resource=Resource({"secret": MARKER}),
        attributes={
            "langfuse.observation.input": MARKER,
            "langfuse.observation.output": MARKER,
            "langfuse.observation.metadata.user_id": MARKER,
            "langfuse.observation.usage_details": json.dumps({"input": 3, "unexpected": MARKER}),
            "tool.args": MARKER,
            "langfuse.observation.type": "generation",
            "langfuse.observation.metadata.request_id": "server-id",
        },
        status=Status(StatusCode.ERROR, MARKER),
    )
    assert exporter.export([span]) == SpanExportResult.SUCCESS
    value = sink.spans[0]
    assert MARKER not in value.to_json()
    assert value.attributes["langfuse.observation.metadata.request_id"] == "server-id"


def test_export_filter_failure_drops_batch():
    sink = Sink()
    exporter = MetadataExporter(sink)
    assert exporter.export([object()]) == SpanExportResult.FAILURE
    assert not hasattr(sink, "spans")


def test_json_logs_drop_raw_sdk_exception_and_url():
    formatter = MetadataFormatter()
    record = logging.LogRecord(
        "httpx",
        logging.ERROR,
        __file__,
        1,
        "https://host?user=" + MARKER,
        (),
        (RuntimeError, RuntimeError(MARKER), None),
    )
    assert MARKER not in formatter.format(record)
    record.support_record = {"request_id": "server-id", "status": 500, "body": MARKER}
    assert json.loads(formatter.format(record))["request_id"] == "server-id"
    assert MARKER not in formatter.format(record)


def test_model_usage_is_unknown_until_returned_and_not_double_counted():
    trace = RequestTrace(deadline=perf_counter() + 1, category="model")
    token = current.set(trace)
    try:
        callback = BusinessCallback()
        callback.on_chat_model_start({}, [], run_id="call")
        assert trace.record()["model_calls"][0]["usage_known"] is False
        response = LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content=MARKER,
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        )
                    )
                ]
            ]
        )
        callback.on_llm_end(response, run_id="call")
        assert len(trace.record()["model_calls"]) == 1
        assert trace.record()["model_calls"][0]["usage"]["total_tokens"] == 5
        assert MARKER not in json.dumps(trace.record())
    finally:
        current.reset(token)
