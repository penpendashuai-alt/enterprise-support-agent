import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from rag.config import RAGSettings
from rag.fusion import reciprocal_rank_fusion
from rag.hybrid_retriever import HybridRetriever, select_evidence
from rag.models import Candidate, RAGError
from rag.reranker import Reranker


def candidate(key="a", score=0.8, kind="dense", snapshot="snap", text="规定必须人工确认。"):
    return Candidate(
        doc_id="policy",
        chunk_id=key,
        title="演示规定",
        source_type="synthetic",
        source_path="policy.md",
        url=None,
        document_version="1",
        location=f"段落 {key}",
        text=text,
        content_hash=key,
        number=0,
        score=score,
        score_type=kind,
        dense_score=score if kind == "dense" else None,
        bm25_score=score if kind == "bm25" else None,
        collection="test",
        index_version="version",
        snapshot_id=snapshot,
    )


def test_rrf_formula_duplicates_empty_and_stable_ties():
    a, b = candidate("a"), candidate("b")
    rows = reciprocal_rank_fusion([a, a, b], [candidate("b", 8, "bm25"), candidate("a", 6, "bm25")])
    assert [r.chunk_id for r in rows] == ["a", "b"]
    assert rows[0].rrf_score == pytest.approx(1 / 61 + 1 / 62)
    assert (rows[0].dense_rank, rows[0].bm25_rank) == (1, 2)
    assert rows[0].dense_score == 0.8 and rows[0].bm25_score == 6
    assert reciprocal_rank_fusion([], []) == []
    assert reciprocal_rank_fusion([a], [])[0].score == pytest.approx(1 / 61)
    with pytest.raises(RAGError, match="index_candidate_mismatch"):
        reciprocal_rank_fusion([a], [candidate(snapshot="wrong")])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "index", "duplicate", "count", "score", "auth", "timeout"])
async def test_reranker_mapping_and_failure_contract(mode):
    requests = []

    def handle(request):
        requests.append(request)
        if mode == "auth":
            return httpx.Response(401)
        if mode == "timeout":
            raise httpx.ReadTimeout("test")
        rows = [{"index": 1, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.9}]
        if mode == "index":
            rows[0]["index"] = 2
        if mode == "duplicate":
            rows[0]["index"] = 0
        if mode == "count":
            rows.pop()
        if mode == "score":
            rows[0]["relevance_score"] = "NaN"
        return httpx.Response(
            200, json={"output": {"results": rows}, "usage": {"total_tokens": 10}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    reranker = Reranker(RAGSettings(_env_file=None), client)
    try:
        if mode == "ok":
            rows, usage = await reranker.rerank("规定？", [candidate("a"), candidate("b")])
            assert [r.chunk_id for r in rows] == ["a", "b"]
            assert rows[0].rerank_score == 0.9 and usage["rerank_tokens"] == 10
        else:
            with pytest.raises(RAGError):
                await reranker.rerank("规定？", [candidate("a"), candidate("b")])
        assert len(requests) == (3 if mode == "timeout" else 1)
        with pytest.raises(RAGError, match="input_limit"):
            await reranker.rerank("过长" * 3000, [candidate()])
    finally:
        await client.aclose()


def test_selection_score_contract_multiple_clauses_and_budget():
    settings = RAGSettings(_env_file=None, RAG_RRF_THRESHOLD=0.01, RAG_MIN_SCORE=0.65)
    rows = reciprocal_rank_fusion([candidate("a"), candidate("b")], [])
    evidence, decisions = select_evidence(rows, settings, "hybrid")
    assert [r.number for r in evidence] == [1, 2]
    assert len({r.doc_id for r in evidence}) == 1
    with pytest.raises(RAGError, match="score_type"):
        select_evidence(rows, settings, "dense")
    settings.RAG_CONTEXT_CHARS = 200
    evidence, decisions = select_evidence(rows, settings, "hybrid")
    assert not evidence and {r["reason"] for r in decisions} == {"character_budget"}


def dependencies():
    dense, lexical, embedder, reranker = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    dense.collection = "dense"
    for store in [dense, lexical]:
        store.manifest.return_value = {
            "status": "ready",
            "snapshot_id": "snap",
            "index_version": "version",
        }
    dense.search.return_value = [SimpleNamespace(payload=candidate().model_dump(), score=0.8)]
    lexical.search.return_value = [candidate(kind="bm25", score=5)]
    embedder.embed_with_usage.return_value = ([[1.0] * 1024], {"embedding_tokens": 3})
    return dense, lexical, embedder, reranker


@pytest.mark.asyncio
async def test_pair_mismatch_never_silently_falls_back():
    d, lex, e, r = dependencies()
    lex.manifest.return_value["snapshot_id"] = "different"
    settings = RAGSettings(_env_file=None, RAG_RETRIEVAL_MODE="hybrid", RAG_ALLOW_FALLBACK=True)
    result = await HybridRetriever(settings, d, lex, e).retrieve("规定")
    assert result.status == "index_inconsistent"
    e.embed_with_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_es_timeout_falls_back_using_dense_threshold():
    d, lex, e, r = dependencies()
    lex.search.side_effect = RAGError("es_unavailable")
    settings = RAGSettings(
        _env_file=None,
        RAG_RETRIEVAL_MODE="hybrid",
        RAG_ALLOW_FALLBACK=True,
        RAG_DENSE_THRESHOLD=0.9,
        RAG_RRF_THRESHOLD=0,
    )
    result = await HybridRetriever(settings, d, lex, e).retrieve("规定")
    assert result.actual_mode == "dense" and result.status == "insufficient"
    assert result.failures[0]["stage"] == "bm25" and result.selection[0]["threshold"] == 0.9


@pytest.mark.asyncio
async def test_rerank_failure_uses_hybrid_selection():
    d, lex, e, r = dependencies()
    r.rerank.side_effect = RAGError("rerank_unavailable")
    settings = RAGSettings(
        _env_file=None,
        RAG_RETRIEVAL_MODE="hybrid_rerank",
        RAG_ALLOW_FALLBACK=True,
        RAG_RRF_THRESHOLD=0.02,
        RAG_RERANK_THRESHOLD=0.99,
    )
    result = await HybridRetriever(settings, d, lex, e, r).retrieve("规定")
    assert result.actual_mode == "hybrid" and result.status == "ok"
    assert result.evidence[0].score_type == "rrf"


@pytest.mark.asyncio
async def test_bm25_needs_no_embedding_and_total_budget_cancels():
    settings = RAGSettings(_env_file=None, RAG_RETRIEVAL_MODE="bm25", RAG_TIMEOUT=0.02)
    settings.require_mode()
    _, lex, _, _ = dependencies()
    retriever = HybridRetriever(settings, lexical=lex)
    assert retriever.dense is None and retriever.embedder is None and retriever.reranker is None
    assert (await retriever.retrieve("规定")).status == "ok"

    async def delay(*args, **kwargs):
        await asyncio.sleep(1)

    lex.search.side_effect = delay
    result = await retriever.retrieve("规定")
    assert result.error_code == "retrieval_timeout"


def test_config_uses_only_requested_dependencies():
    s = RAGSettings(
        _env_file=None,
        RAG_RETRIEVAL_MODE="dense",
        QDRANT_URL="https://example.com",
        QDRANT_API_KEY=SecretStr("x"),
        EMBED_API_KEY=SecretStr("y"),
        ES_URL="invalid",
    )
    s.require_mode()
    s.RAG_RETRIEVAL_MODE = "hybrid_rerank"
    with pytest.raises(ValueError):
        s.require_mode()
