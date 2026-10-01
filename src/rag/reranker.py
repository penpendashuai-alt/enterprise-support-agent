import math

import httpx

from rag.config import RAGSettings
from rag.http_transport import request
from rag.models import Candidate, RAGError


class Reranker:
    def __init__(self, settings: RAGSettings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.RERANK_TIMEOUT,
            headers={
                "Authorization": f"Bearer {settings.RERANK_API_KEY.get_secret_value() if settings.RERANK_API_KEY else ''}",
            },
        )
        self.usage_tokens = 0
        self.requests = 0

    async def close(self):
        await self.client.aclose()

    async def rerank(self, query: str, candidates: list[Candidate]):
        if not candidates:
            return [], {"rerank_tokens": 0, "rerank_requests": 0}
        docs = [f"{c.title}\n{c.text}" for c in candidates]
        sizes = [len(t.encode("utf-8")) for t in [query, *docs]]
        if (
            len(docs) > self.settings.RERANK_MAX_CANDIDATES
            or max(sizes) > 3900
            or sizes[0] * len(docs) + sum(sizes[1:]) > 29000
        ):
            raise RAGError("rerank_input_limit")
        self.requests += 1
        response = await request(
            self.client,
            "POST",
            self.settings.RERANK_BASE_URL,
            "rerank",
            json={
                "model": self.settings.RERANK_MODEL,
                "input": {"query": query, "documents": docs},
                "parameters": {"top_n": len(docs), "return_documents": False},
            },
        )
        try:
            body = response.json()
            usage = body["usage"]["total_tokens"]
            if type(usage) is not int or usage < 0:
                raise ValueError("invalid usage")
            self.usage_tokens += usage
            rows = body["output"]["results"]
            indices = [row["index"] for row in rows]
            if any(type(i) is not int for i in indices) or sorted(indices) != list(
                range(len(docs))
            ):
                raise ValueError("invalid indices")
            results = []
            for row in rows:
                score = row["relevance_score"]
                if (
                    type(score) not in {int, float}
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError("invalid score")
                results.append(
                    candidates[row["index"]].model_copy(
                        update={"rerank_score": score, "score": score, "score_type": "rerank"}
                    )
                )
            return sorted(results, key=lambda c: (-c.score, c.chunk_id)), {
                "rerank_tokens": usage,
                "rerank_requests": 1,
            }
        except (KeyError, TypeError, ValueError):
            raise RAGError("rerank_invalid_response") from None
