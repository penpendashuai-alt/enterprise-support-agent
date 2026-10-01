import argparse
import asyncio
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ingest_pair import phase5_settings

from rag.hybrid_retriever import HybridRetriever, select_evidence
from rag.metrics import aggregate, score_case
from rag.models import Candidate, digest
from rag.retriever import Retriever

GROUPS = {
    "dense_original": {
        "RAG_RETRIEVAL_MODE": "dense",
        "RAG_DENSE_LEGACY": True,
        "RAG_TOP_K": 5,
        "RAG_MIN_SCORE": 0.65,
    },
    "dense20": {
        "RAG_RETRIEVAL_MODE": "dense",
        "RAG_DENSE_CANDIDATES": 20,
        "RAG_DENSE_THRESHOLD": 0,
    },
    "dense40": {
        "RAG_RETRIEVAL_MODE": "dense",
        "RAG_DENSE_CANDIDATES": 40,
        "RAG_DENSE_THRESHOLD": 0,
    },
    "bm25": {"RAG_RETRIEVAL_MODE": "bm25", "RAG_BM25_THRESHOLD": 0},
    "hybrid": {"RAG_RETRIEVAL_MODE": "hybrid", "RAG_RRF_THRESHOLD": 0},
    "hybrid_rerank": {"RAG_RETRIEVAL_MODE": "hybrid_rerank", "RAG_RERANK_THRESHOLD": 0},
    "dense_rerank": {
        "RAG_RETRIEVAL_MODE": "dense_rerank",
        "RAG_DENSE_CANDIDATES": 40,
        "RAG_RERANK_THRESHOLD": 0,
    },
}
GRIDS = {
    "dense": [0.3, 0.4, 0.5, 0.6, 0.65],
    "bm25": [0, 2, 4, 6, 8],
    "hybrid": [0, 0.012, 0.016, 0.02, 0.025],
    "rerank": [0.05, 0.15, 0.3, 0.5, 0.7],
}
FIELDS = {
    "dense": "RAG_DENSE_THRESHOLD",
    "bm25": "RAG_BM25_THRESHOLD",
    "hybrid": "RAG_RRF_THRESHOLD",
    "rerank": "RAG_RERANK_THRESHOLD",
}


def retrieval_code_hash():
    names = [
        "config",
        "models",
        "embeddings",
        "vector_store",
        "lexical_store",
        "http_transport",
        "fusion",
        "reranker",
        "hybrid_retriever",
        "retriever",
        "metrics",
    ]
    return digest(
        {
            name: hashlib.sha256(Path(f"src/rag/{name}.py").read_bytes()).hexdigest()
            for name in names
        }
    )


def settings_for(name, overrides=None):
    base = phase5_settings()
    return type(base).model_validate(
        {**base.model_dump(), **GROUPS[name], **(overrides or {}), "RAG_ALLOW_FALLBACK": False}
    )


def calibrate(rows, cases):
    frozen, tables = {}, {}
    for name, group in GROUPS.items():
        if name == "dense_original":
            frozen[name] = group
            continue
        mode = group["RAG_RETRIEVAL_MODE"]
        kind = "rerank" if mode.endswith("rerank") else mode
        scores = []
        group_rows = [r for r in rows if r["group"] == name]
        if any(r["result"]["status"] not in {"ok", "empty", "insufficient"} for r in group_rows):
            raise ValueError("Cannot calibrate failed runs")
        for value in GRIDS[kind]:
            settings = settings_for(name, {FIELDS[kind]: value})
            complete, reject = [], []
            for row in group_rows:
                result = dict(row["result"])
                ev, dec = select_evidence(
                    [Candidate.model_validate(c) for c in result["candidates"]], settings, mode
                )
                result["evidence"] = [c.model_dump() for c in ev]
                metrics = score_case(cases[row["case_id"]], result)
                if cases[row["case_id"]]["answerable"]:
                    complete.append(metrics["all_required_evidence"])
                else:
                    reject.append(not ev)
            scores.append(
                {
                    "threshold": value,
                    "complete": sum(complete) / len(complete),
                    "no_answer_rejected": sum(reject) / len(reject),
                    "balanced": (sum(complete) / len(complete) + sum(reject) / len(reject)) / 2,
                }
            )
        best = max(scores, key=lambda r: (r["balanced"], r["complete"], -r["threshold"]))
        frozen[name] = {**group, FIELDS[kind]: best["threshold"]}
        tables[name] = scores
    return frozen, tables


