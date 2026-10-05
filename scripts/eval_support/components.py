"""Router-free experiments; never counted as end-to-end task successes."""

import hashlib
import json
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from eval_support.ledger import Ledger, now, write
from eval_support.schema import digest


async def run(args, dataset, cases):
    from agents.support_agent import HANDLER_PROMPT, get_support_model
    from core import settings
    from rag.answers import evidence_message, finalize, unavailable_answer
    from rag.models import RetrievalResult
    from rag.retriever import close_retriever, retrieve

    if args.output.exists():
        raise ValueError("Preserve prior component runs")
    if any(len(c.turns) != 1 or c.turns[0].operation != "user" for c in cases):
        raise ValueError("Components require single user questions, not business workflows")
    if settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
        raise ValueError("Model outside pricing contract")
    if args.layer == "fixed_evidence" and args.evidence is None:
        raise ValueError("Fixed generation requires a recorded retrieval directory")
    args.output.mkdir(parents=True)
    run_id = uuid4().hex
    ledger = Ledger(args.ledger, run_id, args.budget, args.max_calls)
    restore = ledger.patch_embeddings()
    model = get_support_model({}).model_copy(
        update={
            "temperature": 0,
            "max_tokens": 1800,
            "max_retries": 0,
            "stream_usage": True,
            "callbacks": [ledger],
        }
    )
    manifest = {
        "run_id": run_id,
        "created_at": now(),
        "variant": args.variant,
        "layer": args.layer,
        "router_bypassed": True,
        "dataset_sha256": digest(dataset.model_dump()),
        "planned_cases": [c.case_id for c in cases],
        "records": [],
        "status": "running",
        "source_sha256": {
            str(p.relative_to(args.source)).replace("\\", "/"): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in (args.source / "src").rglob("*.py")
        },
        "model": settings.COMPATIBLE_MODEL,
        "temperature": 0,
        "max_tokens": 1800,
        "cache": False,
        "evidence_input": str(args.evidence) if args.evidence else None,
        "method": "Raw user query retrieval"
        if args.layer == "retrieval"
        else "Replay recorded evidence without Router, tools, memory or business workflow",
    }
    write(args.output / "manifest.json", manifest)
    try:
        for case in cases:
            ledger.case_id, ledger.turn = case.case_id, 0
            started = perf_counter()
            row = {
                "run_id": run_id,
                "case_id": case.case_id,
                "attempt": 1,
                "layer": args.layer,
                "question": case.turns[0].text,
            }
            try:
                if args.layer == "retrieval":
                    result = await retrieve(case.turns[0].text)
                else:
                    path = args.evidence / f"{case.case_id}.json"
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    result = RetrievalResult.model_validate(raw["retrieval"])
                    row["evidence_record_sha256"] = digest(raw)
                row["retrieval"] = result.model_dump()
                if args.layer == "fixed_evidence":
                    before = None
                    if result.status == "ok":
                        response = await model.ainvoke(
                            [
                                SystemMessage(content=HANDLER_PROMPT),
                                evidence_message(result),
                                HumanMessage(content=case.turns[0].text),
                            ]
                        )
                        before = response.content
                        answer = finalize(response, result)
                    else:
                        answer = unavailable_answer(result)
                    row.update(
                        before=before,
                        after=answer.content,
                        custom_data=answer.additional_kwargs.get("custom_data", {}),
                    )
                row["status"] = "completed"
            except Exception as exc:
                row.update(status="error", error_type=type(exc).__name__)
            row.update(
                seconds=perf_counter() - started,
                external_call_ids=[
                    e["call_id"]
                    for e in ledger.events()
                    if e["run_id"] == run_id and e["case_id"] == case.case_id
                ],
            )
            write(args.output / f"{case.case_id}.json", row)
            manifest["records"].append(case.case_id)
            write(args.output / "manifest.json", manifest)
            print(json.dumps({"case": case.case_id, "status": row["status"]}), flush=True)
            if ledger.exhausted or row["status"] != "completed":
                break
    finally:
        await close_retriever()
        restore()
        manifest.update(
            status="complete"
            if len(manifest["records"]) == len(cases) and not ledger.exhausted
            else "incomplete",
            finished_at=now(),
        )
        write(args.output / "manifest.json", manifest)
