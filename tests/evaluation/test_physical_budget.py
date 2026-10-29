import asyncio
import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from resume_support.budget import AttemptLedger, BudgetStop, MeteredTransport  # noqa: E402


def contract(concurrent=False):
    return {
        "total_cny": 1,
        "stages": {"pilot": 1},
        "allow_concurrent": concurrent,
        "providers": {
            "chat": {
                "host": "example.test",
                "path": "/v1/chat/completions",
                "model": "test-model",
                "max_attempts": 20,
                "max_output_tokens": 1800,
                "input_per_million_cny": 9,
                "output_per_million_cny": 27,
            }
        },
    }


@pytest.mark.asyncio
async def test_sdk_reads_gzip_response_after_metering(tmp_path):
    payload = {
        "id": "test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }

    async def reply(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(json.dumps(payload).encode()),
        )

    ledger = AttemptLedger(tmp_path / "budget.db", contract())
    transport = MeteredTransport(ledger, "pilot", "chat", lambda: {}, httpx.MockTransport(reply))
    async with AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        result = await client.chat.completions.create(
            model="test-model", messages=[{"role": "user", "content": "hi"}], max_tokens=50
        )
    assert result.choices[0].message.content == "ok"
    assert len(ledger.events()) == 1


def test_cross_process_style_reservation_is_atomic(tmp_path):
    spec = contract(True)
    path = tmp_path / "budget.db"
    AttemptLedger(path, spec)

    def attempt(_):
        try:
            return AttemptLedger(path, spec).reserve("pilot", "chat", 0.2, {})
        except BudgetStop:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(attempt, range(20)))
    assert sum(x is not None for x in accepted) == 5
    ledger = AttemptLedger(path, spec)
    assert sum(e["reserved_cny"] for e in ledger.events()) == pytest.approx(1)
    ledger.finish(next(x for x in accepted if x), error_type="TimeoutError")
    with pytest.raises(BudgetStop, match="Unknown usage"):
        ledger.reserve("pilot", "chat", 0.01, {})


def test_restart_cannot_reset_pending_or_change_contract(tmp_path):
    path = tmp_path / "budget.db"
    ledger = AttemptLedger(path, contract())
    ledger.reserve("pilot", "chat", 0.2, {})
    with pytest.raises(BudgetStop, match="Pending attempt"):
        AttemptLedger(path, contract()).reserve("pilot", "chat", 0.2, {})
    with pytest.raises(BudgetStop, match="contract changed"):
        AttemptLedger(path, {**contract(), "total_cny": 2})


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 100000, "completion_tokens": 1},
    ],
)
def test_missing_invalid_or_excess_usage_fails_closed(tmp_path, usage):
    ledger = AttemptLedger(tmp_path / "budget.db", contract())
    key = ledger.reserve("pilot", "chat", 0.2, {})
    ledger.finish(key, usage)
    assert ledger.events()[0]["status"] == "unknown"
    with pytest.raises(BudgetStop):
        ledger.reserve("pilot", "chat", 0.2, {})


@pytest.mark.asyncio
async def test_sdk_retry_never_sends_after_unknown_failed_request(tmp_path):
    requests = []

    async def fail(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    ledger = AttemptLedger(tmp_path / "budget.db", contract())
    transport = MeteredTransport(ledger, "pilot", "chat", lambda: {}, httpx.MockTransport(fail))
    async with AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        max_retries=1,
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        with pytest.raises(Exception):
            await client.chat.completions.create(
                model="test-model", messages=[{"role": "user", "content": "hi"}], max_tokens=50
            )
    assert len(requests) == len(ledger.events()) == 1
    assert ledger.events()[0]["http_status"] == 429
    assert ledger.events()[0]["estimated_cny"] is None


@pytest.mark.asyncio
async def test_buffered_stream_usage_and_cancellation_are_accounted(tmp_path):
    ledger = AttemptLedger(tmp_path / "budget.db", contract())

    async def stream(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(
        transport=MeteredTransport(ledger, "pilot", "chat", lambda: {}, httpx.MockTransport(stream))
    ) as client:
        await client.post(
            "https://example.test/v1/chat/completions",
            json={"model": "test-model", "max_tokens": 50},
        )
    assert ledger.events()[0]["estimated_cny"] == pytest.approx(0.00144)

    async def cancel(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=MeteredTransport(ledger, "pilot", "chat", lambda: {}, httpx.MockTransport(cancel))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post(
                "https://example.test/v1/chat/completions",
                json={"model": "test-model", "max_tokens": 50},
            )
    assert len(ledger.events()) == 2
    assert ledger.events()[-1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_unpriced_model_and_missing_cap_block_before_network(tmp_path):
    ledger = AttemptLedger(tmp_path / "budget.db", contract())

    async def unexpected(request):
        pytest.fail("Must not send")

    async with httpx.AsyncClient(
        transport=MeteredTransport(
            ledger, "pilot", "chat", lambda: {}, httpx.MockTransport(unexpected)
        )
    ) as client:
        for body in ({"model": "other", "max_tokens": 50}, {"model": "test-model"}):
            with pytest.raises(BudgetStop):
                await client.post("https://example.test/v1/chat/completions", json=body)
    assert ledger.events() == []
