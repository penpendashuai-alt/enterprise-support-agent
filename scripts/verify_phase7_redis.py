"""Opt-in real Redis verification, using only UUID-scoped keys with finite TTL."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from core import settings
from execution.control import TOKEN_BUCKET
from execution.redis_runtime import client, initialize_redis
from rag.cache import RetrievalCache

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/execution"))
from test_cache_control import fixture  # noqa: E402


async def run(args):
    if args.output.exists():
        raise ValueError("Preserve existing report")
    settings.REDIS_URL = SecretStr(args.redis_url)
    settings.REDIS_NAMESPACE = "esa:p7:verify:" + uuid4().hex
    settings.RAG_CACHE_ENABLED = True
    settings.RAG_CACHE_TTL = 1
    report = {"checks": [], "namespace": settings.REDIS_NAMESPACE}
    async with initialize_redis():
        redis = client()
        info = await redis.info("server")
        report["redis_version"] = info["redis_version"]
        cache, (retriever, manifest) = RetrievalCache(), fixture()
        first = await cache.retrieve(retriever, "MFA")
        hit = await cache.retrieve(retriever, "MFA")
        assert hit.cache["status"] == "hit" and hit.evidence == first.evidence
        assert hit.usage["requests"] == 0 and retriever._retrieve.await_count == 1
        report["checks"].append("equivalent_hit_zero_usage")
        keys = [k async for k in redis.scan_iter(settings.REDIS_NAMESPACE + ":cache:*")]
        assert len(keys) == 1 and 0 < await redis.pttl(keys[0]) <= 1000
        await asyncio.sleep(1.05)
        assert (await cache.retrieve(retriever, "MFA")).cache["status"] == "miss"
        assert retriever._retrieve.await_count == 2
        report["checks"].append("real_ttl_expiry_recomputes")
        await redis.set(keys[0], b"corrupt", ex=1)
        assert (await cache.retrieve(retriever, "MFA")).cache["status"] == "corrupt"
        manifest["status"] = "building"
        assert (await cache.retrieve(retriever, "MFA")).status == "index_inconsistent"
        manifest["status"] = "ready"
        report["checks"].append("corruption_recompute_index_check_before_hit")
        service = settings.REDIS_NAMESPACE + ":service"
        user = settings.REDIS_NAMESPACE + ":user"

        async def permit(s=service, u=user, cap=7, refill=0.001):
            return await redis.eval(TOKEN_BUCKET, 2, s, u, 100, refill, cap, refill)

        results = await asyncio.gather(*(permit() for _ in range(100)))
        assert sum(row[0] for row in results) == 7
        state = await redis.hgetall(service)
        assert 92.99 <= float(state[b"tokens"]) < 93.01
        assert await redis.pttl(service) > 0 and await redis.pttl(user) > 0
        report["checks"].append("100_concurrent_exactly_7_all_or_nothing_debits")
        for key in (service, user):
            await redis.pexpire(key, 50)
        await asyncio.sleep(0.08)
        assert not await redis.exists(service, user)
        assert (await permit(cap=1, refill=10))[0] == 1
        assert (await permit(cap=1, refill=10))[0] == 0
        await asyncio.sleep(0.12)
        assert (await permit(cap=1, refill=10))[0] == 1
        report["checks"].append("redis_time_refill_and_expiry")
        # Service exhaustion must not debit a newly seen user.
        await redis.delete(service, user)
        args_lua = (TOKEN_BUCKET, 2, service, user, 1, 0.001, 100, 0.001)
        assert (await redis.eval(*args_lua))[0] == 1
        other = settings.REDIS_NAMESPACE + ":other"
        assert (await redis.eval(TOKEN_BUCKET, 2, service, other, 1, 0.001, 100, 0.001))[0] == 0
        assert not await redis.exists(other)
        report["checks"].append("service_exhaustion_does_not_create_or_debit_user")
        owned = [k async for k in redis.scan_iter(settings.REDIS_NAMESPACE + ":*")]
        if owned:
            await redis.delete(*owned)
        await cache.close()
    report["status"] = "passed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
