"""Initialize evidence once. Does not load .env or call model APIs."""

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "evaluation/resume-improvement/20261022-r1"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if (CAMPAIGN / "manifest.json").exists():
        raise SystemExit("Campaign already initialized; preserve baseline")
    for name in (
        "datasets",
        "baseline",
        "development",
        "heldout",
        "performance",
        "demo",
        "reviews",
        "calls",
        "derived",
    ):
        (CAMPAIGN / name).mkdir(parents=True, exist_ok=True)
    archive = ROOT / ".cache/resume-20261022/baseline.zip"
    source = archive.parent / "baseline"
    with ZipFile(archive) as z:
        z.extractall(source)
    history = {}
    for directory in ("evaluation", "benchmarks"):
        for p in (source / directory).rglob("*"):
            if p.is_file():
                history[p.relative_to(source).as_posix()] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
    write(CAMPAIGN / "baseline/historical-artifacts.sha256.json", history)
    write(
        CAMPAIGN / "manifest.json",
        {
            "campaign_id": CAMPAIGN.name,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "preparing",
            "baseline_git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "baseline_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "preexisting_untracked": ["tmp/"],
            "python": sys.version,
            "platform": platform.platform(),
            "cpu": "AMD Ryzen 7 6800H, 16 logical processors",
            "physical_memory_bytes": 16312639488,
            "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
            "worker_count": 1,
            "plan": {
                "retrieval": {"dev": 60, "heldout": 60, "answerable_per_split": 45},
                "agent": {"dev": 40, "heldout": 40},
                "max_candidate_iterations": 2,
            },
            "authorization": "User approved total CNY 60 in this conversation on 2026-10-22; no publication authorization",
            "stages": {
                "A": "in_progress",
                "B": "in_progress",
                "C": "not_started",
                "D": "not_started",
                "E": "not_started",
                "F": "not_started",
            },
            "real_model_results": "not_run",
            "review_independence": "Same assistant designs, implements and reviews. No independent annotation or claim of human interview readiness.",
        },
    )
    write(
        CAMPAIGN / "calls/contract.json",
        {
            "version": "physical-http-v1",
            "authorized_on": "2026-10-22",
            "total_cny": 60,
            "allow_concurrent": False,
            "stages": {
                "pilot": 3,
                "baseline": 10,
                "development": 12,
                "heldout": 10,
                "performance": 22,
                "demo": 3,
            },
            "providers": {
                "chat": {
                    "host": "api.deepseek.com",
                    "path": "/chat/completions",
                    "model": "deepseek-v4-pro",
                    "input_per_million_cny": 9,
                    "output_per_million_cny": 27,
                    "max_attempts": 1200,
                    "max_output_tokens": 1800,
                },
                "embedding": {
                    "host": "dashscope.aliyuncs.com",
                    "path": "/compatible-mode/v1/embeddings",
                    "model": "text-embedding-v3",
                    "input_per_million_cny": 0.5,
                    "output_per_million_cny": 0,
                    "max_attempts": 700,
                },
            },
            "price_checked_on": "2026-10-22",
            "price_sources": [
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "https://help.aliyun.com/zh/model-studio/embedding",
            ],
            "estimate_scope": "Peak/cache-miss upper estimate, no free-credit assumptions; not an invoice. Unknown usage stops future attempts.",
            "chat_retries": 0,
            "paid_rerank_calls": 0,
            "paid_judge_calls": 0,
        },
    )
    print(
        json.dumps(
            {"campaign": str(CAMPAIGN), "historical_files_frozen": len(history), "paid_calls": 0}
        )
    )


if __name__ == "__main__":
    main()
