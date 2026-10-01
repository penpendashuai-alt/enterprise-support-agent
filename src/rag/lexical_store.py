import json
import re

import httpx

from rag.config import RAGSettings
from rag.http_transport import request
from rag.models import Candidate, Chunk, RAGError, digest


def technical_terms(text: str) -> list[str]:
    return sorted(set(re.findall(r"[a-zA-Z0-9]+(?:[-_./][a-zA-Z0-9]+)*", text.lower())))


def index_definition():
    keywords = [
        "chunk_id",
        "doc_id",
        "document_version",
        "source_type",
        "source_path",
        "url",
        "content_hash",
        "snapshot_id",
        "index_version",
        "technical_terms",
    ]
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {"analyzer": {"support_zh": {"type": "smartcn"}}},
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                **{name: {"type": "keyword"} for name in keywords},
                "title": {"type": "text", "analyzer": "support_zh"},
                "text": {"type": "text", "analyzer": "support_zh"},
                "location": {"type": "keyword", "index": False},
            },
        },
    }


def lexical_contract():
    return {
        "server": "elasticsearch-9.5.3",
        "plugin": "analysis-smartcn-9.5.3",
        "mapping": digest(index_definition()),
        "technical_terms": "ascii-identifiers-v1",
    }


class LexicalStore:
    def __init__(self, settings: RAGSettings, client=None):
        self.settings = settings
        self.index = settings.ES_INDEX
        self.client = client or httpx.AsyncClient(
            base_url=settings.ES_URL,
            timeout=settings.ES_TIMEOUT,
            headers={"Authorization": f"ApiKey {settings.ES_API_KEY.get_secret_value()}"}
            if settings.ES_API_KEY
            else {},
        )

    async def close(self):
        await self.client.aclose()

    async def call(self, method, path, **kwargs):
        return await request(self.client, method, path, "es", **kwargs)

    async def manifest(self):
        response = await self.call("GET", f"/{self.index}/_mapping")
        mapping = response.json()[self.index]["mappings"]
        meta = mapping.get("_meta", {})
        if meta.get("owner") != "enterprise-support-agent":
            raise RAGError("index_unknown_es")
        if meta.get("contract") != lexical_contract():
            raise RAGError("index_es_contract_mismatch")
        if mapping.get("properties") != index_definition()["mappings"]["properties"]:
            raise RAGError("index_es_mapping_mismatch")
        return meta

    async def write_manifest(self, manifest):
        await self.call("PUT", f"/{self.index}/_mapping", json={"_meta": manifest})

    async def begin(self, manifest):
        response = await self.client.head(f"/{self.index}")
        if response.status_code == 200:
            old = await self.manifest()
            if old.get("snapshot_id") != manifest["snapshot_id"]:
                raise RAGError("index_es_immutable")
            if old.get("status") == "ready":
                return False
        elif response.status_code == 404:
            definition = index_definition()
            definition["mappings"]["_meta"] = manifest
            await self.call("PUT", f"/{self.index}", json=definition)
        else:
            raise RAGError("es_unavailable")
        await self.write_manifest(manifest)
        return True

    async def upsert(self, chunks: list[Chunk], snapshot: str, version: str):
        lines = []
        for chunk in chunks:
            lines.append(json.dumps({"index": {"_index": self.index, "_id": chunk.chunk_id}}))
            lines.append(
                json.dumps(
                    {
                        **chunk.model_dump(),
                        "snapshot_id": snapshot,
                        "index_version": version,
                        "technical_terms": technical_terms(chunk.title + " " + chunk.text),
                    },
                    ensure_ascii=False,
                )
            )
        response = await self.call(
            "POST",
            "/_bulk?refresh=wait_for",
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        if response.json().get("errors"):
            raise RAGError("es_partial_bulk_failure")

    async def inventory(self):
        response = await self.call(
            "POST",
            f"/{self.index}/_search",
            json={
                "size": 10000,
                "track_total_hits": True,
                "_source": ["chunk_id", "content_hash", "snapshot_id"],
                "query": {"match_all": {}},
            },
        )
        data = response.json()["hits"]
        if data["total"]["value"] != len(data["hits"]):
            raise RAGError("index_inventory_limit")
        return [hit["_source"] for hit in data["hits"]]

    async def search(self, query: str, limit: int, manifest: dict):
        clauses: list[dict] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": [f"title^{self.settings.ES_TITLE_BOOST}", "text"],
                    "type": "best_fields",
                }
            }
        ]
        terms = technical_terms(query)
        if terms:
            clauses.append({"terms": {"technical_terms": terms, "boost": 1.0}})
        response = await self.call(
            "POST",
            f"/{self.index}/_search",
            json={
                "size": limit,
                "query": {"bool": {"should": clauses, "minimum_should_match": 1}},
                "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
            },
        )
        result = []
        for rank, hit in enumerate(response.json()["hits"]["hits"], 1):
            body = hit["_source"]
            if (
                body["snapshot_id"] != manifest["snapshot_id"]
                or body["index_version"] != manifest["index_version"]
            ):
                raise RAGError("index_es_payload_mismatch")
            result.append(
                Candidate.model_validate(
                    {
                        **body,
                        "score": hit["_score"],
                        "score_type": "bm25",
                        "bm25_score": hit["_score"],
                        "bm25_rank": rank,
                        "number": 0,
                        "collection": self.index,
                    }
                )
            )
        return result

    async def analyze(self, text):
        response = await self.call(
            "POST", f"/{self.index}/_analyze", json={"analyzer": "support_zh", "text": text}
        )
        return [row["token"] for row in response.json()["tokens"]]
