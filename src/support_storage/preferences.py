import logging
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages import SystemMessage
from pydantic import BaseModel, ConfigDict

from execution.telemetry import measured
from support_storage.identity import identity

logger = logging.getLogger(__name__)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["zh", "en"] = "zh"
    detail: Literal["concise", "detailed"] = "concise"


def namespace(user_id):
    return ("support", identity(user_id), "preferences")


@measured("preferences")
async def read_preferences(store, user_id):
    if store is None:
        return {"status": "unavailable", "preferences": None, "reason": "store_not_initialized"}
    try:
        item = await store.aget(namespace(user_id), "explicit")
        if item is None:
            return {"status": "default", "preferences": None}
        value = item.value
        if value.get("schema_version") != 1 or value.get("source") != "explicit_user_setting":
            raise ValueError("Invalid preference metadata")
        prefs = Preferences.model_validate(value["preferences"])
        return {"status": "saved", **value, "preferences": prefs.model_dump()}
    except Exception as exc:
        logger.warning("Preference read unavailable: %s", type(exc).__name__)
        return {"status": "unavailable", "preferences": None, "reason": "store_read_failed"}


async def save_preferences(store, user_id, prefs):
    value = {
        "schema_version": 1,
        "source": "explicit_user_setting",
        "updated_at": datetime.now(UTC).isoformat(),
        "preferences": prefs.model_dump(),
    }
    await store.aput(namespace(user_id), "explicit", value, index=False)
    return {"status": "saved", **value}


async def delete_preferences(store, user_id):
    await store.adelete(namespace(user_id), "explicit")
    return {
        "status": "deleted",
        "message": "仅删除后续回答使用的偏好；历史消息、检查点和工单保持不变。",
    }


async def preference_message(store, user_id):
    result = await read_preferences(store, user_id)
    if not result["preferences"]:
        return []
    prefs = Preferences.model_validate(result["preferences"])
    language = {"zh": "中文", "en": "英文"}[prefs.language]
    detail = {"concise": "简洁", "detailed": "详细"}[prefs.detail]
    return [
        SystemMessage(
            content=f"用户明确保存的表达偏好：{language}，{detail}。这覆盖默认的‘用中文简洁回答’表达要求；本轮没有明确要求另一种语言时，必须用{language}回答，提问使用的语言本身不算修改偏好。本轮用户明确提出的语言和详细程度要求优先。偏好只改变表达，不改变事实、知识证据、工具权限或工单审批。"
        )
    ]
