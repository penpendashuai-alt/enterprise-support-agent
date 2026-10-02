import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from support_storage.identity import identity
from support_storage.postgres import ticket_from_row
from tickets.models import TicketRecord, fingerprint, request_key
from tickets.repository import TicketConflict


def read_source(path: Path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT ticket_id,draft_id,idempotency_key,record FROM tickets"
        ).fetchall()
    records = []
    seen = [set(), set(), set()]
    for row in rows:
        record = TicketRecord.model_validate_json(row[3])
        values = (record.ticket_id, record.draft_id, record.idempotency_key)
        if (
            not re.fullmatch(r"DEMO-[A-F0-9]{32}", record.ticket_id)
            or values != row[:3]
            or record.fingerprint != fingerprint(record.draft)
            or record.idempotency_key != request_key(record.draft_id, record.draft_version)
        ):
            raise ValueError("Invalid source identity or fingerprint")
        if datetime.fromisoformat(record.created_at).tzinfo is None:
            raise ValueError("Source timestamp must have timezone")
        for value, used in zip(values, seen, strict=True):
            if value in used:
                raise ValueError("Duplicate source identity")
            used.add(value)
        records.append(record)
    return records


def same_import(existing, record, user_id):
    return bool(
        existing
        and existing.user_id == user_id
        and existing.source == "sqlite_import"
        and all(
            getattr(existing, k) == getattr(record, k)
            for k in [
                "ticket_id",
                "draft_id",
                "draft_version",
                "idempotency_key",
                "fingerprint",
                "draft",
                "thread_id",
                "state",
            ]
        )
        and datetime.fromisoformat(existing.created_at) == datetime.fromisoformat(record.created_at)
    )


async def target_record(conn, record):
    rows = await (
        await conn.execute(
            "SELECT * FROM tickets WHERE ticket_id=%s OR draft_id=%s OR idempotency_key=%s",
            (record.ticket_id, record.draft_id, record.idempotency_key),
        )
    ).fetchall()
    if len(rows) > 1:
        raise TicketConflict("Multiple target identities collide")
    return ticket_from_row(rows[0]) if rows else None


async def import_records(pool, records, mapping, *, apply=False):
    for user in mapping.values():
        identity(user)
    report: dict[str, Any] = {
        "mode": "import" if apply else "preflight",
        "source_count": len(records),
        "created": 0,
        "reused": 0,
        "conflicts": 0,
        "unprocessed": 0,
        "new": 0,
        "records": [],
    }
    async with pool.connection() as conn, conn.transaction():
        pending = []
        for record in records:
            user = mapping.get(record.ticket_id)
            status = "unprocessed"
            if user:
                existing = await target_record(conn, record)
                status = (
                    "new"
                    if existing is None
                    else "reused"
                    if same_import(existing, record, user)
                    else "conflicts"
                )
                if record.user_id and record.user_id != user:
                    status = "conflicts"
                if status == "new":
                    pending.append((record, user))
            report[status] += 1
            report["records"].append({"ticket_id": record.ticket_id, "status": status})
        if apply and report["conflicts"]:
            report["status"] = "rejected_conflicts"
            return report
        if apply:
            for record, user in pending:
                await conn.execute(
                    "INSERT INTO users(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user,)
                )
                inserted = await conn.execute(
                    "INSERT INTO tickets(ticket_id,user_id,thread_id,source_thread_id,draft_id,draft_version,idempotency_key,fingerprint,draft,state,created_at,source) VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,'sqlite_import') ON CONFLICT DO NOTHING RETURNING ticket_id",
                    (
                        record.ticket_id,
                        user,
                        record.thread_id,
                        record.draft_id,
                        record.draft_version,
                        record.idempotency_key,
                        record.fingerprint,
                        Jsonb(record.draft.model_dump()),
                        record.state,
                        record.created_at,
                    ),
                )
                if not same_import(await target_record(conn, record), record, user):
                    raise TicketConflict("Concurrent import conflict; batch rolled back")
                report["created" if await inserted.fetchone() else "reused"] += 1
        report["status"] = "complete"
    return report
