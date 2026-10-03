import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class RequestTrace:
    deadline: float
    category: str
    request_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=perf_counter)
    run_id: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    locked_thread: str | None = None
    outcome: str = "success"
    status: int = 200

    def record(self):
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "category": self.category,
            "outcome": self.outcome,
            "status": self.status,
            "total_seconds": perf_counter() - self.started,
            "timings": self.timings,
        }


current: ContextVar[RequestTrace | None] = ContextVar("request_trace", default=None)


@contextmanager
def measure(name):
    started = perf_counter()
    try:
        yield
    finally:
        if trace := current.get():
            trace.timings[name] = trace.timings.get(name, 0) + perf_counter() - started


def measured(name, *, asynchronous=True):
    def decorate(function):
        @wraps(function)
        async def async_call(*args, **kwargs):
            with measure(name):
                return await function(*args, **kwargs)

        @wraps(function)
        def sync_call(*args, **kwargs):
            with measure(name):
                return function(*args, **kwargs)

        return async_call if asynchronous else sync_call

    return decorate


class ControlError(Exception):
    def __init__(self, code, status=503, retry_after=1):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)

    @property
    def detail(self):
        return {
            "code": self.code,
            "message": "请求未完成；若涉及工单，请使用原审批请求核查结果，不要重复新建。",
        }
