"""Rebuild descriptive Phase 5 reports from preserved records without paid calls."""

import gzip
import hashlib
import json
import statistics
from pathlib import Path

from evaluate_hybrid import GROUPS, settings_for

from rag.hybrid_retriever import select_evidence
from rag.metrics import aggregate, matches, score_case
from rag.models import Candidate

ROOT = Path("evaluation/results/phase5")


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def write(name, value):
    (ROOT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    frozen = read("frozen-v1.json")
    cases = {
        c["id"]: c
        for c in json.loads(Path("evaluation/datasets/hybrid_v1.json").read_text(encoding="utf-8"))[
            "cases"
        ]
    }
    comparison = {
        "method": "Derived offline; dev reselected with frozen thresholds; original journals untouched",
        "default": frozen["provisional_default"],
        "splits": {},
    }
    for split in ["dev", "heldout"]:
        report = read(f"{split}-v1.json")
        journal = ROOT / report["raw_journal"]
        assert hashlib.sha256(journal.read_bytes()).hexdigest() == report["raw_sha256"]
        with gzip.open(journal, "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        assert len(rows) == report["completed_requests"]
        for row in rows:
            if split == "dev" and row["group"] != "dense_original":
                config = settings_for(row["group"], frozen["groups"][row["group"]])
                result = row["result"]
                evidence, selection = select_evidence(
                    [Candidate.model_validate(c) for c in result["candidates"]],
                    config,
                    config.RAG_RETRIEVAL_MODE,
                )
                result.update(evidence=[c.model_dump() for c in evidence], selection=selection)
                row["metrics"] = score_case(cases[row["case_id"]], result)
        groups = {}
        for name in GROUPS:
            selected = [r for r in rows if r["group"] == name]
            data = aggregate(selected)
            count_keys = {k for r in selected for k in r["result"].get("counts", {})}
            data["candidate_counts"] = {
                k: {
                    "min": min(values),
                    "mean": statistics.mean(values),
                    "max": max(values),
                }
                for k in sorted(count_keys)
                if (
                    values := [
                        r["result"]["counts"][k] for r in selected if k in r["result"]["counts"]
                    ]
                )
            }
            loss = {}
            for row in selected:
                case, result = cases[row["case_id"]], row["result"]
                if not case["answerable"]:
                    continue
                gold = [loc for group in case["evidence_alternatives"] for loc in group]
                candidates = {c["chunk_id"]: c for c in result["candidates"]}
                for decision in result.get("selection", []):
                    candidate = candidates[decision["chunk_id"]]
                    if decision["reason"] != "selected" and any(
                        matches(candidate, loc) for loc in gold
                    ):
                        loss.setdefault(decision["reason"], []).append(
                            {"case_id": case["id"], "chunk_id": candidate["chunk_id"]}
                        )
            data["excluded_required_chunks"] = loss
            data["excluded_required_note"] = (
                "Candidate exclusion events, not failed-question count; alternative gold groups may overlap"
            )
            groups[name] = data
        comparison["splits"][split] = groups
    write("comparison.json", comparison)

    generation = read("generation-v1.json")
    reviewed = {r["id"]: r for r in read("generation-review.json")["records"]}
    assert len(generation["records"]) == len(reviewed) == 63
    quality = {}
    for name in GROUPS:
        rows = [r for r in generation["records"] if r["group"] == name]
        reviews = [reviewed[r["id"]] for r in rows]
        for row, review in zip(rows, reviews, strict=True):
            assert hashlib.sha256(row["response"].encode()).hexdigest() == review["response_sha256"]
        quality[name] = {
            "n": len(rows),
            "answerable_n": sum(bool(r["expected_answer_points"]) for r in reviews),
            "no_answer_n": sum(not r["expected_answer_points"] for r in reviews),
            "supported_points": sum(r["supported_points_covered"] for r in reviews),
            "required_points": sum(len(r["expected_answer_points"]) for r in reviews),
            **{
                k: sum(r[k] for r in reviews)
                for k in [
                    "complete",
                    "unsupported_claim",
                    "false_refusal",
                    "wrong_answer_on_no_answer",
                ]
            },
            "citation_rejected": sum(
                r["custom_data"].get("citation_check") == "rejected" for r in rows
            ),
        }
    write(
        "generation-summary.json",
        {
            "groups": quality,
            "note": "Coding-assistant review of nine selected questions, seven answerable and two no-answer per group; not independent human evaluation. Complete means requested supported points covered and unknown subquestion boundary explicit; unsupported claims reported separately.",
        },
    )

    ledger = []

    def add(name, usage):
        ledger.append({"source": name, **usage})

    for name in ["ingestion-v2.json", "ingestion-v2-repeat.json"]:
        add(name, {"embedding_tokens": read(name)["embedding_tokens"]})
    add("rerank-probe.json", read("rerank-probe.json")["usage"])
    for mode, result in read("integration-probe.json")["modes"].items():
        add(f"integration-probe.json:{mode}", result["usage"])
    for mode, result in read("dependency-faults.json").items():
        add(f"dependency-faults.json:{mode}", result["usage"])
    for name in [
        "dev-v1.json",
        "heldout-v1.json",
        "business-regression.json",
        "generation-v1.json",
    ]:
        add(name, read(name)["usage"])
    for path in ROOT.glob("agent-*.json"):
        data = read(path.name)
        usage = {
            "chat_input_tokens": data["chat_usage"]["input_tokens"],
            "chat_output_tokens": data["chat_usage"]["output_tokens"],
        }
        for key in ["embedding_tokens", "rerank_tokens"]:
            usage[key] = sum(
                (t.get("retrieval") or {}).get("usage", {}).get(key, 0)
                for c in data["cases"]
                for t in c["turns"]
            )
        add(path.name, usage)
    rates = {
        "embedding_tokens": 0.5,
        "rerank_tokens": 0.8,
        "chat_input_tokens": 9,
        "chat_output_tokens": 27,
    }
    for entry in ledger:
        entry["estimated_cny"] = sum(
            entry.get(k, 0) * rate / 1_000_000 for k, rate in rates.items()
        )
    total = sum(entry["estimated_cny"] for entry in ledger)
    assert total < 50
    write(
        "cost-summary.json",
        {
            "budget_cny": 50,
            "rates_cny_per_million": rates,
            "entries": ledger,
            "total_usage": {k: sum(e.get(k, 0) for e in ledger) for k in rates},
            "total_estimated_cny": total,
            "note": "API-reported successful usage only; no free-credit/cache/holiday assumptions. Not an invoice; Qdrant subscription, local electricity and unreported failed-provider usage unavailable. Derived summaries and raw journals not double counted.",
            "prices": read("environment.json")["price_sources"],
        },
    )
    print("Wrote comparison and cost summary; estimated CNY", round(total, 6))


if __name__ == "__main__":
    main()
