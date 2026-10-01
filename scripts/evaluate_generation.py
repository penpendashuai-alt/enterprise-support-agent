import argparse
import asyncio
import gzip
import hashlib
import json
import random
from pathlib import Path
from time import perf_counter

from evaluate_hybrid import GROUPS
from langchain_core.messages import HumanMessage, SystemMessage
from run_phase4_scenarios import Usage

from agents.support_agent import HANDLER_PROMPT, get_support_model
from core import settings
from rag.answers import evidence_message, finalize, unavailable_answer
from rag.hybrid_retriever import select_evidence
from rag.models import Candidate, RetrievalResult, digest


async def run(args):
    from evaluate_hybrid import settings_for

    if args.output.exists():
        raise ValueError("Preserve prior generation results")
    blind_output = args.output.with_name(f"{args.output.stem}-review-blinded.json")
    if blind_output.exists():
        raise ValueError("Preserve prior blinded review")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(Path("evaluation/datasets/hybrid_v1.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in corpus["cases"]}
    selection = json.loads(
        Path("evaluation/datasets/generation_v1.json").read_text(encoding="utf-8")
    )
    frozen = json.loads(args.freeze.read_text(encoding="utf-8"))
    if digest(corpus) != frozen["dataset_sha256"]:
        raise ValueError("Frozen question mismatch")
    if settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
        raise ValueError("Price contract only covers deepseek-v4-pro")
    retrievals = {}
    for journal in args.retrieval:
        with gzip.open(journal, "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                retrievals[row["case_id"], row["group"]] = row["result"]
    prior = sum(
        data.get("estimated_cny", 0)
        for p in args.output.parent.glob("*.json")
        if isinstance(data := json.loads(p.read_text(encoding="utf-8")), dict)
    )
    tracker = Usage(50, prior)
    model = get_support_model({}).model_copy(
        update={
            "temperature": 0,
            "max_tokens": 1600,
            "max_retries": 0,
            "stream_usage": True,
            "callbacks": [tracker],
        }
    )
    report = {
        "model": settings.COMPATIBLE_MODEL,
        "parameters": {"temperature": 0, "max_tokens": 1600, "thinking": "disabled"},
        "prompt_sha256": hashlib.sha256(
            (HANDLER_PROMPT + Path("src/rag/answers.py").read_text(encoding="utf-8")).encode()
        ).hexdigest(),
        "dataset_sha256": digest(corpus),
        "frozen_sha256": hashlib.sha256(args.freeze.read_bytes()).hexdigest(),
        "method": "Replay recorded raw-query retrieval candidates; dev candidates reselected with frozen thresholds; no Router; no gold answers in generation input",
        "records": [],
    }
    tasks = [(case_id, name) for case_id in selection["case_ids"] for name in GROUPS]
    random.Random(20261001).shuffle(tasks)
    try:
        for number, (case_id, name) in enumerate(tasks, 1):
            raw = dict(retrievals[case_id, name])
            if name != "dense_original":
                config = settings_for(name, frozen["groups"][name])
                evidence, decisions = select_evidence(
                    [Candidate.model_validate(c) for c in raw["candidates"]],
                    config,
                    config.RAG_RETRIEVAL_MODE,
                )
                raw.update(
                    evidence=[c.model_dump() for c in evidence],
                    selection=decisions,
                    status="ok" if evidence else "insufficient" if raw["candidates"] else "empty",
                )
            result = RetrievalResult.model_validate(raw)
            started = perf_counter()
            previous = len(tracker.calls)
            raw_response = None
            if result.status != "ok":
                answer = unavailable_answer(result)
            else:
                async with asyncio.timeout(60):
                    response = await model.ainvoke(
                        [
                            SystemMessage(content=HANDLER_PROMPT),
                            evidence_message(result),
                            HumanMessage(content=cases[case_id]["question"]),
                        ]
                    )
                raw_response = response.content
                answer = finalize(response, result)
            report["records"].append(
                {
                    "id": f"answer-{number:03d}",
                    "case_id": case_id,
                    "group": name,
                    "question": cases[case_id]["question"],
                    "response": answer.content,
                    "provider_response_before_validation": raw_response,
                    "custom_data": answer.additional_kwargs.get("custom_data", {}),
                    "evidence": [e.model_dump() for e in result.evidence],
                    "seconds": perf_counter() - started,
                    "chat_usage": tracker.calls[previous:],
                }
            )
            report["usage"] = {
                "chat_input_tokens": sum(r["input_tokens"] for r in tracker.calls),
                "chat_output_tokens": sum(r["output_tokens"] for r in tracker.calls),
                "calls": len(tracker.calls),
            }
            report["estimated_cny"] = tracker.estimated_cny
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(number, len(tasks), case_id, name, flush=True)
    finally:
        report["status"] = "complete" if len(report["records"]) == len(tasks) else "incomplete"
        report["usage"] = {
            "chat_input_tokens": sum(r["input_tokens"] for r in tracker.calls),
            "chat_output_tokens": sum(r["output_tokens"] for r in tracker.calls),
            "calls": len(tracker.calls),
        }
        report["estimated_cny"] = tracker.estimated_cny
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        blind = [
            {k: r[k] for k in ["id", "question", "response"]}
            | {
                "sources": [
                    {"text": s["text"], "title": s["title"], "location": s["location"]}
                    for s in r["evidence"]
                ]
            }
            for r in report["records"]
        ]
        blind_output.write_text(
            json.dumps(blind, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--retrieval", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
