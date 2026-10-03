import asyncio
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from core import settings

_clients: dict = {}


def client():
    return _clients.get(asyncio.get_running_loop())


@asynccontextmanager
async def initialize_redis():
    loop = asyncio.get_running_loop()
    connection = None
    if settings.RAG_CACHE_ENABLED or settings.ADMISSION_ENABLED:
        connection = Redis.from_url(
            settings.REDIS_URL.get_secret_value(),
            socket_timeout=settings.REDIS_TIMEOUT,
            socket_connect_timeout=settings.REDIS_TIMEOUT,
            max_connections=settings.REDIS_POOL_SIZE,
            retry=Retry(NoBackoff(), 0),
            decode_responses=False,
        )
        _clients[loop] = connection
    try:
        yield connection
    finally:
        from execution.control import close_runtime
        from rag.cache import close_cache

        await close_cache()
        close_runtime()
        _clients.pop(loop, None)
        if connection:
            await connection.aclose()
