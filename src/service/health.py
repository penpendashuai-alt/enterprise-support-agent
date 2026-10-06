import asyncio
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core import settings
from core.settings import DatabaseType
from execution.redis_runtime import client
from support_storage.migrations import verify_schema
from support_storage.postgres import PostgresRepository
from support_storage.runtime import repository

router = APIRouter()


def state(status, reason):
    return {"status": status, "reason": reason}


@router.get("/health")
@router.get("/health/live")
async def live():
    return {"status": "ok"}


async def core(request):
    if not getattr(request.app.state, "support_ready", False):
        return state("unavailable", "initializing")
    try:
        async with asyncio.timeout(settings.HEALTH_TIMEOUT):
            repo = repository()
            if settings.DATABASE_TYPE == DatabaseType.POSTGRES:
                if not isinstance(repo, PostgresRepository):
                    raise RuntimeError
                await verify_schema(repo.pool)
                if len(request.app.state.graph_pools) != 2:
                    raise RuntimeError
                for pool, table in request.app.state.graph_pools:
                    async with pool.connection() as conn:
                        if table not in {"checkpoints", "store"}:
                            raise RuntimeError
                        await conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            else:
                await repo.tickets("health-probe", 1)
        return state("ok", "core_ready")
    except Exception:
        return state("unavailable", "storage_or_schema_unavailable")


@router.get("/health/ready")
async def ready(request: Request):
    result = await core(request)
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


async def diagnose(request):
    from execution.observability import tracing_status
    from rag.config import get_settings
    from rag.vector_store import VectorStore

    database = await core(request)
    redis = state("disabled", "not_enabled")
    if settings.ADMISSION_ENABLED or settings.RAG_CACHE_ENABLED:
        try:
            async with asyncio.timeout(settings.HEALTH_TIMEOUT):
                connection = client()
                if connection is None:
                    raise RuntimeError
                await connection.ping()
                memory = await connection.info("memory")
            budget = memory.get("maxmemory", 0)
            accounted = memory.get("used_memory", 0) - memory.get("mem_not_counted_for_evict", 0)
            redis = (
                state("unavailable", "redis_memory_limit")
                if budget and accounted >= budget and memory.get("maxmemory_policy") == "noeviction"
                else state("ok", "reachable")
            )
        except Exception:
            redis = state("unavailable", "redis_unavailable")
    retrieval = state("unavailable", "rag_not_configured")
    store = None
    try:
        async with asyncio.timeout(settings.HEALTH_TIMEOUT):
            config = get_settings()
            config.require_mode()
            store = VectorStore(config)
            manifest = await store.manifest()
            count = await store.chunk_count()
            if (
                manifest.get("status") != "ready"
                or count != manifest.get("chunk_count")
                or not count
                or (not config.RAG_DENSE_LEGACY and not manifest.get("snapshot_id"))
            ):
                retrieval = state("unavailable", "index_not_ready")
            else:
                retrieval = state("ok", "dense_index_ready")
                if config.RAG_RETRIEVAL_MODE != "dense":
                    retrieval = state("degraded", "optional_retrieval_dependencies_unprobed")
    except ValueError:
        pass
    except Exception:
        retrieval = state("unavailable", "index_or_qdrant_unavailable")
    finally:
        if store:
            await store.close()
    blocked = database["status"] != "ok"
    return {
        "core": database,
        "question": database
        if blocked
        else state("unavailable", "redis_required")
        if settings.ADMISSION_ENABLED and redis["status"] == "unavailable"
        else state("ok", "model_configured_callability_unprobed"),
        "approval_and_read": database
        if blocked
        else state("degraded", "local_admission_fallback")
        if settings.ADMISSION_ENABLED and redis["status"] == "unavailable"
        else state("ok", "available"),
        "retrieval": retrieval,
        "cache": redis,
        "tracing": tracing_status(),
        "model": state("configured", "no_paid_probe"),
    }


async def capabilities(request: Request):
    cached = getattr(request.app.state, "capability_cache", None)
    if cached and monotonic() - cached[0] < 5:
        return cached[1]
    lock = getattr(request.app.state, "capability_lock", None)
    if lock is None:
        lock = request.app.state.capability_lock = asyncio.Lock()
    async with lock:
        cached = getattr(request.app.state, "capability_cache", None)
        if cached and monotonic() - cached[0] < 5:
            return cached[1]
        result = await diagnose(request)
        request.app.state.capability_cache = (monotonic(), result)
        return result
