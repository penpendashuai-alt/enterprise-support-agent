import asyncio
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from rag.config import RAGSettings
from rag.models import Chunk, RAGError

MANIFEST_ID = str(uuid5(NAMESPACE_URL, "enterprise-support:index-manifest:v1"))


class VectorStore:
    def __init__(self, settings: RAGSettings, client: AsyncQdrantClient | None = None):
        self.settings = settings
        self.client = client or AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None,
            timeout=int(settings.QDRANT_TIMEOUT),
            check_compatibility=False,
        )
        self.collection = settings.QDRANT_COLLECTION

    async def close(self):
        await self.client.close()

    async def call(self, operation, **kwargs):
        for attempt in range(3):
            try:
                return await operation(**kwargs)
            except (UnexpectedResponse, ResponseHandlingException) as exc:
                status = exc.status_code if isinstance(exc, UnexpectedResponse) else None
                transient = status is None or status == 429 or status >= 500
                code = (
                    "qdrant_auth"
                    if status in {401, 403}
                    else "qdrant_rate_limit"
                    if status == 429
                    else "qdrant_unavailable"
                )
                if not transient or attempt == 2:
                    raise RAGError(code) from None
                await asyncio.sleep(0.3 * 2**attempt)
        raise RAGError("qdrant_retry_exhausted")

    async def manifest(self) -> dict:
        info = await self.call(self.client.get_collection, collection_name=self.collection)
        vector = info.config.params.vectors
        if (
            not isinstance(vector, models.VectorParams)
            or vector.size != self.settings.EMBED_DIMENSIONS
            or vector.distance != models.Distance.COSINE
        ):
            raise RAGError("index_vector_mismatch")
        records = await self.call(
            self.client.retrieve,
            collection_name=self.collection,
            ids=[MANIFEST_ID],
            with_payload=True,
        )
        if not records or (records[0].payload or {}).get("owner") != "enterprise-support-agent":
            raise RAGError("unknown_collection")
        manifest = records[0].payload
        if manifest.get("contract") != self.settings.index_contract():
            raise RAGError("index_model_or_configuration_mismatch")
        return manifest

    async def write_manifest(self, manifest: dict):
        await self.call(
            self.client.upsert,
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=MANIFEST_ID, vector=[0.0] * self.settings.EMBED_DIMENSIONS, payload=manifest
                )
            ],
            wait=True,
        )

    async def begin(self, manifest: dict) -> bool:
        exists = await self.call(self.client.collection_exists, collection_name=self.collection)
        if exists:
            previous = await self.manifest()
            if previous.get("index_version") != manifest["index_version"]:
                raise RAGError("immutable_index_requires_new_collection")
            if previous.get("status") == "ready":
                if await self.chunk_count() != manifest["chunk_count"]:
                    raise RAGError("index_count_mismatch")
                return False
        else:
            await self.call(
                self.client.create_collection,
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.settings.EMBED_DIMENSIONS, distance=models.Distance.COSINE
                ),
            )
            await self.write_manifest(manifest)
        await self.call(
            self.client.create_payload_index,
            collection_name=self.collection,
            field_name="kind",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )
        await self.write_manifest(manifest)
        return True

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]], version: str):
        if len(chunks) != len(vectors):
            raise RAGError("embedding_count_mismatch")
        await self.call(
            self.client.upsert,
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=c.point_id,
                    vector=v,
                    payload={**c.model_dump(), "kind": "chunk", "index_version": version},
                )
                for c, v in zip(chunks, vectors, strict=True)
            ],
            wait=True,
        )

    @staticmethod
    def chunk_filter():
        return models.Filter(
            must=[models.FieldCondition(key="kind", match=models.MatchValue(value="chunk"))]
        )

    async def chunk_count(self) -> int:
        result = await self.call(
            self.client.count,
            collection_name=self.collection,
            count_filter=self.chunk_filter(),
            exact=True,
        )
        return result.count

    async def search(self, vector: list[float], limit: int):
        result = await self.call(
            self.client.query_points,
            collection_name=self.collection,
            query=vector,
            query_filter=self.chunk_filter(),
            limit=limit,
            with_payload=True,
        )
        return result.points
