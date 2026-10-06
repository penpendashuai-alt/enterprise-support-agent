"""Run only inside a disposable closeout stack; never flush a shared Redis."""

import asyncio
import json
import sys

from redis.asyncio import Redis
from redis.exceptions import OutOfMemoryError

from core import settings

PREFIX = "esa:p9:closeout-memory:"
SENTINEL = PREFIX + "rate-state"


async def run(mode):
    if not settings.CI_TEST_MODE:
        raise ValueError("Memory probes require explicit CI mode")
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        if mode == "fill":
            assert (await connection.info("memory"))["maxmemory"] == 4 * 1024 * 1024
            await connection.hset(SENTINEL, mapping={"tokens": "7", "time": "synthetic"})
            for index in range(1024):
                try:
                    await connection.set(PREFIX + str(index), "x" * 32768)
                except OutOfMemoryError as exc:
                    result = {
                        "oom_type": type(exc).__name__,
                        "oom_message": str(exc),
                        "written_keys": index,
                    }
                    break
            else:
                raise AssertionError("Expected bounded OOM before 32 MiB test payload")
            # Keep pressure stable after the fill client's network buffers are freed.
            await connection.config_set("maxmemory", 3 * 1024 * 1024)
            result["pressure_budget_bytes"] = 3 * 1024 * 1024
            result["initial_budget_bytes"] = 4 * 1024 * 1024
        elif mode == "cache":
            from execution.redis_runtime import initialize_redis
            from rag.retriever import close_retriever, retrieve

            async with initialize_redis():
                try:
                    value = await retrieve("CI_MEMORY_CACHE_SET")
                    result = {
                        "status": value.status,
                        "cache": value.cache,
                        "evidence": len(value.evidence),
                    }
                    assert value.status == "ok" and value.evidence
                    assert value.cache["write"] == "unavailable"
                finally:
                    await close_retriever()
        elif mode == "release":
            keys = [key async for key in connection.scan_iter(PREFIX + "*")]
            if keys:
                await connection.delete(*keys)
            await connection.config_set("maxmemory", 4 * 1024 * 1024)
            result = {"deleted_own_keys": len(keys)}
        else:
            result = {}
        memory = await connection.info("memory")
        result["memory"] = {
            key: memory[key]
            for key in [
                "used_memory",
                "used_memory_rss",
                "maxmemory",
                "maxmemory_policy",
                "mem_not_counted_for_evict",
            ]
        }
        result["evicted_keys"] = (await connection.info("stats"))["evicted_keys"]
        result["sentinel"] = await connection.hgetall(SENTINEL)
        print(json.dumps(result))
    finally:
        await connection.aclose()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
