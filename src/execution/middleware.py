import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import JSONResponse

from core import settings
from execution.control import runtime
from execution.telemetry import ControlError, RequestTrace, current, logger, measure
from schema import ChatHistoryInput, UserInput
from support_storage.identity import guard_thread, identity


class SupportExecutionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path.startswith("/support-agent/")
            or path in {"/invoke", "/stream", "/history", "/threads"}
        ):
            return await self.app(scope, receive, send)
        started = perf_counter()
        trace = RequestTrace(deadline=started + settings.REQUEST_TIMEOUT, category="read")
        token = current.set(trace)
        begun = finished = False
        monitor = None
        try:
            if settings.AUTH_SECRET:
                headers = dict(scope.get("headers", []))
                authorization = headers.get(b"authorization", b"").decode()
                scheme, _, credential = authorization.partition(" ")
                if scheme.lower() != "bearer" or not secrets.compare_digest(
                    credential, settings.AUTH_SECRET.get_secret_value()
                ):
                    raise HTTPException(401, "Unauthorized")
            body = b""
            async with asyncio.timeout(min(settings.REQUEST_TIMEOUT, 10)):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        raise asyncio.CancelledError
                    body += event.get("body", b"")
                    if len(body) > 65536:
                        raise HTTPException(413, "Request too large")
                    if not event.get("more_body", False):
                        break
            params = parse_qs(scope.get("query_string", b"").decode())
            thread = None
            if path.endswith(("/invoke", "/stream")):
                value = UserInput.model_validate_json(body)
                user = identity(value.user_id)
                thread = value.thread_id or str(uuid4())
                data = json.loads(body)
                data["thread_id"] = thread
                body = json.dumps(data).encode()
                if value.agent_config:
                    raise HTTPException(422, "Support Agent does not accept agent_config overrides")
                trace.category = "approval" if value.approval else "model"
            elif path.endswith("/history"):
                value = ChatHistoryInput.model_validate_json(body)
                user, thread = identity(value.user_id), value.thread_id
            else:
                user = identity(params.get("user_id", [None])[0])
                thread = params.get("thread_id", [None])[0]
            scope = {
                **scope,
                "headers": [
                    (k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"
                ]
                + [(b"content-length", str(len(body)).encode())],
            }
            delivered = False
            queue = asyncio.Queue(maxsize=1)
            parent = asyncio.current_task()
            assert parent is not None

            async def watch():
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        if not finished:
                            trace.outcome = "disconnected"
                            parent.cancel()
                        return
                    await queue.put(event)

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await queue.get()

            async def output(event):
                nonlocal begun, finished
                if event["type"] == "http.response.start":
                    begun = True
                    trace.status = event["status"]
                    if trace.status >= 400:
                        trace.outcome = "http_error"
                    event["headers"] = [
                        *event.get("headers", []),
                        (b"x-request-id", trace.request_id.encode()),
                        (
                            b"server-timing",
                            ", ".join(
                                f"{k};dur={v * 1000:.3f}" for k, v in trace.timings.items()
                            ).encode(),
                        ),
                    ]
                if event["type"] == "http.response.body" and not event.get("more_body", False):
                    finished = True
                with measure("output_send"):
                    await send(event)

            @asynccontextmanager
            async def admission():
                if not settings.ADMISSION_ENABLED:
                    yield
                    return
                from service.support import execution_lock

                capacity = runtime().classes[trace.category]
                async with capacity.reserve():
                    await runtime().rate(user, trace.category)
                    async with execution_lock("support-agent", thread):
                        trace.locked_thread = thread
                        async with capacity.execute():
                            if thread:
                                with measure("ownership_preflight"):
                                    await guard_thread(
                                        thread,
                                        user,
                                        "support-agent",
                                        allow_new=trace.category == "model",
                                    )
                            yield

            monitor = asyncio.create_task(watch())
            async with (
                asyncio.timeout_at(trace.deadline if settings.ADMISSION_ENABLED else None),
                admission(),
            ):
                with measure("execution"):
                    await self.app(scope, replay, output)
        except asyncio.CancelledError:
            if trace.outcome != "disconnected":
                trace.outcome = "cancelled"
            raise
        except (ControlError, TimeoutError, HTTPException, ValidationError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, ControlError)
                else ControlError("request_timeout", 504)
                if isinstance(exc, TimeoutError)
                else ControlError("invalid_request", 422)
            )
            status = exc.status_code if isinstance(exc, HTTPException) else error.status
            detail = exc.detail if isinstance(exc, HTTPException) else error.detail
            trace.status, trace.outcome = status, error.code
            if not begun:
                await JSONResponse(
                    {"detail": detail},
                    status_code=status,
                    headers={
                        "Retry-After": str(error.retry_after),
                        "X-Request-ID": trace.request_id,
                    },
                )(scope, receive, send)
            elif not finished:
                payload = (
                    "data: "
                    + json.dumps({"type": "error", "content": detail})
                    + "\n\ndata: [DONE]\n\n"
                )
                await send(
                    {"type": "http.response.body", "body": payload.encode(), "more_body": False}
                )
        except Exception:
            trace.status, trace.outcome = 500, "service_error"
            raise
        finally:
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            record = trace.record()
            logger.info("support_request %s", json.dumps(record))
            runtime().completed.append(record)
            current.reset(token)
