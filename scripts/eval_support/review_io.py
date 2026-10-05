"""Compile explicitly authored judgments; this module does not judge answers."""

import re

from eval_support.schema import Claim, Review, digest


def compile_review(case, record, annotation, timestamp):
    if annotation["record_sha256"] != digest(record):
        raise ValueError("Annotation targets a different visible answer")
    claims = []
    for judgment in annotation.get("turn_judgments", []):
        index = judgment["turn"]
        text = record["turns"][index]["response"].get("content", "")
        if judgment["text_sha256"] != digest(text):
            raise ValueError("Reviewer has not signed this answer text")
        overrides = judgment.get("overrides", [])
        for override in overrides:
            if override["quote"] not in text:
                raise ValueError("Unsupported excerpt is absent from visible answer")
        # Segmentation formats a judgment already made by the reviewer, not a semantic classifier.
        for line in text.splitlines():
            if not line.strip():
                continue
            matching = [o for o in overrides if o["quote"] in line]
            claims.append(
                Claim(
                    turn=index,
                    quote=line,
                    supported=matching[0]["supported"]
                    if matching
                    else judgment["default_supported"],
                    reason=matching[0]["reason"] if matching else judgment["reason"],
                    citation_numbers=[int(n) for n in re.findall(r"\[(\d+)\]", line)],
                )
            )
    return Review(
        run_id=record["run_id"],
        case_id=case.case_id,
        attempt=record.get("attempt", 1),
        record_sha256=digest(record),
        status=annotation["status"],
        reviewer="Codex development assistant (same designer and implementer)",
        reviewed_at=timestamp,
        points=annotation["points"],
        claims=claims,
        evidence_complete=annotation["evidence_complete"],
        missing_citations=annotation["missing_citations"],
        refusal=annotation["refusal"],
        semantic_success=annotation["semantic_success"],
        primary_cause=annotation.get("primary_cause"),
        secondary_causes=annotation.get("secondary_causes", []),
        rationale=annotation["rationale"],
        uncertainties=annotation.get("uncertainties", []),
    )
