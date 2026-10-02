import asyncio
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.settings import settings


def selector_loop_factory(use_subprocess=False):
    return asyncio.SelectorEventLoop()


def validate_postgres_config() -> None:
    required = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ]
    if any(not getattr(settings, key) for key in required):
        raise ValueError("PostgreSQL connection settings are incomplete")
    if (
        not 1
        <= settings.POSTGRES_MIN_CONNECTIONS_PER_POOL
        <= settings.POSTGRES_MAX_CONNECTIONS_PER_POOL
        <= 20
    ):
        raise ValueError("PostgreSQL pool bounds must satisfy 1 <= min <= max <= 20")


def get_postgres_connection_string() -> str:
    validate_postgres_config()
    assert settings.POSTGRES_PASSWORD is not None
    return make_conninfo(
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD.get_secret_value(),
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        connect_timeout=settings.POSTGRES_CONNECT_TIMEOUT,
    )


@asynccontextmanager
async def postgres_pool(role: str, *, autocommit: bool):
    pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
        get_postgres_connection_string(),
        open=False,
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        timeout=settings.POSTGRES_POOL_TIMEOUT,
        max_waiting=settings.POSTGRES_POOL_MAX_WAITING,
        reconnect_timeout=settings.POSTGRES_POOL_TIMEOUT,
        kwargs={
            "autocommit": autocommit,
            "row_factory": dict_row,
            "application_name": f"{settings.POSTGRES_APPLICATION_NAME}-{role}",
            "options": f"-c statement_timeout={settings.POSTGRES_STATEMENT_TIMEOUT_MS}",
        },
        check=AsyncConnectionPool.check_connection,
    )
    try:
        await pool.open(wait=True, timeout=settings.POSTGRES_POOL_TIMEOUT)
        yield pool
    finally:
        await pool.close(timeout=5)


@asynccontextmanager
async def get_postgres_saver():
    async with postgres_pool("saver", autocommit=True) as pool:
        yield AsyncPostgresSaver(pool)


@asynccontextmanager
async def get_postgres_store():
    async with postgres_pool("store", autocommit=True) as pool:
        yield AsyncPostgresStore(pool)
