"""Run frozen heldout cases adjacently in isolated source processes, never reusing outputs."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from resume_support.budget import AttemptLedger


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--campaign", type=Path, default=Path("evaluation/resume-improvement/20261022-r1")
    )
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--paid", action="store_true")
    p.add_argument("--resume", action="store_true", help="Continue frozen unexecuted items only")
    args = p.parse_args()
    if not args.paid:
        p.error("Explicit --paid required")
    root = args.campaign.resolve()
    plan = root / "heldout/paired-plan.json"
    if plan.exists() and not args.resume:
        raise ValueError("Paired plan already exists; never silently replay heldout")
    if args.resume and not plan.exists():
        raise ValueError("No frozen plan to resume")
    data = json.loads((root / "datasets/agent.json").read_text(encoding="utf-8"))
    tasks = [c for c in data["cases"] if c["split"] == "heldout"]
    order = [
        {"case_id": case["case_id"], "variant": variant}
        for i, case in enumerate(tasks)
        for variant in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline"))
    ]
    sources = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    spec = {
        "status": "frozen",
        "planned": order,
        "dataset_file_sha256": hashlib.sha256(
            (root / "datasets/agent.json").read_bytes()
        ).hexdigest(),
        "source_files": {
            key: {
                p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (source / "src").rglob("*.py")
            }
            for key, source in sources.items()
        },
        "outputs_seen_before_freeze": False,
        "no_more_tuning": True,
    }
    if args.resume:
        if json.loads(plan.read_text(encoding="utf-8")) != spec:
            raise ValueError("Frozen source/dataset/order changed; cannot resume")
    else:
        plan.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    contract = json.loads((root / "calls/contract.json").read_text(encoding="utf-8"))
    ledger = AttemptLedger(root / "calls/attempts.db", contract)
    logroot = Path(".cache/resume-20261022/heldout")
    logroot.mkdir(parents=True, exist_ok=True)
    completed = (
        json.loads((root / "heldout/execution.json").read_text(encoding="utf-8"))["attempted"]
        if args.resume
        else []
    )
    unresolved = []
    for row in completed:
        if row["finished"]:
            continue
        marker = root / "heldout" / row["variant"] / row["case_id"] / "interruption.json"
        if not marker.exists():
            unresolved.append(row)
            continue
        reconciliation = json.loads(marker.read_text(encoding="utf-8"))
        if (
            reconciliation.get("classification") != "infrastructure_error"
            or reconciliation.get("primary_outcome") != "failed_retained_without_replay"
        ):
            unresolved.append(row)
    if (
        unresolved
        or [{"case_id": r["case_id"], "variant": r["variant"]} for r in completed]
        != order[: len(completed)]
    ):
        raise ValueError("Cannot silently retry failed/partial tasks or change execution order")
    for item in order[len(completed) :]:
        if any(e["status"] in {"pending", "unknown"} for e in ledger.events()):
            print("Stopped: unresolved physical request", flush=True)
            break
        free = shutil.disk_usage(Path.home().anchor).free
        if free < 1024**3:
            (root / "heldout/execution.json").write_text(
                json.dumps(
                    {
                        "planned": len(order),
                        "attempted": completed,
                        "stop_reason": "host_system_disk_below_1GiB",
                        "free_bytes": free,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print("Stopped before new task: system disk below 1 GiB", flush=True)
            break
        variant, cid = item["variant"], item["case_id"]
        output = root / "heldout" / variant / cid
        if output.exists():
            marker = output / "startup-interruption.json"
            if (
                not args.resume
                or not marker.exists()
                or any(output.rglob("manifest.json"))
                or any(output.rglob("a-*.json"))
            ):
                raise ValueError("Existing run needs explicit reconciliation; preserve raw outputs")
            interruption = json.loads(marker.read_text(encoding="utf-8"))
            if not interruption.get("no_provider_attempts"):
                raise ValueError("Interrupted attempt must be accounted first")
            output = output / "startup-recovery-1"
        cmd = [
            sys.executable,
            "scripts/run_resume_campaign.py",
            "agent",
            "--paid",
            "--stage",
            "heldout",
            "--split",
            "heldout",
            "--source",
            str(sources[variant]),
            "--campaign",
            str(root),
            "--variant",
            variant,
            "--cases",
            cid,
            "--output",
            str(output),
        ]
        with (logroot / f"{variant}-{cid}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        finished = result.returncode == 0 and (output / "manifest.json").exists()
        if finished:
            report = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            finished = report["status"] == "complete"
        completed.append({**item, "exit_code": result.returncode, "finished": finished})
        (root / "heldout/execution.json").write_text(
            json.dumps({"planned": len(order), "attempted": completed}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {**item, "finished": finished, "progress": len(completed), "planned": len(order)}
            ),
            flush=True,
        )
        if not finished:
            break


if __name__ == "__main__":
    main()
