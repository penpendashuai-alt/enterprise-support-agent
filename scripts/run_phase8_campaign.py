"""Run the frozen heldout family pairs serially, without inspecting answers."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from eval_support.ledger import now, write
from eval_support.schema import digest, load_dataset


def hashes(root):
    return {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "src").rglob("*.py")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true", required=True)
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 < args.budget <= 30 or args.output.exists():
        raise ValueError("Authorized budget and fresh campaign directory required")
    root = Path(__file__).resolve().parents[1]
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    dataset = load_dataset(root / "evaluation/datasets/agent_v1.json")
    if freeze["dataset_sha256"] != digest(dataset.model_dump()):
        raise ValueError("Dataset changed after freeze")
    for variant, source in [("baseline", root / ".cache/phase8-baseline"), ("candidate", root)]:
        if freeze[variant + "_source_sha256"] != hashes(source):
            raise ValueError("Source changed after freeze")
    for relative, expected in freeze["evaluator_sha256"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Evaluator changed after freeze")
    args.output.mkdir(parents=True)
    journal = {
        "started_at": now(),
        "freeze_sha256": hashlib.sha256(args.freeze.read_bytes()).hexdigest(),
        "order": freeze["heldout_order"],
        "completed": [],
        "status": "running",
    }
    write(args.output / "campaign.json", journal)
    for step in freeze["heldout_order"]:
        name = step["variant"] + "-" + step["family"]
        command = [
            sys.executable,
            str(root / "scripts/evaluate_agent.py"),
            "--paid",
            "--budget",
            str(args.budget),
            "--variant",
            step["variant"],
            "--split",
            "heldout",
            "--cases",
            ",".join(step["cases"]),
            "--output",
            str(args.output / name),
        ]
        if step["variant"] == "baseline":
            command += ["--source", str(root / ".cache/phase8-baseline")]
        with (args.output / (name + ".log")).open("w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        manifest_path = args.output / name / "manifest.json"
        complete = (
            result.returncode == 0
            and manifest_path.exists()
            and json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "complete"
        )
        journal["completed"].append({"name": name, "complete": complete, "finished_at": now()})
        write(args.output / "campaign.json", journal)
        print(json.dumps({"run": name, "complete": complete}), flush=True)
        if not complete:
            break
    journal.update(
        status="complete"
        if len(journal["completed"]) == len(journal["order"])
        and all(r["complete"] for r in journal["completed"])
        else "incomplete",
        finished_at=now(),
    )
    write(args.output / "campaign.json", journal)


if __name__ == "__main__":
    main()
