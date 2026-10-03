"""Deterministic HTTP benchmark fixture; real PostgreSQL/Redis, synthetic model and retrieval."""

import asyncio
import importlib
import os
from types import SimpleNamespace

import uvicorn
from langchain_core.messages import AIMessage, HumanMessage

from agents.support_agent import SupportEntities
from rag.config import get_settings
from rag.hybrid_retriever import HybridRetriever
from schema import AgentInfo

service = importlib.import_module("service.service")
support = importlib.import_module("agents.support_agent")
retrieval = importlib.import_module("rag.retriever")
COUNTS = {"model": 0, "embedding": 0, "search": 0, "index": 0, "active_model": 0, "peak_model": 0}


class Model:
    def __init__(self, schema=None):
        self.schema = schema

    def with_structured_output(self, schema):
        return Model(schema)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config):
        COUNTS["model"] += 1
        COUNTS["active_model"] += 1
        COUNTS["peak_model"] = max(COUNTS["peak_model"], COUNTS["active_model"])
        text = next(str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage))
        try:
            await asyncio.sleep(3 if "SLOW" in text else 0.025 if self.schema else 0.06)
        finally:
            COUNTS["active_model"] -= 1
        if self.schema:
            if self.schema.__name__ == "RouteDecision":
                return {
                    "intent": "general_question",
                    "entities": SupportEntities().model_dump(),
                    "needs_clarification": False,
                    "knowledge_required": True,
                    "retrieval_query": text,
                    "clarification_question": None,
                }
            raise ValueError("Use phase6_test_server for approval fixtures")
        return AIMessage(content="演示指南要求启用 MFA [1]。")


class Dense:
    collection = "enterprise_support_dense_fixture"

    async def manifest(self):
        COUNTS["index"] += 1
        await asyncio.sleep(0.005)
        return {
            "status": "ready",
            "snapshot_id": "fixture-v1",
            "index_version": "fixture-v1",
            "contract": get_settings().index_contract(),
        }

    async def search(self, vector, limit):
        COUNTS["search"] += 1
        await asyncio.sleep(0.025)
        return [
            SimpleNamespace(
                score=0.9,
                payload={
                    "doc_id": "fixture",
                    "chunk_id": "fixture-1",
                    "title": "演示指南",
                    "source_type": "synthetic",
                    "source_path": "fixture.md",
                    "url": None,
                    "document_version": "1",
                    "location": "段落1",
                    "text": "演示指南要求启用 MFA。",
                    "content_hash": "fixture",
                    "index_version": "fixture-v1",
                    "snapshot_id": "fixture-v1",
                },
            )
        ]

    async def close(self):
        pass


class Embedder:
    async def embed_with_usage(self, texts):
        COUNTS["embedding"] += 1
        await asyncio.sleep(0.025)
        return [[0.0]], {"embedding_tokens": 10, "requests": 1, "retries": 0}

    async def close(self):
        pass


if __name__ == "__main__":
    support.get_support_model = lambda _: Model()
    service.get_all_agent_info = lambda: [
        AgentInfo(key="support-agent", description="benchmark fixture")
    ]
    config = get_settings().model_copy(
        update={
            "RAG_RETRIEVAL_MODE": "dense",
            "RAG_DENSE_LEGACY": False,
            "QDRANT_COLLECTION": Dense.collection,
        }
    )
    runtime = HybridRetriever(config, dense=Dense(), embedder=Embedder())
    retrieval.retrieve = runtime.retrieve

    @service.app.get("/fixture/stats")
    async def stats():
        output = dict(COUNTS)
        try:
            from core import settings

            if not hasattr(settings, "ADMISSION_ENABLED"):
                return output
            from execution.control import runtime as controls

            output["control"] = controls().snapshot()
            output["completed"] = list(controls().completed)
            from service.support import _locks

            output["thread_locks"] = len(_locks)
        except ImportError:
            pass
        return output

    uvicorn.run(
        service.app,
        host="127.0.0.1",
        port=int(os.environ["PHASE7_TEST_PORT"]),
        loop="memory.postgres:selector_loop_factory",
        log_level="warning",
    )
