"""Campaign runner: validation is offline, paid runs require --paid explicitly."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


async def preflight(args):
    from rag.config import get_settings
    from rag.vector_store import VectorStore

    config = get_settings()
    config.require_mode()
    if config.CI_TEST_MODE or config.RAG_RETRIEVAL_MODE != "dense" or config.RAG_DENSE_LEGACY:
        raise ValueError("Campaign requires real Dense20/v2")
    store = VectorStore(config)
    try:
        manifest = await store.manifest()
    finally:
        await store.close()
    path = args.campaign / "baseline/index-preflight.json"
    if path.exists():
        raise ValueError("Preserve preflight; use recorded index contract")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"index": manifest, "paid_calls": 0}, ensure_ascii=False))


async def draft_pilot(args):
    from langchain_core.messages import HumanMessage
    from resume_support.runtime import PhysicalObserver

    from agents import support_agent, ticket_flow

    observer = PhysicalObserver(args.campaign, "pilot", uuid4().hex)
    model = observer.model()
    support_agent.get_support_model = lambda _: model
    cases = [
        (
            "pilot-plan",
            "请建工单：VPN 从早上开始断连，只影响我。我希望安排网络排查，还没重启客户端。",
        ),
        ("pilot-missing", "请建工单：我的电脑打不开项目，影响范围还没查清。"),
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        for case_id, message in cases:
            observer.case_id = case_id
            result = await ticket_flow.prepare_draft({"messages": [HumanMessage(message)]}, {})
            serial = {
                k: [m.model_dump() for m in v] if k == "messages" else v for k, v in result.items()
            }
            (args.output / f"{case_id}.json").write_text(
                json.dumps(
                    {"case_id": case_id, "input": message, "result": serial},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(json.dumps({"case": case_id, "result": serial}, ensure_ascii=False), flush=True)
            if observer.exhausted:
                break
    finally:
        observer.export()
        await model.http_async_client.aclose()


async def import_index(args):
    from import_local_index import run
    from resume_support.runtime import PhysicalObserver

    observer = PhysicalObserver(args.campaign, "pilot", uuid4().hex)
    restore = observer.patch_embeddings()
    args.snapshot = ROOT / "evaluation/datasets/snapshot_v2.json"
    args.budget = 1
    try:
        await run(args)
    finally:
        restore()
        observer.export()


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "mode",
        choices=[
            "build-datasets",
            "validate",
            "preflight",
            "draft-pilot",
            "import-index",
            "agent",
            "retrieval",
            "select-offline",
            "cache-experiment",
        ],
    )
    p.add_argument(
        "--campaign", type=Path, default=ROOT / "evaluation/resume-improvement/20261022-r1"
    )
    p.add_argument("--source", type=Path, default=ROOT)
    p.add_argument("--dataset", type=Path)
    p.add_argument("--split", choices=["dev", "heldout", "regression"], default="dev")
    p.add_argument(
        "--stage",
        choices=["pilot", "baseline", "development", "heldout", "performance", "demo"],
        default="baseline",
    )
    p.add_argument("--variant", default="baseline")
    p.add_argument("--cases")
    p.add_argument("--output", type=Path)
    p.add_argument("--input", type=Path)
    p.add_argument("--paid", action="store_true")
    p.add_argument("--cache", action="store_true")
    p.add_argument("--performance", action="store_true")
    p.add_argument(
        "--connection-json", type=Path, default=ROOT / ".cache/resume-20261022/connection.json"
    )
    args = p.parse_args()
    args.campaign, args.source = args.campaign.resolve(), args.source.resolve()
    if args.mode not in {"build-datasets", "validate", "preflight", "select-offline"} and (
        not args.paid or args.output is None
    ):
        p.error("Real model runs require --paid and a fresh --output")
    sys.path.insert(0, str(args.source / "src"))
    os.environ["REDIS_URL"] = "redis://127.0.0.1:16422"
    # Dedicated formal corpus rebuilt from snapshot_v2; never the one-block CI fixture.
    os.environ.update(
        QDRANT_LOCAL="true",
        QDRANT_URL="http://127.0.0.1:16433",
        QDRANT_API_KEY="",
        QDRANT_COLLECTION="enterprise_support_dense_resume20261022",
        CI_TEST_MODE="false",
    )
    if args.mode == "build-datasets":
        from resume_support.dataset import build

        build(args.campaign / "datasets")
        return
    if args.mode == "import-index":
        asyncio.run(import_index(args), loop_factory=asyncio.SelectorEventLoop)
        return
    if args.mode == "retrieval":
        from resume_support.retrieval import run

        asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
        return
    if args.mode == "cache-experiment":
        from resume_support.cache_experiment import run

        asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
        return
    if args.mode == "select-offline":
        from resume_support.retrieval import select_offline

        select_offline(args)
        return
    if args.mode == "preflight":
        asyncio.run(preflight(args), loop_factory=asyncio.SelectorEventLoop)
        return
    if args.mode == "draft-pilot":
        asyncio.run(draft_pilot(args), loop_factory=asyncio.SelectorEventLoop)
        return
    from eval_support.schema import check_sources, load_dataset

    dataset = load_dataset(args.dataset or args.campaign / "datasets/agent.json")
    check_sources(dataset, ROOT / "data/knowledge_v2")
    cases = [
        c
        for c in dataset.cases
        if c.split == args.split and (not args.cases or c.case_id in args.cases.split(","))
    ]
    if not cases:
        raise ValueError("Empty selection")
    if args.mode == "validate":
        print({"mode": "offline", "selected_tasks": len(cases), "paid_calls": 0})
        return
    from eval_support.runner import run
    from resume_support.runtime import PhysicalObserver

    args.ledger_factory = lambda run_id: PhysicalObserver(args.campaign, args.stage, run_id)
    args.namespace_prefix, args.database_prefix = "esa:resume:", "resume_"
    args.layer = "agent"
    args.demo_samples = True
    args.cache_probe = not args.performance
    asyncio.run(run(args, dataset, cases), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
