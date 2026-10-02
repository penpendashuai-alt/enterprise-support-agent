import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from support_storage.repository import OwnershipConflict
from tickets.models import TicketRecord
from tickets.repository import TicketConflict, TicketRepository


class SQLiteRepository:
    def __init__(self, path):
        self.path = path
        self.legacy = TicketRepository(path)
        self.ready = False
        self.lock = asyncio.Lock()

    def _setup(self):
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 1); CREATE TABLE IF NOT EXISTS support_sessions(thread_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,agent_id TEXT NOT NULL,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL); CREATE INDEX IF NOT EXISTS support_sessions_user_activity ON support_sessions(user_id,agent_id,updated_at DESC);"
            )

    async def _run(self, fn, *args):
        if not self.ready:
            async with self.lock:
                if not self.ready:
                    await asyncio.to_thread(self._setup)
                    self.ready = True
        return await asyncio.to_thread(fn, *args)

    def _session(self, thread_id):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM support_sessions WHERE thread_id=?", (thread_id,)
            ).fetchone()
            return dict(row) if row else None

    async def session(self, thread_id):
        return await self._run(self._session, thread_id)

    def _register(self, user_id, thread_id, title):
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id,created_at) VALUES (?,?)", (user_id, now)
            )
            conn.execute(
                "INSERT OR IGNORE INTO support_sessions VALUES (?,?,'support-agent',?,?,?)",
                (thread_id, user_id, title[:60], now, now),
            )
            row = conn.execute(
                "SELECT * FROM support_sessions WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row["user_id"] != user_id:
                raise OwnershipConflict("Thread belongs to a different user")
            conn.execute(
                "UPDATE support_sessions SET updated_at=?,title=CASE WHEN title='' THEN ? ELSE title END WHERE thread_id=?",
                (now, title[:60], thread_id),
            )
            return dict(row)

    async def register(self, user_id, thread_id, title):
        return await self._run(self._register, user_id, thread_id, title)

    def _sessions(self, user_id, limit):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM support_sessions WHERE user_id=? AND agent_id='support-agent' ORDER BY updated_at DESC,thread_id LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            ]

    async def sessions(self, user_id, limit):
        return await self._run(self._sessions, user_id, limit)

    async def create(self, draft, draft_id, version, thread_id, user_id):
        row = await self.session(thread_id)
        if not row or row["user_id"] != user_id:
            raise TicketConflict("Session ownership mismatch")
        return await self._run(self.legacy.create, draft, draft_id, version, thread_id, user_id)

    async def get(self, ticket_id, user_id):
        record = await self._run(self.legacy.get, ticket_id)
        return record if record and record.user_id == user_id else None

    async def by_request(self, draft_id, thread_id, user_id):
        record = await self._run(self.legacy.by_request, draft_id, thread_id)
        if record and record.user_id != user_id:
            raise TicketConflict("Request context mismatch")
        return record

    def _tickets(self, user_id, limit):
        with self.legacy.connection() as conn:
            records = [
                TicketRecord.model_validate_json(r[0])
                for r in conn.execute("SELECT record FROM tickets").fetchall()
            ]
            return sorted(
                (r for r in records if r.user_id == user_id),
                key=lambda r: r.created_at,
                reverse=True,
            )[:limit]

    async def tickets(self, user_id, limit):
        return await self._run(self._tickets, user_id, limit)
