"""Group identical observed answers for explicit semantic review; never infer a score."""

import argparse
import json
from pathlib import Path

from summarize_resume_campaign import digest


def packets(directory, dataset):
    gold = {c["case_id"]: c for c in dataset["cases"]}
    groups = {}
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    missing = []
    for cid in manifest["planned_cases"]:
        path = directory / f"{cid}.json"
        if not path.exists():
            missing.append(cid)
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if not record["measurement"]:
            continue
        turn = record["turns"][0] if record["turns"] else {}
        response = turn.get("response", {})
        content = {
            "question": turn.get("user_message"),
            "answer": response.get("content"),
            "evidence": [
                {k: e.get(k) for k in ("number", "doc_id", "chunk_id", "text")}
                for e in (turn.get("retrieval") or {}).get("evidence", [])
            ],
            "tools": turn.get("tools"),
            "citation_check": response.get("custom_data", {}).get("citation_check"),
        }
        key = digest(content)
        if key not in groups:
            qid = next(k for k in gold if "-" + k + "-" in cid)
            groups[key] = {
                "group_id": key,
                "query_id": qid,
                "gold": gold[qid],
                **content,
                "records": [],
            }
        groups[key]["records"].append({"case_id": cid, "record_sha256": digest(record)})
    return {"unexecuted": missing, "groups": list(groups.values())}


def apply(packet, rulings):
    if packet["unexecuted"]:
        raise ValueError("Cannot finalize review with unexecuted planned tasks")
    by_id = {r["group_id"]: r for r in rulings}
    if set(by_id) != {g["group_id"] for g in packet["groups"]} or len(by_id) != len(rulings):
        raise ValueError("Every unique observed answer needs exactly one explicit ruling")
    result = []
    for group in packet["groups"]:
        ruling = by_id[group["group_id"]]
        if not isinstance(ruling["semantic_success"], bool) or not ruling["reason"]:
            raise ValueError("Explicit boolean decision and reason required")
        result.extend(
            {
                **record,
                **ruling,
                "method": "Non-independent assistant review of question, full answer and evidence; exact identical packets grouped, not independent human review",
            }
            for record in group["records"]
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--rulings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = packets(args.input, json.loads(args.dataset.read_text(encoding="utf-8")))
    result = (
        apply(packet, json.loads(args.rulings.read_text(encoding="utf-8")))
        if args.rulings
        else packet
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        {
            "groups": len(packet["groups"]),
            "unexecuted": len(packet["unexecuted"]),
            "output": str(args.output),
        }
    )


if __name__ == "__main__":
    main()
