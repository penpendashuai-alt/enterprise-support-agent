"""Record scoped browser-session API/DB assertions without exposing service credentials."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot", "stale", "repeat"])
    parser.add_argument("--user", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--approval-from", type=Path)
    args = parser.parse_args()
    if not args.label.replace("-", "").isalnum():
        parser.error("Simple label required")
    output = (
        ROOT / "evaluation/resume-improvement/20261022-r1/demo/browser" / (args.label + ".json")
    )
    if output.exists():
        parser.error("Preserve prior assertions")
    private = json.loads((ROOT / ".cache/resume-20261022/demo.json").read_text(encoding="utf-8"))
    result = {
        "at": datetime.now(UTC).isoformat(),
        "action": args.action,
        "user_id": args.user,
        "thread_id": args.thread,
    }
    with httpx.Client(
        base_url=f"http://127.0.0.1:{private['service_port']}",
        headers={"Authorization": "Bearer " + private["auth_secret"]},
        trust_env=False,
        timeout=60,
    ) as client:
        if args.action != "snapshot":
            if args.approval_from is None:
                parser.error("Approval must come from a saved observed snapshot")
            previous = json.loads(args.approval_from.read_text(encoding="utf-8"))
            assert previous["user_id"] == args.user and previous["thread_id"] == args.thread
            pending = previous["pending"]
            request = {
                "thread_id": args.thread,
                "user_id": args.user,
                "approval": {
                    "draft_id": pending["draft_id"],
                    "draft_version": pending["draft_version"],
                    "action": "approve",
                },
            }
            response = client.post("/support-agent/invoke", json=request)
            result.update(
                request=request, response_status=response.status_code, response=response.json()
            )
        response = client.get(
            "/support-agent/approval", params={"thread_id": args.thread, "user_id": args.user}
        )
        response.raise_for_status()
        result["pending"] = response.json()["pending"]
        response = client.get("/support-agent/tickets", params={"user_id": args.user})
        response.raise_for_status()
        result["tickets_api"] = response.json()
    with psycopg.connect(make_conninfo(**private["connection"]), row_factory=dict_row) as conn:
        result["tickets_database"] = conn.execute(
            "SELECT ticket_id, user_id, thread_id, draft_id, draft_version, idempotency_key, fingerprint, draft FROM tickets WHERE user_id=%s AND thread_id=%s ORDER BY ticket_id",
            (args.user, args.thread),
        ).fetchall()
    checks = {"ticket_count": len(result["tickets_database"]) == args.expected_count}
    if args.expected_version is not None:
        checks["pending_version"] = bool(
            result["pending"] and result["pending"]["draft_version"] == args.expected_version
        )
    if args.action == "stale":
        checks["stale_rejected"] = result["response_status"] == 409
    if args.action == "repeat":
        ticket = result["response"].get("custom_data", {}).get("ticket_result", {}).get("ticket")
        if ticket is None:
            ticket = result["response"].get("custom_data", {}).get("ticket")
        checks["same_persisted_ticket"] = bool(
            ticket
            and result["response_status"] == 200
            and len(result["tickets_database"]) == 1
            and ticket["ticket_id"] == result["tickets_database"][0]["ticket_id"]
        )
    result["checks"] = checks
    result["passed"] = all(checks.values())
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print({"output": str(output), "checks": checks})
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
