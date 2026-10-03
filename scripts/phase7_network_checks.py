"""Real network fault scenarios for the isolated deterministic fixture."""

import asyncio
import json
from time import perf_counter
from uuid import uuid4

import httpx


async def run(url, unavailable=False):
    rows = []
    async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=10) as client:

        async def stats():
            return (await client.get("/fixture/stats")).json()

        async def clean():
            for _ in range(100):
                value = await stats()
                if (
                    not value["active_model"]
                    and not value["thread_locks"]
                    and all(not v["present"] and not v["active"] for v in value["control"].values())
                ):
                    return value
                await asyncio.sleep(0.02)
            raise AssertionError("Resources remained occupied")

        if unavailable:
            response = await client.post(
                "/support-agent/invoke", json={"message": "MFA", "user_id": "network"}
            )
            assert (
                response.status_code == 503
                and response.json()["detail"]["code"] == "rate_dependency_unavailable"
            )
            response = await client.get("/support-agent/preferences", params={"user_id": "network"})
            assert response.status_code == 200
            assert (await stats())["model"] == 0
            rows.append(
                {"check": "redis_unreachable_expensive_closed_read_available", "status": "passed"}
            )
            await clean()
            return rows
        response = await client.post("/support-agent/stream", json={"message": "MFA"})
        assert response.status_code == 422 and response.headers["content-type"].startswith(
            "application/json"
        )
        rows.append({"check": "sse_preflight_identity_http422", "status": "passed"})
        thread = uuid4().hex
        async with client.stream(
            "POST",
            "/support-agent/stream",
            json={"message": "SLOW disconnect", "user_id": "network", "thread_id": thread},
        ) as response:
            assert response.status_code == 200
            for _ in range(100):
                if (await stats())["active_model"]:
                    break
                await asyncio.sleep(0.01)
            assert (await stats())["active_model"] == 1
        value = await clean()
        assert any(r["outcome"] == "disconnected" for r in value["completed"])
        rows.append(
            {"check": "real_sse_close_cancels_model_releases_lock_and_capacity", "status": "passed"}
        )
        response = await client.post(
            "/support-agent/invoke",
            json={"message": "MFA", "user_id": "network", "thread_id": thread},
        )
        assert response.status_code == 200
        rows.append({"check": "same_thread_usable_after_disconnect", "status": "passed"})
        request = asyncio.create_task(
            client.post(
                "/support-agent/invoke",
                json={"message": "SLOW invoke disconnect", "user_id": "network"},
            )
        )
        for _ in range(100):
            if (await stats())["active_model"]:
                break
            await asyncio.sleep(0.01)
        request.cancel()
        try:
            await request
        except asyncio.CancelledError:
            pass
        await clean()
        rows.append({"check": "real_invoke_close_releases_resources", "status": "passed"})
        started = perf_counter()
        response = await client.post(
            "/support-agent/stream", json={"message": "SLOW timeout", "user_id": "network"}
        )
        assert (
            response.status_code == 200
            and '"type": "error"' in response.text
            and "request_timeout" in response.text
            and "[DONE]" in response.text
        )
        rows.append(
            {
                "check": "sse_after_headers_structured_deadline_error",
                "seconds": perf_counter() - started,
                "status": "passed",
            }
        )
        await clean()
        shared = uuid4().hex
        slow = asyncio.create_task(
            client.post(
                "/support-agent/invoke",
                json={"message": "SLOW lock", "thread_id": shared, "user_id": "network"},
            )
        )
        await asyncio.sleep(0.1)
        busy = await client.post(
            "/support-agent/invoke",
            json={"message": "MFA", "thread_id": shared, "user_id": "network"},
        )
        assert busy.status_code == 503 and busy.json()["detail"]["code"] == "thread_busy"
        await slow
        await clean()
        rows.append({"check": "thread_wait_timeout_and_cleanup", "status": "passed"})
        start = perf_counter()
        marks = {
            "first_event": None,
            "first_content": None,
            "first_validated_answer": None,
            "end": None,
            "ttft": None,
        }
        async with client.stream(
            "POST", "/support-agent/stream", json={"message": "MFA timing", "user_id": "network"}
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                elapsed = perf_counter() - start
                if marks["first_event"] is None:
                    marks["first_event"] = elapsed
                if line == "data: [DONE]":
                    marks["end"] = elapsed
                    continue
                event = json.loads(line[6:])
                assert event["type"] != "token", "Knowledge must be validated before output"
                if event["type"] == "message" and event["content"].get("content"):
                    marks["first_content"] = marks["first_content"] or elapsed
                    if (
                        event["content"].get("custom_data", {}).get("citation_check")
                        == "valid_numbers"
                    ):
                        marks["first_validated_answer"] = elapsed
        assert marks["first_validated_answer"] and marks["end"]
        rows.append({"check": "validated_answer_sse_timing", "status": "passed", **marks})
        value = await clean()
        rows.append({"check": "final_resources", "status": "passed", "stats": value})
    return rows
