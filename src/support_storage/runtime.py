from contextlib import asynccontextmanager
from functools import lru_cache

from core.settings import DatabaseType, settings
from memory.postgres import postgres_pool
from support_storage.migrations import verify_schema
from support_storage.postgres import PostgresRepository
from support_storage.repository import SupportRepository
from support_storage.sqlite import SQLiteRepository

_active: SupportRepository | None = None


@lru_cache(maxsize=16)
def sqlite_repository(path):
    return SQLiteRepository(path)


def repository() -> SupportRepository:
    if settings.DATABASE_TYPE == DatabaseType.POSTGRES:
        if _active is None:
            raise RuntimeError("PostgreSQL business storage is not initialized")
        return _active
    if settings.DATABASE_TYPE == DatabaseType.SQLITE:
        return sqlite_repository(settings.TICKET_DB_PATH)
    raise RuntimeError("Support Agent requires explicit PostgreSQL or SQLite mode")


@asynccontextmanager
async def initialize_support_storage():
    global _active
    if settings.DATABASE_TYPE == DatabaseType.POSTGRES:
        async with postgres_pool("business", autocommit=False) as pool:
            await verify_schema(pool)
            _active = PostgresRepository(pool)
            try:
                yield _active
            finally:
                _active = None
    else:
        yield None
