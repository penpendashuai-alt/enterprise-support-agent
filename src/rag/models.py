import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=1)
    path: str
    version: str
    source_type: Literal["synthetic", "public_summary"]
    url: HttpUrl | None = None
    accessed: str | None = None
    usage: str


class Section(BaseModel):
    text: str
    location: str


class Chunk(BaseModel):
    doc_id: str
    chunk_id: str
    title: str
    source_type: str
    source_path: str
    url: str | None
    document_version: str
    location: str
    text: str
    content_hash: str

    @property
    def point_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.chunk_id))


class Evidence(Chunk):
    number: int
    score: float
    collection: str
    index_version: str


class Candidate(Evidence):
    snapshot_id: str
    score_type: Literal["dense", "bm25", "rrf", "rerank"]
    dense_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    dense_rank: int | None = None
    bm25_rank: int | None = None


class RetrievalResult(BaseModel):
    cache: dict = Field(default_factory=dict)
    status: Literal[
        "ok", "empty", "insufficient", "configuration_error", "index_inconsistent", "unavailable"
    ]
    query: str
    evidence: list[Candidate | Evidence] = Field(default_factory=list)
    candidates: list[Candidate | Evidence] = Field(default_factory=list)
    index_version: str | None = None
    collection: str | None = None
    error_code: str | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    requested_mode: str = "dense"
    actual_mode: str = "dense"
    snapshot_id: str | None = None
    backend_versions: dict = Field(default_factory=dict)
    failures: list[dict] = Field(default_factory=list)
    selection: list[dict] = Field(default_factory=list)
    counts: dict = Field(default_factory=dict)


class RAGError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
