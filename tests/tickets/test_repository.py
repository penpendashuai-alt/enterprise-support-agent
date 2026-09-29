import asyncio
from uuid import uuid4

import pytest

from tickets.models import TicketDraft
from tickets.repository import TicketConflict, TicketRepository
from tickets.service import create_ticket


@pytest.mark.asyncio
async def test_concurrent_idempotency_reopen_and_conflicts(tmp_path):
    path = str(tmp_path / "nested" / "tickets.db")
    repo = TicketRepository(path)
    draft = TicketDraft(title="VPN", description="809", impact="本人")
    draft_id = f"draft-{uuid4().hex}"
    records = await asyncio.gather(
        *(asyncio.to_thread(repo.create, draft, draft_id, 1, "thread") for _ in range(12))
    )
    assert len({r.ticket_id for r in records}) == 1
    assert TicketRepository(path).get(records[0].ticket_id) == records[0]
    assert repo.by_request(draft_id, "wrong-thread") is None
    for changed, version, thread in [
        (draft.model_copy(update={"priority": "P1"}), 1, "thread"),
        (draft, 2, "thread"),
        (draft, 1, "other"),
    ]:
        with pytest.raises(TicketConflict):
            repo.create(changed, draft_id, version, thread)
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["before_commit", "after_commit", "unknown"])
async def test_storage_failure_reconciliation(monkeypatch, tmp_path, failure):
    repo = TicketRepository(str(tmp_path / "tickets.db"))
    original = repo.create

    def failing(*args):
        if failure == "after_commit":
            original(*args)
        raise OSError("simulated response failure")

    monkeypatch.setattr(repo, "create", failing)
    if failure == "unknown":
        monkeypatch.setattr(repo, "by_request", lambda *args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("tickets.service.repository", lambda: repo)
    result = await create_ticket(
        TicketDraft(title="VPN", description="809", impact="本人"),
        f"draft-{uuid4().hex}",
        1,
        "thread",
    )
    assert (
        result["status"]
        == {"before_commit": "failed", "after_commit": "success", "unknown": "unknown"}[failure]
    )
