import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from eval_support.ledger import now, write
from eval_support.schema import digest

from resume_support.runtime import PhysicalObserver


async def run(args):
    from core import settings
    from rag.config import get_settings
    from rag.hybrid_retriever import HybridRetriever
    from rag.metrics import score_case

    dataset = json.loads((args.campaign / "datasets/retrieval.json").read_text(encoding="utf-8"))
    cases = [c for c in dataset["cases"] if c["split"] == args.split]
    if args.cases:
        selected = set(args.cases.split(","))
        cases = [c for c in cases if c["case_id"] in selected]
        if selected != {c["case_id"] for c in cases}:
            raise ValueError("Unknown retrieval case")
    args.output.mkdir(parents=True, exist_ok=False)
    settings.RAG_CACHE_ENABLED = False
    config = get_settings()
    observer = PhysicalObserver(args.campaign, args.stage, uuid4().hex)
    restore = observer.patch_embeddings()
    retriever = HybridRetriever(config)
    manifest = {
        "run_id": observer.run_id,
        "variant": args.variant,
        "split": args.split,
        "planned_cases": [c["case_id"] for c in cases],
        "dataset_sha256": digest(dataset),
        "source_sha256": {
            p.relative_to(args.source).as_posix(): digest(p.read_text(encoding="utf-8"))
            for p in (args.source / "src").rglob("*.py")
        },
        "index": await retriever.dense.manifest(),
        "config": {
            k: v
            for k, v in config.model_dump(mode="json").items()
            if not any(s in k for s in ("KEY", "URL"))
        },
        "started_at": now(),
        "status": "running",
        "records": [],
    }
    write(args.output / "manifest.json", manifest)
    try:
        for case in cases:
            observer.case_id = case["case_id"]
            point = perf_counter()
            result = await retriever.retrieve(case["query"])
            row = {
                "case_id": case["case_id"],
                "family_id": case["family_id"],
                "split": args.split,
                "run_id": observer.run_id,
                "variant": args.variant,
                "seconds": perf_counter() - point,
                "result": result.model_dump(),
                "metrics": score_case(case, result.model_dump()),
            }
            write(args.output / f"{case['case_id']}.json", row)
            manifest["records"].append(case["case_id"])
            write(args.output / "manifest.json", manifest)
            print(
                json.dumps(
                    {
                        "case": case["case_id"],
                        "status": result.status,
                        "coverage": row["metrics"].get("selected_evidence_coverage"),
                    }
                ),
                flush=True,
            )
            if observer.exhausted:
                break
    finally:
        await retriever.close()
        restore()
        observer.export()
        manifest.update(
            status="complete"
            if len(manifest["records"]) == len(cases) and not observer.exhausted
            else "incomplete",
            finished_at=now(),
        )
        write(args.output / "manifest.json", manifest)


def select_offline(args):
    """Replay a frozen recall with current selector; no provider/client creation."""
    from rag.config import RAGSettings
    from rag.hybrid_retriever import select_evidence
    from rag.metrics import score_case
    from rag.models import Candidate

    source = args.input
    baseline = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    config = RAGSettings(**baseline["config"])
    args.output.mkdir(parents=True, exist_ok=False)
    dataset = json.loads((args.campaign / "datasets/retrieval.json").read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in dataset["cases"]}
    for cid in baseline["records"]:
        record = json.loads((source / f"{cid}.json").read_text(encoding="utf-8"))
        result = record["result"]
        evidence, decisions = select_evidence(
            [Candidate.model_validate(c) for c in result["candidates"]], config, "dense"
        )
        result.update(evidence=[e.model_dump() for e in evidence], selection=decisions)
        record.update(
            variant=args.variant,
            metrics=score_case(cases[cid], result),
            replay="frozen candidates; selection only; timing not remeasured",
        )
        write(args.output / f"{cid}.json", record)
    write(
        args.output / "manifest.json",
        {
            **baseline,
            "variant": args.variant,
            "source_recall": str(Path(source).relative_to(args.campaign)),
            "selector_source_sha256": digest(
                (args.source / "src/rag/hybrid_retriever.py").read_text(encoding="utf-8")
            ),
            "replay_only": True,
        },
    )
