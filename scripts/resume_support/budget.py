"""Physical HTTP attempt ledger, with atomic cross-process reservations.

Prices are a frozen campaign contract, not a claim about the provider invoice.
Input cache discounts and off-peak discounts are deliberately not deducted.
No credentials, headers, input text or provider error bodies enter this ledger.
"""

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


class BudgetStop(RuntimeError):
    """No more network attempts may be sent under this contract."""


def timestamp():
    return datetime.now(UTC).isoformat()


class AttemptLedger:
    def __init__(self, path: Path, contract: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.contract = contract
        canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        self.contract_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS contract (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts "
                "(id TEXT PRIMARY KEY, stage TEXT, kind TEXT, state TEXT, "
                "reserved REAL, cost REAL, event TEXT)"
            )
            previous = db.execute("SELECT data FROM contract WHERE id=1").fetchone()
            if previous and previous[0] != canonical:
                raise BudgetStop("Budget contract changed; never reset a campaign ledger")
            db.execute("INSERT OR IGNORE INTO contract VALUES (1, ?)", (canonical,))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def events(self):
        with self.transaction() as db:
            return [
                json.loads(r[0]) for r in db.execute("SELECT event FROM attempts ORDER BY rowid")
            ]

    def reserve(self, stage, kind, reservation, metadata):
        if not math.isfinite(reservation) or reservation <= 0:
            raise BudgetStop("Reservation must be positive and finite")
        if stage not in self.contract["stages"] or kind not in self.contract["providers"]:
            raise BudgetStop("Unknown stage/provider")
        with self.transaction() as db:
            rows = db.execute("SELECT stage,kind,state,reserved,cost FROM attempts").fetchall()
            if any(r[2] == "unknown" for r in rows):
                raise BudgetStop("Unknown usage must be reconciled before any further attempt")
            spent = sum(r[4] if r[4] is not None else r[3] for r in rows)
            stage_spent = sum(r[4] if r[4] is not None else r[3] for r in rows if r[0] == stage)
            if spent + reservation > self.contract["total_cny"]:
                raise BudgetStop("Campaign monetary ceiling")
            if stage_spent + reservation > self.contract["stages"][stage]:
                raise BudgetStop("Stage monetary ceiling")
            provider = self.contract["providers"][kind]
            if sum(r[1] == kind for r in rows) >= provider["max_attempts"]:
                raise BudgetStop("Physical attempt ceiling")
            # Until load experiments are explicitly enabled, even independent processes
            # cannot overlap paid requests. Pending after a crash therefore blocks resume.
            if any(r[2] == "pending" for r in rows) and not self.contract["allow_concurrent"]:
                raise BudgetStop("Pending attempt: wait or reconcile interrupted run")
            key = uuid4().hex
            event = {
                **metadata,
                "call_id": key,
                "stage": stage,
                "provider_kind": kind,
                "started_at": timestamp(),
                "status": "pending",
                "reserved_cny": reservation,
                "estimated_cny": None,
                "usage": None,
                "price_contract_sha256": self.contract_hash,
            }
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?)",
                (key, stage, kind, "pending", reservation, None, json.dumps(event)),
            )
            return key

    def finish(self, key, usage=None, status_code=None, error_type=None):
        with self.transaction() as db:
            row = db.execute("SELECT event FROM attempts WHERE id=?", (key,)).fetchone()
            if not row:
                raise BudgetStop("Unknown attempt")
            event = json.loads(row[0])
            if event["status"] != "pending":
                raise BudgetStop("Attempt already settled")
            price = self.contract["providers"][event["provider_kind"]]
            cost = None
            required = (
                ("prompt_tokens", "completion_tokens")
                if event["provider_kind"] == "chat"
                else ("total_tokens",)
            )
            if isinstance(usage, dict) and all(
                isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0
                for k in required
            ):
                input_tokens = usage[required[0]]
                output_tokens = usage.get("completion_tokens", 0)
                cost = (
                    input_tokens * price["input_per_million_cny"]
                    + output_tokens * price["output_per_million_cny"]
                ) / 1_000_000
            state = "settled" if cost is not None and cost <= event["reserved_cny"] else "unknown"
            event.update(
                status=state,
                estimated_cny=cost,
                usage=usage,
                http_status=status_code,
                error_type=error_type,
                finished_at=timestamp(),
                accounting="peak/cache-miss upper estimate; not provider invoice",
            )
            db.execute(
                "UPDATE attempts SET state=?,cost=?,event=? WHERE id=?",
                (state, cost, json.dumps(event), key),
            )

    def export(self, path):
        Path(path).write_text(
            json.dumps(self.events(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


class MeteredTransport(httpx.AsyncBaseTransport):
    """Wrap the transport, so SDK retries and application retries are both counted.

    Evaluation buffers the provider response, including SSE usage. Therefore no
    TTFT claim may be made with this transport. End-to-end completion is measured.
    """

    def __init__(self, ledger, stage, kind, context, inner=None):
        self.ledger, self.stage, self.kind, self.context = ledger, stage, kind, context
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        price = self.ledger.contract["providers"][self.kind]
        if request.url.scheme != "https" or request.url.host != price["host"]:
            raise BudgetStop("Unpriced endpoint")
        if request.method != "POST" or request.url.path != price["path"]:
            raise BudgetStop("Unpriced operation")
        payload = json.loads(request.content)
        if payload.get("model") != price["model"]:
            raise BudgetStop("Unpriced model")
        if self.kind == "chat":
            maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
            if not isinstance(maximum, int) or not 0 < maximum <= price["max_output_tokens"]:
                raise BudgetStop("Missing/excess output cap")
            if payload.get("n", 1) != 1:
                raise BudgetStop("Multiple completions not in contract")
        else:
            maximum = 0
        # UTF-8 request bytes plus protocol overhead conservatively bound byte-BPE
        # inputs. Unexpected excess usage stops subsequent calls at settlement.
        reserve = (
            (len(request.content) + 4096) * price["input_per_million_cny"]
            + maximum * price["output_per_million_cny"]
        ) / 1_000_000
        key = self.ledger.reserve(self.stage, self.kind, reserve, dict(self.context()))
        response = None
        try:
            response = await self.inner.handle_async_request(request)
            body = await response.aread()
            usage = None
            try:
                if "text/event-stream" in response.headers.get("content-type", ""):
                    for line in body.decode().splitlines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            usage = json.loads(line[6:]).get("usage") or usage
                else:
                    usage = json.loads(body).get("usage")
            except (ValueError, AttributeError, UnicodeError):
                pass
            self.ledger.finish(key, usage, response.status_code)
            # aread() decoded content encodings already. Keeping gzip here would
            # make the SDK decode the JSON/SSE a second time.
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=body,
                extensions=response.extensions,
            )
        except BaseException as exc:
            # If settlement itself failed, retain pending; never invent zero usage.
            if response is None or not response.is_stream_consumed:
                self.ledger.finish(key, error_type=type(exc).__name__)
            raise
        finally:
            if response is not None:
                await response.aclose()

    async def aclose(self):
        await self.inner.aclose()
