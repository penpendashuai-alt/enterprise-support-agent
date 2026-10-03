"""Closed-loop deterministic service benchmark. Reuse this driver across source versions."""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


async def batch(url, size, concurrency, workload, repeat, namespace):
    limiter = asyncio.Semaphore(concurrency)
    rows = []
    async with httpx.AsyncClient(
        base_url=url, trust_env=False, timeout=130, limits=httpx.Limits(max_connections=concurrency)
    ) as client:
        hot_query = f"固定热点 MFA {namespace}"
        if workload == "hot":
            await client.post(
                "/support-agent/invoke", json={"message": hot_query, "user_id": "warmup-user"}
            )
        before = (await client.get("/fixture/stats")).json()
        started = time.perf_counter()

        async def one(i):
            async with limiter:
                query = (
                    hot_query
                    if workload in {"hot", "cold", "same_thread"}
                    else f"问题 {namespace} {i}"
                    if workload == "unique"
                    else f"混合问题 {namespace} {i % 10}"
                )
                thread = namespace + ("-shared" if workload == "same_thread" else f"-{i}")
                t = time.perf_counter()
                response = await client.post(
                    "/support-agent/invoke",
                    json={"message": query, "thread_id": thread, "user_id": "bench-user"},
                )
                body = response.json()
                ok = (
                    response.status_code == 200
                    and body.get("custom_data", {}).get("citation_check") == "valid_numbers"
                )
                rows.append(
                    {
                        "order": i,
                        "query": query,
                        "seconds": time.perf_counter() - t,
                        "status": response.status_code,
                        "success": ok,
                        "detail": body.get("detail"),
                        "timing": response.headers.get("server-timing"),
                        "request_id": response.headers.get("x-request-id"),
                        "retrieval": {
                            k: body.get("custom_data", {}).get(k)
                            for k in ["query", "timings", "usage", "cache"]
                        },
                    }
                )

        await asyncio.gather(*(one(i) for i in range(size)))
        elapsed = time.perf_counter() - started
        after = (await client.get("/fixture/stats")).json()
    successful = sorted(r["seconds"] for r in rows if r["success"])
    return {
        "size": size,
        "concurrency": concurrency,
        "workload": workload,
        "repeat": repeat,
        "warmup": "one unmeasured request"
        if workload == "hot"
        else "none; distinct batch query prefix",
        "driver": "closed loop; client semaphore, keepalive reused; total excludes stats calls",
        "seconds": elapsed,
        "success": len(successful),
        "successful_rps": len(successful) / elapsed,
        "p50_success": successful[int((len(successful) - 1) * 0.5)] if successful else None,
        "p95_success": successful[int((len(successful) - 1) * 0.95)] if successful else None,
        "backend_calls": {
            k: after[k] - before[k] for k in ["model", "embedding", "search", "index"]
        },
        "stats_after": {k: v for k, v in after.items() if k != "completed"},
        "traces": [
            r
            for r in after.get("completed", [])
            if r["request_id"] in {row["request_id"] for row in rows}
        ],
        "requests": sorted(rows, key=lambda r: r["order"]),
    }


def run(args):
    if args.output.exists():
        raise ValueError("Preserve previous benchmark")
    c = json.loads(args.connection_json.read_text(encoding="utf-8"))
    db = "phase7_bench_" + uuid4().hex
    with psycopg.connect(make_conninfo(**c), autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    c["dbname"] = db
    env = os.environ.copy()
    for k, v in c.items():
        env[
            "POSTGRES_"
            + {
                "host": "HOST",
                "port": "PORT",
                "user": "USER",
                "password": "PASSWORD",
                "dbname": "DB",
            }[k]
        ] = str(v)
    env.update(
        PYTHONPATH=str(args.source.resolve() / "src"),
        DATABASE_TYPE="postgres",
        AUTH_SECRET="",
        LANGFUSE_TRACING="false",
        LANGSMITH_TRACING="false",
        REDIS_URL=args.redis_url,
        REDIS_NAMESPACE="esa:p7:" + uuid4().hex,
        RAG_CACHE_ENABLED=str(args.mode in {"cache", "full"}).lower(),
        ADMISSION_ENABLED=str(args.mode in {"control", "full"}).lower(),
        POSTGRES_MAX_CONNECTIONS_PER_POOL=str(args.pool),
    )
    if args.faults:
        env.update(
            REQUEST_TIMEOUT="1.5",
            THREAD_WAIT_TIMEOUT="0.15",
            RATE_USER_CAPACITY="1000",
            RATE_SERVICE_CAPACITY="1000",
        )
    subprocess.run(
        [sys.executable, str(args.source / "scripts/migrate_support.py")],
        env=env,
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env["PHASE7_TEST_PORT"] = str(port)
    with (ROOT / ".cache" / f"{db}.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/phase7_test_server.py")],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        report = {
            "mode": args.mode,
            "source": str(args.source.relative_to(ROOT))
            if args.source.is_relative_to(ROOT)
            else str(args.source),
            "database": db,
            "pool_size_per_role": args.pool,
            "worker_count": 1,
            "model": "deterministic; NOT real LLM throughput",
            "batches": [],
            "source_sha256": {
                str(p.relative_to(args.source)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in (args.source / "src").rglob("*.py")
            },
            "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fixture_sha256": hashlib.sha256(
                (ROOT / "scripts/phase7_test_server.py").read_bytes()
            ).hexdigest(),
            "controls": {
                "execution": 6,
                "execution_queue": 12,
                "model": 3,
                "rate_service": [40, 20],
                "rate_user": [20, 10],
            },
        }
        try:
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(150):
                    if proc.poll() is not None:
                        raise RuntimeError("Fixture exited; inspect private log")
                    try:
                        if client.get(url + "/info").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.2)
            if args.faults:
                from phase7_network_checks import run as network_checks

                report["network_checks"] = asyncio.run(
                    network_checks(url, args.redis_url.endswith(":1/0"))
                )
                print(json.dumps({"network_checks": "passed"}), flush=True)
            for repeat in range(0 if args.faults else args.rounds):
                for size in map(int, args.sizes.split(",")):
                    for concurrency in map(int, args.concurrency.split(",")):
                        for workload in args.workloads.split(","):
                            row = asyncio.run(
                                batch(url, size, concurrency, workload, repeat, uuid4().hex)
                            )
                            report["batches"].append(row)
                            print(
                                json.dumps(
                                    {
                                        k: v
                                        for k, v in row.items()
                                        if k not in {"requests", "stats_after", "traces"}
                                    }
                                ),
                                flush=True,
                            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(15)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=ROOT)
    p.add_argument("--connection-json", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mode", choices=["baseline", "cache", "control", "full"], required=True)
    p.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    p.add_argument("--pool", type=int, default=1)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--sizes", default="50,100")
    p.add_argument("--concurrency", default="5,10,20")
    p.add_argument("--workloads", default="unique,hot,mixed,same_thread")
    p.add_argument("--faults", action="store_true")
    run(p.parse_args())
