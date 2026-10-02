from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from support_storage.repository import OwnershipConflict
from tickets.models import TicketDraft, TicketRecord, fingerprint, request_key
from tickets.repository import TicketConflict


def ticket_from_row(row):
    return (
        TicketRecord(
            **{
                **row,
                "thread_id": row["source_thread_id"],
                "created_at": row["created_at"].isoformat(),
            }
        )
        if row
        else None
    )


class PostgresRepository:
    def __init__(self, pool):
        self.pool = pool

    async def session(self, thread_id):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM support_sessions WHERE thread_id=%s", (thread_id,)
                )
            ).fetchone()

    async def register(self, user_id, thread_id, title):
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO users(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,)
            )
            await conn.execute(
                "INSERT INTO support_sessions(thread_id,user_id,agent_id,title) VALUES (%s,%s,'support-agent',%s) ON CONFLICT DO NOTHING",
                (thread_id, user_id, title[:60]),
            )
            row = await (
                await conn.execute(
                    "SELECT * FROM support_sessions WHERE thread_id=%s FOR UPDATE", (thread_id,)
                )
            ).fetchone()
            if row["user_id"] != user_id:
                raise OwnershipConflict("Thread belongs to a different user")
            await conn.execute(
                "UPDATE support_sessions SET updated_at=now(), title=CASE WHEN title='' THEN %s ELSE title END WHERE thread_id=%s",
                (title[:60], thread_id),
            )
            return row

    async def sessions(self, user_id, limit):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM support_sessions WHERE user_id=%s AND agent_id='support-agent' ORDER BY updated_at DESC,thread_id LIMIT %s",
                    (user_id, limit),
                )
            ).fetchall()

    async def create(self, draft, draft_id, version, thread_id, user_id):
        draft = TicketDraft.model_validate(draft.model_dump())
        record = TicketRecord(
            ticket_id=f"DEMO-{uuid4().hex.upper()}",
            user_id=user_id,
            thread_id=thread_id,
            draft_id=draft_id,
            draft_version=version,
            idempotency_key=request_key(draft_id, version),
            fingerprint=fingerprint(draft),
            draft=draft,
            created_at=datetime.now(UTC).isoformat(),
            source="postgres",
        )
        async with self.pool.connection() as conn, conn.transaction():
            owner = await (
                await conn.execute(
                    "SELECT user_id FROM support_sessions WHERE thread_id=%s", (thread_id,)
                )
            ).fetchone()
            if not owner or owner["user_id"] != user_id:
                raise TicketConflict("Session ownership mismatch")
            await conn.execute(
                "INSERT INTO tickets(ticket_id,user_id,thread_id,source_thread_id,draft_id,draft_version,idempotency_key,fingerprint,draft,state,created_at,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    record.ticket_id,
                    user_id,
                    thread_id,
                    thread_id,
                    draft_id,
                    version,
                    record.idempotency_key,
                    record.fingerprint,
                    Jsonb(draft.model_dump()),
                    record.state,
                    record.created_at,
                    record.source,
                ),
            )
            existing = ticket_from_row(
                await (
                    await conn.execute("SELECT * FROM tickets WHERE draft_id=%s", (draft_id,))
                ).fetchone()
            )
            if not existing or any(
                getattr(existing, k) != getattr(record, k)
                for k in [
                    "user_id",
                    "thread_id",
                    "draft_version",
                    "idempotency_key",
                    "fingerprint",
                    "source",
                ]
            ):
                raise TicketConflict("Request already exists with different content or context")
            return existing

    async def get(self, ticket_id, user_id):
        async with self.pool.connection() as conn:
            return ticket_from_row(
                await (
                    await conn.execute(
                        "SELECT * FROM tickets WHERE ticket_id=%s AND user_id=%s",
                        (ticket_id.upper(), user_id),
                    )
                ).fetchone()
            )

    async def by_request(self, draft_id, thread_id, user_id):
        async with self.pool.connection() as conn:
            record = ticket_from_row(
                await (
                    await conn.execute("SELECT * FROM tickets WHERE draft_id=%s", (draft_id,))
                ).fetchone()
            )
            if record and (
                record.user_id != user_id
                or record.thread_id != thread_id
                or record.source != "postgres"
            ):
                raise TicketConflict("Request context mismatch")
            return record

    async def tickets(self, user_id, limit):
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM tickets WHERE user_id=%s ORDER BY created_at DESC,ticket_id LIMIT %s",
                    (user_id, limit),
                )
            ).fetchall()
            return [record for row in rows if (record := ticket_from_row(row)) is not None]
