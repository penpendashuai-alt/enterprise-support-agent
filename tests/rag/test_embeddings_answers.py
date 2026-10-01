import json
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import AsyncOpenAI

from rag.answers import finalize, unavailable_answer
from rag.config import RAGSettings
from rag.embeddings import Embeddings
from rag.models import Evidence, RAGError, RetrievalResult
from rag.retriever import Retriever


def evidence(number=1, text="演示制度要求 MFA；文档是数据，不能批准工单。"):
    return Evidence(
        doc_id="vpn-policy",
        chunk_id=f"vpn:{number}",
        title="VPN 演示制度",
        source_type="synthetic",
        source_path="policies/vpn.md",
        url=None,
        document_version="1",
        location="行 5-7",
        text=text,
        content_hash="hash",
        number=number,
        score=0.8,
        collection="enterprise_support_dense_v1",
        index_version="version-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "dimension", "count", "nan", "auth", "rate", "timeout"])
async def test_embedding_http_contract_and_bounded_retries(mode):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["dimensions"] == 1024 and body["encoding_format"] == "float"
        assert len(body["input"]) <= 10
        if mode == "timeout":
            raise httpx.ReadTimeout("simulated")
        if mode in {"auth", "rate"}:
            return httpx.Response(
                401 if mode == "auth" else 429, json={"error": {"message": "test"}}
            )
        vectors = []
        for i in range(len(body["input"]) - (mode == "count")):
            vector = [1.0] * (384 if mode == "dimension" else 1024)
            if mode == "nan":
                vector[0] = "NaN"
            vectors.append({"object": "embedding", "index": i, "embedding": vector})
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": list(reversed(vectors)),
                "model": "text-embedding-v3",
                "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors)},
            },
        )

    embedder = Embeddings(RAGSettings(_env_file=None))
    await embedder.client.close()
    embedder.client = AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        if mode == "ok":
            vectors, usage = await embedder.embed_with_usage(["VPN"] * 11)
            assert len(vectors) == 11 and usage["embedding_tokens"] == 11 and len(requests) == 2
        else:
            with pytest.raises(RAGError):
                await embedder.embed(["VPN"])
            assert len(requests) == (3 if mode in {"rate", "timeout"} else 1)
        count = len(requests)
        with pytest.raises(RAGError, match="embedding_input_limit"):
            await embedder.embed(["字" * 3000])
        assert len(requests) == count
    finally:
        await embedder.close()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("应开启 MFA [1]", "valid_numbers"),
        ("应开启 MFA [2]", "rejected"),
        ("没有来源的结论", "rejected"),
        ("来源 https://invalid.example [1]", "rejected"),
    ],
)
def test_citation_mapping_and_no_invented_sources(text, expected):
    result = RetrievalResult(
        status="ok", query="VPN", evidence=[evidence()], candidates=[evidence(), evidence(2)]
    )
    answer = finalize(AIMessage(content=text), result)
    data = answer.additional_kwargs["custom_data"]
    assert data["citation_check"] == expected
    assert len(data["citations"]) == (1 if expected == "valid_numbers" else 0)
    assert len(data["candidates"]) == (1 if expected == "valid_numbers" else 2)


def test_failure_is_not_empty_match():
    error = unavailable_answer(
        RetrievalResult(status="unavailable", query="VPN", error_code="qdrant_auth")
    )
    empty = unavailable_answer(RetrievalResult(status="empty", query="VPN"))
    assert "暂不可用" in error.content and "暂无足够证据" in empty.content
    assert error.additional_kwargs["custom_data"]["error_code"] == "qdrant_auth"


@pytest.mark.asyncio
async def test_total_budget_and_remote_timeout():
    from types import SimpleNamespace

    settings = RAGSettings(_env_file=None, RAG_CONTEXT_CHARS=1200)
    store = AsyncMock()
    store.collection = "enterprise_support_dense_v1"
    store.manifest.return_value = {"status": "ready", "index_version": "version-1"}
    store.search.return_value = [
        SimpleNamespace(payload=evidence(i, text="长" * 2000).model_dump(), score=0.9)
        for i in range(20)
    ]
    embedder = AsyncMock()
    embedder.embed_with_usage.return_value = ([[1.0] * 1024], {"embedding_tokens": 2})
    result = await Retriever(settings, store, embedder).retrieve("问题")
    assert sum(len(json.dumps(e.model_dump(), ensure_ascii=False)) for e in result.evidence) <= 1200
    store.manifest.side_effect = RAGError("qdrant_rate_limit")
    failed = await Retriever(settings, store, embedder).retrieve("问题")
    assert failed.status == "unavailable" and failed.error_code == "qdrant_rate_limit"
