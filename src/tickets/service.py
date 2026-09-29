import asyncio

from core import settings
from tickets.models import TicketDraft, fingerprint, request_key, ticket_result
from tickets.repository import TicketConflict, TicketRepository


def repository() -> TicketRepository:
    return TicketRepository(settings.TICKET_DB_PATH)


async def create_ticket(draft: TicketDraft, draft_id: str, version: int, thread_id: str) -> dict:
    repo = repository()
    try:
        record = await asyncio.to_thread(repo.create, draft, draft_id, version, thread_id)
        return ticket_result(record)
    except TicketConflict:
        return {"status": "conflict", "message": "创建请求与已保存内容冲突，未覆盖已有工单。"}
    except Exception:
        # A failed response does not establish whether the SQLite commit happened.
        try:
            record = await asyncio.to_thread(repo.by_request, draft_id, thread_id)
        except Exception:
            return {
                "status": "unknown",
                "message": "写入结果待核查。请重试确认以核查同一请求；暂不能修改或取消。",
            }
        if record:
            if record.idempotency_key == request_key(
                draft_id, version
            ) and record.fingerprint == fingerprint(draft):
                return ticket_result(record)
            return {"status": "conflict", "message": "创建请求与已有工单内容冲突。"}
        return {"status": "failed", "message": "已核查未创建工单，可重试确认、修改或取消。"}
