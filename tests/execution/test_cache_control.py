import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import settings
from execution.control import Capacity, Controls
from execution.telemetry import ControlError
from rag.cache import RetrievalCache, cache_key
from rag.config import RAGSettings
from rag.hybrid_retriever import select_evidence
from rag.models import Candidate, RetrievalResult


def fixture():
    config = RAGSettings(_env_file=None, RAG_RETRIEVAL_MODE="dense", RAG_DENSE_LEGACY=False)
    manifest = {
        "status": "ready",
        "snapshot_id": "snapshot",
        "index_version": "version",
        "contract": config.index_contract(),
    }
    row = Candidate(
        doc_id="demo",
        chunk_id="demo-1",
        title="Demo",
        source_type="synthetic",
        source_path="demo.md",
        url=None,
        document_version="1",
        location="1",
        text="MFA is required.",
        content_hash="hash",
        score=0.9,
        dense_score=0.9,
        score_type="dense",
        collection="demo",
        index_version="version",
        snapshot_id="snapshot",
        number=0,
    )
    evidence, selection = select_evidence([row], config, "dense")
    result = RetrievalResult(
        status="ok",
        query="MFA",
        requested_mode="dense",
        actual_mode="dense",
        collection="demo",
        snapshot_id="snapshot",
        index_version="version",
        candidates=[row],
        evidence=evidence,
        selection=selection,
        usage={"embedding_tokens": 10, "requests": 1},
    )
    retriever = SimpleNamespace(
        settings=config,
        dense=SimpleNamespace(manifest=AsyncMock(return_value=manifest), collection="demo"),
        _retrieve=AsyncMock(return_value=result),
    )
    return retriever, manifest


class MemoryCache:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex):
        self.data[key] = value


@pytest.mark.parametrize(
    "field,value",
    [
        ("RAG_MIN_SCORE", 0.8),
        ("RAG_FINAL_K", 3),
        ("RAG_DENSE_CANDIDATES", 10),
        ("RAG_CONTEXT_CHARS", 3000),
        ("EMBED_MODEL_NAME", "changed"),
    ],
)
def test_versioned_key(field, value):
    retriever, manifest = fixture()
    key = cache_key(retriever.settings, "ERR-401", manifest, "demo")
    assert key != cache_key(
        retriever.settings.model_copy(update={field: value}), "ERR-401", manifest, "demo"
    )
    for query in ["ERR-403", "not ERR-401", "ERR-401 "]:
        assert key != cache_key(retriever.settings, query, manifest, "demo")
    assert key != cache_key(
        retriever.settings, "ERR-401", {**manifest, "index_version": "v2"}, "demo"
    )


@pytest.mark.asyncio
async def test_cache_equivalence_corruption_index_and_size(monkeypatch):
    redis = MemoryCache()
    monkeypatch.setattr("rag.cache.client", lambda: redis)
    cache = RetrievalCache()
    retriever, manifest = fixture()
    first = await cache.retrieve(retriever, "MFA")
    hit = await cache.retrieve(retriever, "MFA")
    assert hit.cache["status"] == "hit"
    assert hit.evidence == first.evidence and hit.candidates == first.candidates
    assert hit.usage["requests"] == 0 and first.usage["requests"] == 1
    assert retriever._retrieve.await_count == 1 and retriever.dense.manifest.await_count == 2
    key = next(iter(redis.data))
    redis.data[key] = b'{"schema":"bad"}'
    corrupt = await cache.retrieve(retriever, "MFA")
    assert corrupt.cache["status"] == "corrupt" and retriever._retrieve.await_count == 2
    manifest["status"] = "building"
    bad = await cache.retrieve(retriever, "MFA")
    assert bad.status == "index_inconsistent" and retriever._retrieve.await_count == 2
    manifest["status"] = "ready"
    redis.data.clear()
    monkeypatch.setattr(settings, "RAG_CACHE_MAX_BYTES", 1)
    assert (await cache.retrieve(retriever, "MFA")).cache["write"] == "oversize"
    assert not redis.data


