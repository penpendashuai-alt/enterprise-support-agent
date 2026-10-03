import asyncio
import hashlib
import math
from collections import deque
from contextlib import asynccontextmanager

from core import settings
from execution.redis_runtime import client
from execution.telemetry import ControlError, current, measure

TOKEN_BUCKET = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local caps = {tonumber(ARGV[1]), tonumber(ARGV[3])}
local rates = {tonumber(ARGV[2]), tonumber(ARGV[4])}
local tokens = {}
local wait = 0
for i=1,2 do
  local old = redis.call('HMGET', KEYS[i], 'tokens', 'time')
  tokens[i] = math.min(caps[i], (tonumber(old[1]) or caps[i]) + math.max(0, now-(tonumber(old[2]) or now))*rates[i])
  if tokens[i] < 1 then wait=math.max(wait,(1-tokens[i])/rates[i]) end
end
if wait>0 then return {0,math.ceil(wait*1000)} end
for i=1,2 do
  redis.call('HSET', KEYS[i], 'tokens', tokens[i]-1, 'time', now)
  redis.call('PEXPIRE', KEYS[i], math.ceil(2*caps[i]/rates[i]*1000))
end
return {1,0}
"""


class Capacity:
    def __init__(self, limit, queue):
        self.limit, self.queue = limit, queue
        self.present = self.active = 0
        self.semaphore = asyncio.Semaphore(limit)

    @asynccontextmanager
    async def reserve(self):
        if self.present >= self.limit + self.queue:
            raise ControlError("queue_full")
        self.present += 1
        try:
            yield
        finally:
            self.present -= 1

    @asynccontextmanager
    async def execute(self, timing="execution_wait"):
        with measure(timing):
            try:
                async with asyncio.timeout(settings.QUEUE_WAIT_TIMEOUT):
                    await self.semaphore.acquire()
            except TimeoutError:
                raise ControlError("execution_wait_timeout") from None
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self.semaphore.release()


class Controls:
    def __init__(self):
        self.completed = deque(maxlen=200)
        self.classes = {
            "model": Capacity(settings.EXECUTION_LIMIT, settings.EXECUTION_QUEUE_LIMIT),
            "approval": Capacity(settings.PROTECTED_LIMIT, settings.PROTECTED_QUEUE_LIMIT),
            "read": Capacity(settings.READ_LIMIT, settings.READ_QUEUE_LIMIT),
        }
        self.models = Capacity(
            settings.MODEL_LIMIT, settings.EXECUTION_LIMIT + settings.PROTECTED_LIMIT
        )

    def snapshot(self):
        return {
            k: {"present": v.present, "active": v.active}
            for k, v in {**self.classes, "model_calls": self.models}.items()
        }

    async def rate(self, user_id, category):
        # Both buckets debit only if both can admit. Rejected requests debit neither.
        factor = {"model": 1, "approval": 2, "read": 3}[category]
        prefix = settings.REDIS_NAMESPACE + ":rate:" + category
        user = hashlib.sha256(user_id.encode()).hexdigest()
        try:
            redis = client()
            if redis is None:
                raise ConnectionError("Redis not initialized")
            with measure("rate_check"):
                async with asyncio.timeout(settings.REDIS_TIMEOUT):
                    ok, wait = await redis.eval(
                        TOKEN_BUCKET,
                        2,
                        prefix + ":service",
                        prefix + ":" + user,
                        settings.RATE_SERVICE_CAPACITY * factor,
                        settings.RATE_SERVICE_REFILL * factor,
                        settings.RATE_USER_CAPACITY * factor,
                        settings.RATE_USER_REFILL * factor,
                    )
        except Exception:
            if category == "model":
                raise ControlError("rate_dependency_unavailable") from None
            if trace := current.get():
                trace.timings["rate_local_fallback"] = 1
            return
        if not ok:
            raise ControlError("rate_limited", 429, max(1, math.ceil(wait / 1000)))


_runtimes: dict = {}


def runtime():
    loop = asyncio.get_running_loop()
    if loop not in _runtimes:
        _runtimes[loop] = Controls()
    return _runtimes[loop]


def close_runtime():
    _runtimes.pop(asyncio.get_running_loop(), None)


@asynccontextmanager
async def model_stage(name, timeout):
    @asynccontextmanager
    async def capacity():
        if settings.ADMISSION_ENABLED:
            async with runtime().models.reserve(), runtime().models.execute("model_wait"):
                yield
        else:
            yield

    try:
        async with asyncio.timeout(timeout), capacity():
            with measure(name):
                yield
    except TimeoutError:
        raise ControlError("model_timeout", 504) from None
