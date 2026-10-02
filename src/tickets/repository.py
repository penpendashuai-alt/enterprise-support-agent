import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from tickets.models import TicketDraft, TicketRecord, fingerprint, request_key


class TicketConflict(ValueError):
    pass


class TicketRepository:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connection(self):
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tickets ("
                "ticket_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE, "
                "idempotency_key TEXT NOT NULL UNIQUE, record TEXT NOT NULL)"
            )
            connection.commit()
            yield connection
        finally:
            connection.close()

    def create(
        self,
        draft: TicketDraft,
        draft_id: str,
        version: int,
        thread_id: str,
        user_id: str | None = None,
    ) -> TicketRecord:
        draft = TicketDraft.model_validate(draft.model_dump())
        key = request_key(draft_id, version)
        if not thread_id.strip():
            raise ValueError("thread_id is required")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record FROM tickets WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row:
                existing = TicketRecord.model_validate_json(row[0])
                if (
                    existing.idempotency_key != key
                    or existing.fingerprint != fingerprint(draft)
                    or existing.thread_id != thread_id
                    or existing.user_id != user_id
                ):
                    raise TicketConflict("Request already exists with different content or context")
                return existing
            record = TicketRecord(
                ticket_id=f"DEMO-{uuid4().hex.upper()}",
                draft_id=draft_id,
                draft_version=version,
                thread_id=thread_id,
                idempotency_key=key,
                fingerprint=fingerprint(draft),
                draft=draft,
                created_at=datetime.now(UTC).isoformat(),
                user_id=user_id,
            )
            connection.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?)",
                (record.ticket_id, draft_id, key, record.model_dump_json()),
            )
            connection.commit()
            return record

    def get(self, ticket_id: str) -> TicketRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT record FROM tickets WHERE ticket_id = ?", (ticket_id.upper(),)
            ).fetchone()
            return TicketRecord.model_validate_json(row[0]) if row else None

    def by_request(self, draft_id: str, thread_id: str) -> TicketRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT record FROM tickets WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            record = TicketRecord.model_validate_json(row[0]) if row else None
            return record if record and record.thread_id == thread_id else None