@pytest.mark.asyncio
async def test_singleflight_cancel_and_bounds(monkeypatch):
    redis = MemoryCache()
    monkeypatch.setattr("rag.cache.client", lambda: redis)
    cache = RetrievalCache()
    retriever, _ = fixture()
    result = retriever._retrieve.return_value
    started, finish = asyncio.Event(), asyncio.Event()

    async def work(query):
        started.set()
        await finish.wait()
        return result

    retriever._retrieve.side_effect = work
    one = asyncio.create_task(cache.retrieve(retriever, "MFA"))
    await started.wait()
    two = asyncio.create_task(cache.retrieve(retriever, "MFA"))
    while cache.waiters < 2:
        await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    assert cache.waiters == 1 and not next(iter(cache.flights.values())).task.cancelled()
    finish.set()
    assert (await two).status == "ok" and retriever._retrieve.await_count == 1
    assert not cache.flights and cache.waiters == 0
    redis.data.clear()
    finish.clear()
    started.clear()
    one = asyncio.create_task(cache.retrieve(retriever, "MFA"))
    await started.wait()
    monkeypatch.setattr(settings, "RETRIEVAL_WAITERS_MAX", 1)
    assert (await cache.retrieve(retriever, "other")).error_code == "cache_waiter_capacity"
    monkeypatch.setattr(settings, "RETRIEVAL_WAITERS_MAX", 32)
    monkeypatch.setattr(settings, "RETRIEVAL_INFLIGHT_MAX", 1)
    assert (await cache.retrieve(retriever, "other")).error_code == "cache_inflight_capacity"
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    assert not cache.flights and cache.waiters == 0
    retriever._retrieve.side_effect = RuntimeError("dependency")
    assert (await cache.retrieve(retriever, "MFA")).status == "unavailable"
    assert not cache.flights and not redis.data


@pytest.mark.asyncio
async def test_unavailable_cache_and_limiter_policies(monkeypatch):
    monkeypatch.setattr("rag.cache.client", lambda: None)
    cache = RetrievalCache()
    retriever, _ = fixture()
    result = await cache.retrieve(retriever, "MFA")
    assert result.status == "ok" and result.cache["status"] == "unavailable"
    monkeypatch.setattr("execution.control.client", lambda: None)
    controls = Controls()
    with pytest.raises(ControlError, match="rate_dependency_unavailable"):
        await controls.rate("test", "model")
    await controls.rate("test", "approval")
    await controls.rate("test", "read")


@pytest.mark.asyncio
async def test_capacity_no_leak_on_full_timeout_cancel(monkeypatch):
    monkeypatch.setattr(settings, "QUEUE_WAIT_TIMEOUT", 0.01)
    cap = Capacity(1, 0)
    async with cap.reserve(), cap.execute():
        with pytest.raises(ControlError, match="queue_full"):
            async with cap.reserve():
                pass
        with pytest.raises(ControlError, match="execution_wait_timeout"):
            async with cap.execute():
                pass
        assert cap.active == 1
    assert cap.active == cap.present == 0
    started = asyncio.Event()

    async def run():
        async with cap.reserve(), cap.execute():
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cap.active == cap.present == 0 and cap.semaphore._value == 1


@pytest.mark.asyncio
async def test_checksum_and_structure_cannot_inject_evidence(monkeypatch):
    redis = MemoryCache()
    monkeypatch.setattr("rag.cache.client", lambda: redis)
    cache = RetrievalCache()
    retriever, _ = fixture()
    await cache.retrieve(retriever, "MFA")
    key = next(iter(redis.data))
    value = json.loads(redis.data[key])
    value["result"]["evidence"][0]["text"] = "injected"
    redis.data[key] = json.dumps(value).encode()
    assert (await cache.retrieve(retriever, "MFA")).cache["status"] == "corrupt"
