"""Validate offline by default; paid experiments require explicit mode and budget."""

import argparse
import asyncio
import sys
from pathlib import Path

from eval_support.schema import check_sources, load_dataset

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/agent_v1.json")
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--split", choices=["dev", "heldout", "regression"], default="dev")
    parser.add_argument("--cases", help="Explicit comma-separated IDs; selection is recorded")
    parser.add_argument(
        "--layer", choices=["agent", "fixed_evidence", "retrieval"], default="agent"
    )
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--budget", type=float)
    parser.add_argument("--max-calls", type=int, default=1200)
    parser.add_argument("--ledger", type=Path, default=ROOT / "evaluation/phase8/calls")
    parser.add_argument(
        "--connection-json", type=Path, default=ROOT / ".cache/phase6-pg/connection.json"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--evidence", type=Path, help="Recorded raw-query component directory")
    args = parser.parse_args()
    args.source = args.source.resolve()
    dataset = load_dataset(args.dataset)
    check_sources(dataset, ROOT / "data/knowledge_v2")
    selected = set(args.cases.split(",")) if args.cases else None
    cases = [
        c
        for c in dataset.cases
        if c.split == args.split and (selected is None or c.case_id in selected)
    ]
    if not cases or (selected is not None and selected != {c.case_id for c in cases}):
        raise ValueError("Invalid or empty case selection")
    if not args.paid:
        print(
            {
                "mode": "offline_validation",
                "selected_tasks": len(cases),
                "turns": sum(len(c.turns) for c in cases),
                "paid_calls": 0,
            }
        )
        return
    if args.budget is None or not 1 < args.budget <= 30 or args.output is None:
        raise ValueError("Explicit output and authorized Phase 8 budget (maximum CNY 30) required")
    sys.path.insert(0, str(args.source / "src"))
    if args.layer == "agent":
        from eval_support.runner import run
    else:
        from eval_support.components import run

    asyncio.run(run(args, dataset, cases), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
