import argparse
import asyncio
import json
from pathlib import Path

from run_phase3_scenarios import run as scenarios
from run_phase4_scenarios import Usage

import agents.support_agent as support
from core import settings


async def run(output):
    if output.exists():
        raise ValueError("Preserve existing scenario report")
    if settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
        raise ValueError("Unsupported cost contract")
    prior = sum(
        data.get("estimated_cny", 0)
        for p in output.parent.glob("*.json")
        if isinstance(data := json.loads(p.read_text(encoding="utf-8")), dict)
    )
    tracker = Usage(50, prior)
    original = support.get_support_model
    model = original({}).model_copy(
        update={
            "callbacks": [tracker],
            "temperature": 0,
            "max_tokens": 1600,
            "max_retries": 0,
            "stream_usage": True,
        }
    )
    support.get_support_model = lambda _: model
    try:
        report = await scenarios(output)
        report["phase5_note"] = (
            "Real-model Phase 3 approval regression against Phase 5 code; temporary SQLite; old L3 identifiers intentionally preserved"
        )
        report["usage"] = {
            "chat_input_tokens": sum(r["input_tokens"] for r in tracker.calls),
            "chat_output_tokens": sum(r["output_tokens"] for r in tracker.calls),
            "calls": len(tracker.calls),
        }
        report["estimated_cny"] = tracker.estimated_cny
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        support.get_support_model = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
