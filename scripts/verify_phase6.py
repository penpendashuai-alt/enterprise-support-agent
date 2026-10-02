"""Offline Phase 5 boundary and publication audit; prints no secret values."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def prompt(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "HANDLER_PROMPT" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("Handler prompt missing")


def run(output):
    if output.exists():
        raise ValueError("Preserve previous verification record")
    baseline = json.loads(
        (ROOT / "evaluation/baselines/phase5/manifest.json").read_text(encoding="utf-8")
    )
    frozen = (
        "src/rag/",
        "data/knowledge/",
        "evaluation/datasets/",
        "evaluation/results/phase4/",
        "evaluation/results/phase5/",
    )
    checked = 0
    for name, expected in baseline["files"].items():
        if name.startswith(frozen):
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
            checked += 1
    original = subprocess.check_output(
        ["git", "show", baseline["commit"] + ":src/agents/support_agent.py"], cwd=ROOT
    ).decode("utf-8")
    assert prompt(original) == prompt(
        (ROOT / "src/agents/support_agent.py").read_text(encoding="utf-8")
    )
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode("utf-8")
        .split("\0")
    )
    names = sorted(set(n for n in names if n))
    assert not any(n.startswith(("personal/", ".cache/")) or n == ".env" for n in names)
    secrets = [
        v
        for k, v in dotenv_values(ROOT / ".env").items()
        if re.search(r"KEY|TOKEN|PASSWORD|SECRET", k) and v and len(v) >= 12
    ]
    for name in names:
        path = ROOT / name
        if path.is_file() and path.suffix not in {".zip", ".gz", ".png", ".jpg", ".mp4"}:
            data = path.read_bytes()
            assert not any(s.encode() in data for s in secrets), f"Private credential in {name}"
    sources = [
        n
        for n in names
        if n.startswith(("src/", "scripts/", "tests/", "migrations/"))
        and n.endswith((".py", ".sql"))
    ]
    report = {
        "status": "passed",
        "baseline_commit": baseline["commit"],
        "frozen_files_unchanged": checked,
        "handler_prompt_unchanged": True,
        "private_files_excluded": True,
        "local_credentials_not_in_publishable_text": True,
        "source_hash_format": "SHA256 UTF-8 bytes with CRLF normalized to LF",
        "source_sha256_lf": {
            n: hashlib.sha256((ROOT / n).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for n in sources
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_sha256_lf"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
