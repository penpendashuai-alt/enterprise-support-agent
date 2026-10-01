import argparse
import asyncio
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from rag.config import get_settings
from rag.models import digest
from rag.retriever import Retriever


def document_metrics(records):
    answerable = [r for r in records if r["answerable"]]
    recall, reciprocal = [], []
    for row in answerable:
        docs = list(dict.fromkeys(item["doc_id"] for item in row["result"]["candidates"]))[:5]
        relevant = set(row["relevant_documents"])
        recall.append(len(relevant.intersection(docs)) / len(relevant))
        reciprocal.append(
            next((1 / rank for rank, doc in enumerate(docs, 1) if doc in relevant), 0)
        )
    return {
        "answerable_n": len(answerable),
        "recall_at_5": statistics.mean(recall) if recall else None,
        "mrr_at_5": statistics.mean(reciprocal) if reciprocal else None,
        "accepted_gold_document_coverage": statistics.mean(
            any(e["doc_id"] in row["relevant_documents"] for e in row["result"]["evidence"])
            for row in answerable
        )
        if answerable
        else None,
        "coverage_note": "Correct document retained after score and context filtering; distinct from raw candidate Recall@5",
        "definition": "Top 5 chunks -> map to document IDs -> stable dedup -> truncate at 5 documents; no additional chunks fetched. No-answer excluded from recall/MRR.",
    }


def calibration(records):
    rows = []
    for threshold in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        positive, negative = [], []
        for row in records:
            hits = row["result"]["candidates"]
            if row["answerable"]:
                positive.append(
                    any(
                        h["doc_id"] in row["relevant_documents"] and h["score"] >= threshold
                        for h in hits
                    )
                )
            else:
                negative.append(not any(h["score"] >= threshold for h in hits))
        rows.append(
            {
                "threshold": threshold,
                "answerable_coverage": statistics.mean(positive),
                "no_answer_rejected_by_score": statistics.mean(negative),
                "balanced_score": (statistics.mean(positive) + statistics.mean(negative)) / 2,
            }
        )
    chosen = max(
        rows, key=lambda r: (r["balanced_score"], r["answerable_coverage"], -r["threshold"])
    )
    return {
        "rows": rows,
        "chosen_threshold": chosen["threshold"],
        "rule": "Maximize dev balanced score; tie-break higher answerable coverage then lower threshold. This is evidence filtering, not a guarantee of answerability.",
    }


async def run(args):
    if args.calibrate and args.split != "dev":
        raise ValueError("Calibration is only allowed on the dev split")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    settings = get_settings()
    settings.require_connections()
    if args.threshold is not None:
        settings = type(settings).model_validate(
            {**settings.model_dump(), "RAG_MIN_SCORE": args.threshold}
        )
    retriever = Retriever(settings)
    report = {
        "date": datetime.now(UTC).isoformat(),
        "dataset_version": dataset["version"],
        "dataset_sha256": digest(dataset),
        "split": args.split,
        "contract": settings.index_contract(),
        "collection": settings.QDRANT_COLLECTION,
        "threshold": settings.RAG_MIN_SCORE,
        "network": "Windows client -> DashScope Beijing and Qdrant Cloud Europe; wall-clock includes network and bounded retries; first request cold",
        "records": [],
    }
    try:
        for item in dataset["cases"]:
            if item["split"] != args.split:
                continue
            result = await retriever.retrieve(item["question"])
            report["records"].append({**item, "result": result.model_dump()})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(item["id"], result.status, flush=True)
        report["metrics"] = document_metrics(report["records"])
        report["service_failures"] = sum(
            r["result"]["status"] in {"unavailable", "configuration_error"}
            for r in report["records"]
        )
        if args.calibrate:
            if args.split != "dev" or report["service_failures"]:
                raise ValueError("Calibration requires a successful dev run")
            report["calibration"] = calibration(report["records"])
        latency = {}
        for key in ["embedding_seconds", "qdrant_query_seconds", "retrieval_seconds"]:
            times = sorted(r["result"]["timings"].get(key, 0) for r in report["records"])
            latency[key] = {
                "n": len(times),
                "mean": statistics.mean(times),
                "p95_nearest_rank": times[max(0, math.ceil(len(times) * 0.95) - 1)],
            }
        report["latency"] = latency
        report["usage"] = {
            "embedding_tokens": retriever.embedder.usage_tokens,
            "requests": retriever.embedder.requests,
            "retries": retriever.embedder.retries,
            "estimated_cny": retriever.embedder.usage_tokens * 0.0005 / 1000,
            "tariff": "DashScope Beijing text-embedding-v3: CNY 0.0005 / 1000 reported tokens; no free-credit assumption",
        }
        no_answer = [r for r in report["records"] if not r["answerable"]]
        report["no_answer"] = {
            "n": len(no_answer),
            "candidate_evidence_returned": sum(r["result"]["status"] == "ok" for r in no_answer),
            "note": "Not a hallucination measure; generation must still judge if the evidence answers the question.",
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        await retriever.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/dense_v1.json"))
    parser.add_argument("--split", choices=["dev", "heldout"], required=True)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