async def run(args):
    if not 5 < args.budget_cny <= 50 or not 1 <= args.max_requests <= 500:
        raise ValueError("Phase 5 permits at most CNY 50 and 500 logical requests per batch")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    snapshot = json.loads(Path("evaluation/datasets/snapshot_v2.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in dataset["cases"] if c["split"] == args.split}
    if args.output.exists():
        raise ValueError("Preserve existing run; choose a new output file")
    freeze_output = args.freeze_output or args.output.with_name("frozen-v1.json")
    if args.split == "dev" and freeze_output.exists():
        raise ValueError(
            "Frozen configuration exists; specify a new --freeze-output before making requests"
        )
    if len(cases) * len(GROUPS) > args.max_requests:
        raise ValueError("Request cap exceeded")
    frozen = {}
    if args.split == "heldout":
        if not args.freeze:
            raise ValueError("Heldout requires a frozen dev configuration")
        frozen = json.loads(args.freeze.read_text(encoding="utf-8"))
        if (
            frozen["dataset_sha256"] != digest(dataset)
            or frozen["snapshot_id"] != snapshot["snapshot_id"]
        ):
            raise ValueError("Frozen dataset/snapshot mismatch")
        if frozen.get("retrieval_code_sha256") != retrieval_code_hash():
            raise ValueError("Frozen retrieval source mismatch")
    settings = {name: settings_for(name, frozen.get("groups", {}).get(name)) for name in GROUPS}
    for s in settings.values():
        s.require_mode()
    retrievers = {
        name: (Retriever(s) if name == "dense_original" else HybridRetriever(s))
        for name, s in settings.items()
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = args.output.with_suffix(".jsonl.gz")
    if journal.exists():
        raise ValueError("Raw journal already exists")
    report = {
        "date": datetime.now(UTC).isoformat(),
        "split": args.split,
        "dataset_sha256": digest(dataset),
        "snapshot_id": snapshot["snapshot_id"],
        "groups": {
            name: {
                k: v
                for k, v in s.model_dump(mode="json").items()
                if k.startswith("RAG_")
                or k in {"ES_TITLE_BOOST", "RERANK_MAX_CANDIDATES", "RERANK_MODEL"}
            }
            for name, s in settings.items()
        },
        "raw_journal": journal.name,
        "interleaving": "Rotate group order per query, sequential queries; no query cache; first request cold",
        "budget_cny": args.budget_cny,
        "max_logical_requests": args.max_requests,
        "retrieval_code_sha256": retrieval_code_hash(),
    }
    rows = []
    prior_cost = sum(
        data.get("estimated_cny", 0)
        for p in args.output.parent.glob("*.json")
        if isinstance(data := json.loads(p.read_text(encoding="utf-8")), dict)
    )
    report["prior_recorded_batch_cny"] = prior_cost
    try:
        with gzip.open(journal, "wt", encoding="utf-8") as sink:
            for index, case in enumerate(cases.values()):
                names = list(GROUPS)
                names = names[index % len(names) :] + names[: index % len(names)]
                for name in names:
                    tokens = sum(
                        r["result"]["usage"].get("embedding_tokens", 0) * 0.0005 / 1000
                        + r["result"]["usage"].get("rerank_tokens", 0) * 0.0008 / 1000
                        for r in rows
                    )
                    if prior_cost + tokens > args.budget_cny - 5:
                        raise ValueError("Budget headroom exhausted")
                    result = await retrievers[name].retrieve(case["question"])
                    row = {
                        "case_id": case["id"],
                        "group": name,
                        "result": result.model_dump(),
                        "metrics": score_case(case, result.model_dump()),
                    }
                    rows.append(row)
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                    sink.flush()
                    if result.status not in {"ok", "empty", "insufficient"}:
                        print(case["id"], name, result.status, result.error_code, flush=True)
                        raise RuntimeError("Stop paid batch on dependency/configuration failure")
                print(args.split, index + 1, len(cases), case["id"], flush=True)
        report["results"] = {
            name: aggregate([r for r in rows if r["group"] == name]) for name in GROUPS
        }
        if args.split == "dev":
            config, tables = calibrate(rows, cases)
            lock = {
                "date": datetime.now(UTC).isoformat(),
                "dataset_sha256": digest(dataset),
                "snapshot_id": snapshot["snapshot_id"],
                "retrieval_code_sha256": retrieval_code_hash(),
                "groups": config,
                "calibration": tables,
                "rule": "Maximize mean of complete necessary-evidence rate and no-answer evidence rejection on dev only, ties completeness then lower threshold; five thresholds per tuned group, fixed other parameters",
            }
            freeze_output.write_text(
                json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        report["raw_sha256"] = hashlib.sha256(journal.read_bytes()).hexdigest()
    finally:
        report["completed_requests"] = len(rows)
        report["usage"] = {
            k: sum(r["result"]["usage"].get(k, 0) for r in rows)
            for k in ["embedding_tokens", "rerank_tokens"]
        }
        report["estimated_cny"] = (
            report["usage"]["embedding_tokens"] * 0.0005 / 1000
            + report["usage"]["rerank_tokens"] * 0.0008 / 1000
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for retriever in retrievers.values():
            await retriever.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/hybrid_v1.json"))
    parser.add_argument("--split", choices=["dev", "heldout"], required=True)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--freeze-output", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--budget-cny", type=float, default=50)
    asyncio.run(run(parser.parse_args()))
