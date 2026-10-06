import asyncio
import math
from time import perf_counter

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from rag.config import RAGSettings
from rag.models import RAGError


class Embeddings:
    def __init__(self, settings: RAGSettings):
        self.settings = settings
        self.client = (
            None
            if settings.CI_TEST_MODE
            else AsyncOpenAI(
                api_key=settings.EMBED_API_KEY.get_secret_value()
                if settings.EMBED_API_KEY
                else "not-configured",
                base_url=settings.EMBED_BASE_URL,
                timeout=settings.EMBED_TIMEOUT,
                max_retries=0,
            )
        )
        self.usage_tokens = 0
        self.requests = 0
        self.retries = 0
        self.elapsed = 0.0

    async def close(self):
        if self.client:
            await self.client.close()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return (await self.embed_with_usage(texts))[0]

    async def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        if self.settings.CI_TEST_MODE:
            self.settings.require_dense()
            return [[1.0] + [0.0] * 1023 for _ in texts], {
                "embedding_tokens": 0,
                "requests": 0,
                "retries": 0,
                "fixture_calls": 1,
            }
        assert self.client is not None
        # UTF-8 bytes conservatively bound byte-token input; this is not billing usage.
        if any(not text.strip() or len(text.encode("utf-8")) > 8000 for text in texts):
            raise RAGError("embedding_input_limit")
        vectors = []
        usage = {"embedding_tokens": 0, "requests": 0, "retries": 0}
        started = perf_counter()
        try:
            for offset in range(0, len(texts), self.settings.EMBED_BATCH_SIZE):
                batch = texts[offset : offset + self.settings.EMBED_BATCH_SIZE]
                for attempt in range(3):
                    try:
                        self.requests += 1
                        usage["requests"] += 1
                        response = await self.client.embeddings.create(
                            model=self.settings.EMBED_MODEL_NAME,
                            input=batch,
                            dimensions=self.settings.EMBED_DIMENSIONS,
                            encoding_format="float",
                        )
                        self.usage_tokens += response.usage.total_tokens
                        usage["embedding_tokens"] += response.usage.total_tokens
                        ordered = sorted(response.data, key=lambda item: item.index)
                        if [item.index for item in ordered] != list(range(len(batch))):
                            raise RAGError("embedding_order_or_count")
                        for item in ordered:
                            vector = item.embedding
                            if (
                                len(vector) != self.settings.EMBED_DIMENSIONS
                                or not all(
                                    isinstance(v, (int, float))
                                    and not isinstance(v, bool)
                                    and math.isfinite(v)
                                    for v in vector
                                )
                                or not any(vector)
                            ):
                                raise RAGError("embedding_invalid_vector")
                            vectors.append(vector)
                        break
                    except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
                        status = exc.status_code if isinstance(exc, APIStatusError) else None
                        code = (
                            "embedding_auth"
                            if status in {401, 403}
                            else "embedding_rate_limit"
                            if status == 429
                            else "embedding_timeout"
                            if isinstance(exc, APITimeoutError)
                            else "embedding_unavailable"
                        )
                        transient = status is None or status == 429 or status >= 500
                        if not transient or attempt == 2:
                            raise RAGError(code) from None
                        self.retries += 1
                        usage["retries"] += 1
                        await asyncio.sleep(0.3 * 2**attempt)
            return vectors, usage
        finally:
            self.elapsed += perf_counter() - started
