import json
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler


def now():
    return datetime.now(UTC).isoformat()


def write(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            sleep(0.02 * 2**attempt)


class BudgetExceeded(RuntimeError):
    pass


class Ledger(BaseCallbackHandler):
    raise_error = True

    def __init__(self, root, run_id, budget, max_calls):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id, self.budget, self.max_calls = run_id, budget, max_calls
        self.case_id, self.turn = "setup", -1
        self.active = {}
        self.exhausted = False

    def events(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*.json")]

    def reserve(self, kind, key=None, reservation=0.5):
        if self.exhausted:
            raise BudgetExceeded("Prior usage is unknown or the campaign budget is exhausted")
        previous = self.events()
        if any(e.get("estimated_cny") is None for e in previous):
            self.exhausted = True
            raise BudgetExceeded("Resolve unknown physical-call usage before starting another call")
        committed = sum(
            e["estimated_cny"] if e.get("estimated_cny") is not None else e["reserved_cny"]
            for e in previous
        )
        if len(previous) >= self.max_calls or committed + reservation > self.budget - 1:
            self.exhausted = True
            raise BudgetExceeded("Phase 8 budget/call guard")
        call_id = str(key or uuid4())
        event = {
            "call_id": call_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "turn": self.turn,
            "provider_kind": kind,
            "started_at": now(),
            "status": "pending",
            "usage": None,
            "estimated_cny": None,
            "reserved_cny": reservation,
        }
        if (self.root / f"{call_id}.json").exists():
            raise ValueError("Duplicate physical call ID")
        write(self.root / f"{call_id}.json", event)
        self.active[call_id] = event
        return call_id

    def finish(self, key, usage=None, text=None, error=None):
        event = self.active.pop(str(key))
        cost = None
        if usage:
            cost = (
                (usage.get("input_tokens", 0) * 9 + usage.get("output_tokens", 0) * 27) / 1_000_000
                if event["provider_kind"] == "chat"
                else usage["total_tokens"] * 0.5 / 1_000_000
            )
        event.update(
            status="error" if error else "complete" if usage else "unknown_usage",
            usage=usage,
            estimated_cny=cost,
            finished_at=now(),
            visible_model_output=text,
            error_type=error,
        )
        write(self.root / f"{key}.json", event)
        if usage is None:
            self.exhausted = True

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        length = sum(len(str(m.content).encode("utf-8")) for batch in messages for m in batch)
        self.reserve(
            "chat", run_id, max(0.5, (length + 20000) * 9 / 1_000_000 + 1800 * 27 / 1_000_000)
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = response.generations[0][0].message
        self.finish(run_id, getattr(message, "usage_metadata", None), text=message.content)

    def on_llm_error(self, error, *, run_id, **kwargs):
        if str(run_id) in self.active:
            self.finish(run_id, error=type(error).__name__)

    def patch_embeddings(self):
        from rag.embeddings import Embeddings

        original = Embeddings.__init__
        tracker = self

        def initialize(instance, *args, **kwargs):
            original(instance, *args, **kwargs)
            create = instance.client.embeddings.create

            async def observed(**request):
                key = tracker.reserve("embedding", reservation=0.02)
                try:
                    result = await create(**request)
                except BaseException as exc:
                    tracker.finish(key, error=type(exc).__name__)
                    raise
                tracker.finish(key, {"total_tokens": result.usage.total_tokens})
                return result

            instance.client.embeddings.create = observed

        Embeddings.__init__ = initialize
        return lambda: setattr(Embeddings, "__init__", original)
