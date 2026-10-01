from typing import Literal

from dotenv import find_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file_encoding="utf-8")
    QDRANT_URL: str = ""
    QDRANT_API_KEY: SecretStr | None = None
    QDRANT_COLLECTION: str = Field(
        default="enterprise_support_dense_v1", pattern=r"^enterprise_support_dense_[a-zA-Z0-9_-]+$"
    )
    QDRANT_VECTOR_SIZE: int = 1024
    QDRANT_DISTANCE: Literal["cosine"] = "cosine"
    QDRANT_TIMEOUT: float = Field(default=30, gt=0, le=120)
    EMBED_MODEL_TYPE: Literal["dashscope"] = "dashscope"
    EMBED_MODEL_NAME: Literal["text-embedding-v3"] = "text-embedding-v3"
    EMBED_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBED_API_KEY: SecretStr | None = None
    EMBED_DIMENSIONS: int = 1024
    EMBED_BATCH_SIZE: int = Field(default=10, ge=1, le=10)
    EMBED_TIMEOUT: float = Field(default=30, gt=0, le=120)
    RAG_CHUNK_SIZE: int = Field(default=800, ge=100, le=2000)
    RAG_CHUNK_OVERLAP: int = Field(default=100, ge=0)
    RAG_TOP_K: int = Field(default=5, ge=1, le=20)
    RAG_MIN_SCORE: float = Field(default=0.65, ge=-1, le=1)
    RAG_CONTEXT_CHARS: int = Field(default=4500, ge=200, le=15000)
    RAG_SNIPPET_CHARS: int = Field(default=1000, ge=100, le=3000)
    RAG_TIMEOUT: float = Field(default=90, gt=0, le=180)
    RAG_RETRIEVAL_MODE: Literal["dense", "bm25", "hybrid", "hybrid_rerank", "dense_rerank"] = (
        "dense"
    )
    RAG_DENSE_LEGACY: bool = True
    RAG_DENSE_CANDIDATES: int = Field(default=20, ge=1, le=100)
    RAG_BM25_CANDIDATES: int = Field(default=20, ge=1, le=100)
    RAG_FINAL_K: int = Field(default=5, ge=1, le=20)
    RAG_RRF_C: int = Field(default=60, ge=1, le=200)
    RAG_DENSE_THRESHOLD: float = Field(default=0.5, ge=-1, le=1)
    RAG_BM25_THRESHOLD: float = Field(default=0, ge=0)
    RAG_RRF_THRESHOLD: float = Field(default=0, ge=0)
    RAG_RERANK_THRESHOLD: float = Field(default=0.3, ge=0, le=1)
    RAG_ALLOW_FALLBACK: bool = False
    ES_URL: str = "http://127.0.0.1:19200"
    ES_API_KEY: SecretStr | None = None
    ES_INDEX: str = Field(
        default="enterprise-support-lexical-v2", pattern=r"^enterprise-support-[a-z0-9-]+$"
    )
    ES_TIMEOUT: float = Field(default=10, gt=0, le=60)
    ES_TITLE_BOOST: float = Field(default=2, gt=0, le=10)
    RERANK_PROVIDER: Literal["dashscope"] = "dashscope"
    RERANK_MODEL: Literal["gte-rerank-v2"] = "gte-rerank-v2"
    RERANK_BASE_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    RERANK_API_KEY: SecretStr | None = None
    RERANK_USE_EMBED_KEY: bool = False
    RERANK_TIMEOUT: float = Field(default=20, gt=0, le=90)
    RERANK_MAX_CANDIDATES: int = Field(default=40, ge=1, le=100)

    @model_validator(mode="after")
    def check_contract(self):
        if self.RERANK_USE_EMBED_KEY and self.RERANK_API_KEY is None:
            self.RERANK_API_KEY = self.EMBED_API_KEY
        if self.EMBED_DIMENSIONS != 1024 or self.QDRANT_VECTOR_SIZE != self.EMBED_DIMENSIONS:
            raise ValueError("This dense baseline requires matching 1024-dimensional vectors")
        if self.RAG_CHUNK_OVERLAP >= self.RAG_CHUNK_SIZE:
            raise ValueError("Overlap must be smaller than chunk size")
        return self

    def require_connections(self):
        self.require_dense()

    def require_dense(self):
        if not self.QDRANT_URL or not self.EMBED_API_KEY or not self.QDRANT_API_KEY:
            raise ValueError("RAG connection settings are incomplete")
        if not self.QDRANT_URL.startswith("https://") or not self.EMBED_BASE_URL.startswith(
            "https://"
        ):
            raise ValueError("Cloud connections require HTTPS")

    def require_mode(self):
        from urllib.parse import urlparse

        mode = self.RAG_RETRIEVAL_MODE
        if mode != "bm25":
            self.require_dense()
        if mode in {"bm25", "hybrid", "hybrid_rerank"}:
            url = urlparse(self.ES_URL)
            if url.scheme != "https" and not (
                url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1", "elasticsearch"}
            ):
                raise ValueError("ES requires HTTPS or a local development endpoint")
        if mode.endswith("rerank") and (
            not self.RERANK_API_KEY or not self.RERANK_BASE_URL.startswith("https://")
        ):
            raise ValueError("Reranker requires independent HTTPS/key configuration")

    def index_contract(self) -> dict:
        return {
            "provider": self.EMBED_MODEL_TYPE,
            "model": self.EMBED_MODEL_NAME,
            "dimensions": self.EMBED_DIMENSIONS,
            "distance": self.QDRANT_DISTANCE,
            "chunk_size": self.RAG_CHUNK_SIZE,
            "overlap": self.RAG_CHUNK_OVERLAP,
            "preprocessing": "text-v1",
            "chunker": "paragraph-char-v1",
        }


def get_settings() -> RAGSettings:
    return RAGSettings(_env_file=find_dotenv(usecwd=True) or None)
