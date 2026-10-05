import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Tri = Literal["yes", "no", "uncertain", "not_applicable"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Strict):
    doc_id: str
    path: str
    location: str
    quote: str


class Point(Strict):
    point_id: str
    description: str
    evidence_any_of: list[list[Source]] = Field(default_factory=list)


class ToolRule(Strict):
    name: str
    arguments: dict = Field(default_factory=dict)


class Turn(Strict):
    operation: Literal[
        "user", "approve", "cancel", "edit", "history", "prefs_put", "prefs_get", "prefs_delete"
    ] = "user"
    text: str | None = None
    user: Literal["alice", "bob"] = "alice"
    thread: Literal["primary", "secondary"] = "primary"
    stream: bool = False
    values: dict = Field(default_factory=dict)
    allowed_intents: list[str] = Field(default_factory=list)
    required_tools: list[ToolRule] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    expected_http: int = 200
    expected_kind: str | None = None
    expected_entities: dict = Field(default_factory=dict)
    expected_ticket_count: int | None = None

    @model_validator(mode="after")
    def request_contract(self):
        if (self.operation == "user") != bool(self.text):
            raise ValueError("Only user turns carry model messages")
        if self.stream and self.operation != "user":
            raise ValueError("Stream only user turns in v1")
        return self


class Case(Strict):
    case_id: str
    family_id: str
    split: Literal["dev", "heldout", "regression"]
    category: str
    tags: list[str] = Field(default_factory=list)
    version: str = "agent-v1"
    source: str = "assistant-authored synthetic scenario; not independently annotated"
    layer: Literal["agent", "fixed_evidence", "retrieval"] = "agent"
    answerability: Literal["answerable", "partial", "unanswerable", "not_applicable"] = (
        "not_applicable"
    )
    turns: list[Turn] = Field(min_length=1)
    points: list[Point] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    final_ticket_count: int = 0
    final_draft_version: int | None = None
    review_required: bool = True
    fault: Literal["none", "retrieval_unavailable"] = "none"
    expected_termination: str = "completed"
    dependencies: list[str] = Field(
        default_factory=lambda: [
            "postgres",
            "redis",
            "chat",
            "optional_qdrant_embedding",
            "mock_read_only_tools",
        ]
    )


class Dataset(Strict):
    version: str
    description: str
    cases: list[Case]

    @model_validator(mode="after")
    def grouping(self):
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task ID")
        families = {}
        for case in self.cases:
            previous = families.setdefault(case.family_id, case.split)
            if previous != case.split:
                raise ValueError("Family crosses development and heldout")
        return self


class Claim(Strict):
    turn: int
    quote: str
    supported: Tri
    reason: str
    citation_numbers: list[int] = Field(default_factory=list)


class Review(Strict):
    run_id: str
    case_id: str
    attempt: int = 1
    record_sha256: str
    status: Literal["pending", "reviewed", "uncertain"] = "pending"
    reviewer_type: Literal["developer_assistant_nonindependent", "independent_human", "judge"] = (
        "developer_assistant_nonindependent"
    )
    reviewer: str = ""
    reviewed_at: str | None = None
    rubric_version: str = "phase8-rubric-v1"
    points: dict[str, Tri] = Field(default_factory=dict)
    evidence_complete: Tri = "uncertain"
    claims: list[Claim] = Field(default_factory=list)
    missing_citations: int | None = None
    refusal: Literal["none", "partial", "full", "not_applicable", "uncertain"] = "uncertain"
    semantic_success: Tri = "uncertain"
    primary_cause: str | None = None
    secondary_causes: list[str] = Field(default_factory=list)
    rationale: str = ""
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_review(self):
        if self.status == "reviewed" and (
            not self.reviewer or not self.reviewed_at or not self.rationale
        ):
            raise ValueError("Reviewed judgments require provenance and reasons")
        return self


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def load_dataset(path):
    return Dataset.model_validate_json(Path(path).read_text(encoding="utf-8"))


def check_sources(dataset, corpus_root):
    for case in dataset.cases:
        for point in case.points:
            for combination in point.evidence_any_of:
                for source in combination:
                    path = (Path(corpus_root) / source.path).resolve()
                    if not path.is_relative_to(Path(corpus_root).resolve()):
                        raise ValueError("Source escapes corpus")
                    text = path.read_text(encoding="utf-8")
                    if source.quote not in text or source.location not in text:
                        raise ValueError(
                            f"Invalid source annotation: {case.case_id}/{point.point_id}"
                        )
