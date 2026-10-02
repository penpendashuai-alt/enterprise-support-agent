import re

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig

from core.settings import DatabaseType, settings
from support_storage.repository import OwnershipConflict
from support_storage.runtime import repository


def identity(value: str | None) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise HTTPException(
            422,
            detail={
                "code": "user_id_required",
                "message": "请建立稳定的演示用户 ID。客户端声明身份不等于登录认证。",
            },
        )
    return value


def context_user(config) -> str:
    return identity(config.get("configurable", {}).get("user_id"))


async def guard_thread(thread_id, user_id, agent_id, *, create=False, title="", checkpointer=None):
    if settings.DATABASE_TYPE == DatabaseType.MONGO and agent_id != "support-agent":
        return user_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", thread_id):
        raise HTTPException(422, detail="Invalid thread_id")
    if agent_id == "support-agent":
        user_id = identity(user_id)
    try:
        repo = repository()
        row = await repo.session(thread_id)
        if row:
            if row["user_id"] != user_id or row["agent_id"] != agent_id:
                raise HTTPException(403, detail="Thread ownership mismatch")
        elif agent_id == "support-agent":
            if not create:
                raise HTTPException(404, detail="Support session not found")
            if checkpointer and await checkpointer.aget_tuple(
                RunnableConfig(configurable={"thread_id": thread_id})
            ):
                raise HTTPException(
                    409,
                    detail="Unregistered historical checkpoint; use a new thread or the original environment",
                )
        if agent_id == "support-agent" and create:
            await repo.register(user_id, thread_id, title)
    except HTTPException:
        raise
    except OwnershipConflict:
        raise HTTPException(403, detail="Thread ownership mismatch") from None
    except Exception:
        raise HTTPException(503, detail="Support business storage unavailable") from None
    return user_id
