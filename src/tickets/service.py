from support_storage.identity import identity
from support_storage.runtime import repository
from tickets.models import TicketDraft, fingerprint, request_key, ticket_result
from tickets.repository import TicketConflict


async def create_ticket(
    draft: TicketDraft, draft_id: str, version: int, thread_id: str, user_id: str
) -> dict:
    user_id = identity(user_id)
    repo = repository()
    try:
        record = await repo.create(draft, draft_id, version, thread_id, user_id)
        return ticket_result(record)
    except TicketConflict:
        return {"status": "conflict", "message": "创建请求与已保存内容冲突，未覆盖已有工单。"}
    except Exception:
        # Business commit and graph checkpoint are separate transactions.
        try:
            record = await repo.by_request(draft_id, thread_id, user_id)
        except TicketConflict:
            return {"status": "conflict", "message": "创建请求的归属或内容与已有工单冲突。"}
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
