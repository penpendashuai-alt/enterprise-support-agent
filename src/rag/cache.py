import asyncio
import json
from contextvars import copy_context
from dataclasses import dataclass
from time import perf_counter

from core import settings
from execution.redis_runtime import client
from execution.telemetry import current
from rag.models import Candidate, RAGError, RetrievalResult, digest

SCHEMA = "dense-result-v1"


def cache_key(config, query, manifest, collection):
    excluded = {"QDRANT_API_KEY", "EMBED_API_KEY", "ES_API_KEY", "RERANK_API_KEY"}
    contract = {k: v for k, v in config.model_dump(mode="json").items() if k not in excluded}
    return (
        settings.REDIS_NAMESPACE
        + ":cache:"
        + digest(
            {
                "schema": SCHEMA,
                "query": query,
                "normalization": "identity-v1",
                "config": contract,
                "manifest": manifest,
                "collection": collection,
            }
        )
    )


@dataclass
class Flight:
    task: asyncio.Task
    waiters: int = 0
    claimed: bool = False


class RetrievalCache:
    def __init__(self):
        self.flights: dict[str, Flight] = {}
        self.waiters = 0

    async def close(self):
        tasks = [f.task for f in self.flights.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.flights.clear()

    def validate(self, raw, key, query, manifest, config, collection):
        from rag.hybrid_retriever import select_evidence

        if len(raw) > settings.RAG_CACHE_MAX_BYTES:
            raise ValueError("cache_size")
        envelope = json.loads(raw)
        if (
            set(envelope) != {"schema", "key", "result", "checksum"}
            or envelope["schema"] != SCHEMA
            or envelope["key"] != key
        ):
            raise ValueError("cache_envelope")
        payload = envelope["result"]
        if digest(payload) != envelope["checksum"]:
            raise ValueError("cache_checksum")
        value = RetrievalResult.model_validate(payload)
        if (
            value.status != "ok"
            or value.failures
            or value.query != query
            or value.snapshot_id != manifest["snapshot_id"]
            or value.index_version != manifest["index_version"]
            or value.collection != collection
            or value.actual_mode != "dense"
            or value.requested_mode != "dense"
        ):
            raise ValueError("cache_contract")
        if len(value.candidates) > config.RAG_DENSE_CANDIDATES:
            raise ValueError("cache_candidates")
        for row in value.candidates:
            if (
                not isinstance(row, Candidate)
                or row.snapshot_id != value.snapshot_id
                or row.index_version != value.index_version
                or row.collection != collection
            ):
                raise ValueError("cache_source")
        selected, decisions = select_evidence(
            [r for r in value.candidates if isinstance(r, Candidate)], config, "dense"
        )
        if selected != value.evidence or decisions != value.selection:
            raise ValueError("cache_evidence")
        return value

    async def compute(self, retriever, query, manifest, key, redis):
        async with asyncio.timeout(
            min(retriever.settings.RAG_TIMEOUT, settings.SHARED_RETRIEVAL_TIMEOUT)
        ):
            result = await retriever._retrieve(query)
            if (
                result.status == "ok"
                and not result.failures
                and result.snapshot_id == manifest["snapshot_id"]
                and result.index_version == manifest["index_version"]
            ):
                payload = result.model_dump(exclude={"timings", "usage", "cache"})
                raw = json.dumps(
                    {"schema": SCHEMA, "key": key, "result": payload, "checksum": digest(payload)},
                    ensure_ascii=False,
                ).encode()
                point = perf_counter()
                outcome = "oversize"
                if len(raw) <= settings.RAG_CACHE_MAX_BYTES:
                    try:
                        async with asyncio.timeout(settings.REDIS_TIMEOUT):
                            await redis.set(key, raw, ex=settings.RAG_CACHE_TTL)
                        outcome = "stored"
                    except Exception:
                        outcome = "unavailable"
                result.cache["write"] = outcome
                result.timings["redis_write_seconds"] = perf_counter() - point
            return result

    async def retrieve(self, retriever, query):
        started = perf_counter()
        config = retriever.settings
        timing, status = {}, "miss"
        try:
            async with asyncio.timeout(config.RAG_TIMEOUT):
                point = perf_counter()
                manifest = await retriever.dense.manifest()
                timing["index_check_seconds"] = perf_counter() - point
                if manifest.get("status") != "ready" or not manifest.get("snapshot_id"):
                    raise RAGError("index_not_ready_or_snapshot_missing")
                if manifest.get("contract") != config.index_contract():
                    raise RAGError("index_model_or_configuration_mismatch")
                key = cache_key(config, query, manifest, retriever.dense.collection)
                redis = client()
                point = perf_counter()
                try:
                    if redis is None:
                        raise ConnectionError("Cache not initialized")
                    async with asyncio.timeout(settings.REDIS_TIMEOUT):
                        raw = await redis.get(key)
                    if raw is not None:
                        value = self.validate(
                            raw, key, query, manifest, config, retriever.dense.collection
                        )
                        timing["redis_read_seconds"] = perf_counter() - point
                        return value.model_copy(
                            update={
                                "timings": {
                                    **timing,
                                    "retrieval_seconds": perf_counter() - started,
                                },
                                "usage": {"embedding_tokens": 0, "requests": 0, "retries": 0},
                                "cache": {"status": "hit", "key_digest": key.rsplit(":", 1)[-1]},
                            }
                        )
                except (ValueError, TypeError, KeyError):
                    status = "corrupt"
                except Exception:
                    status = "unavailable"
                timing["redis_read_seconds"] = perf_counter() - point
                if self.waiters >= settings.RETRIEVAL_WAITERS_MAX:
                    raise RAGError("cache_waiter_capacity")
                flight = self.flights.get(key)
                shared = flight is not None
                if flight is None:
                    if len(self.flights) >= settings.RETRIEVAL_INFLIGHT_MAX:
                        raise RAGError("cache_inflight_capacity")
                    context = copy_context()
                    context.run(current.set, None)
                    flight = Flight(
                        asyncio.create_task(
                            self.compute(retriever, query, manifest, key, redis), context=context
                        )
                    )
                    self.flights[key] = flight
                flight.waiters += 1
                self.waiters += 1
                point = perf_counter()
                try:
                    result = (await asyncio.shield(flight.task)).model_copy(deep=True)
                    timing["shared_wait_seconds"] = perf_counter() - point
                    if flight.claimed:
                        result.usage = {"embedding_tokens": 0, "requests": 0, "retries": 0}
                        result.timings = {}
                    else:
                        flight.claimed = True
                    result.timings.update(timing)
                    result.timings["retrieval_seconds"] = perf_counter() - started
                    result.cache.update(
                        status=status, shared=shared, key_digest=key.rsplit(":", 1)[-1]
                    )
                    return result
                finally:
                    flight.waiters -= 1
                    self.waiters -= 1
                    if flight.waiters == 0:
                        if self.flights.get(key) is flight:
                            self.flights.pop(key)
                        if not flight.task.done():
                            flight.task.cancel()
                        await asyncio.gather(flight.task, return_exceptions=True)
        except (RAGError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, RAGError) else "retrieval_timeout"
            return RetrievalResult(
                status="index_inconsistent" if code.startswith("index_") else "unavailable",
                query=query,
                error_code=code,
                timings={**timing, "retrieval_seconds": perf_counter() - started},
                cache={"status": status},
            )
        except Exception:
            return RetrievalResult(
                status="unavailable",
                query=query,
                error_code="retrieval_dependency_error",
                timings={**timing, "retrieval_seconds": perf_counter() - started},
                cache={"status": status},
            )


_caches: dict = {}


def runtime():
    loop = asyncio.get_running_loop()
    if loop not in _caches:
        _caches[loop] = RetrievalCache()
    return _caches[loop]


async def close_cache():
    cache = _caches.pop(asyncio.get_running_loop(), None)
    if cache:
        await cache.close()
