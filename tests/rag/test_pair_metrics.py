from unittest.mock import AsyncMock

import pytest

from rag.config import RAGSettings
from rag.metrics import coverage, score_case
from rag.models import RAGError, digest
from rag.pair_ingestion import build_pair


def snapshot():
    content = {
        "manifest": {"version": "test"},
        "chunk_contract": {},
        "chunks": [
            {
                "doc_id": "p",
                "chunk_id": "c",
                "title": "policy",
                "source_type": "synthetic",
                "source_path": "p.md",
                "url": None,
                "document_version": "1",
                "location": "规则；行 1",
                "text": "必须确认",
                "content_hash": "hash",
            }
        ],
    }
    return {"snapshot_id": digest(content), **content}


def backends(snap):
    dense, lexical, embedder = AsyncMock(), AsyncMock(), AsyncMock()
    for store in [dense, lexical]:
        store.begin.return_value = True
        store.inventory.return_value = [
            {"chunk_id": "c", "content_hash": "hash", "snapshot_id": snap["snapshot_id"]}
        ]
    embedder.embed.return_value = [[1.0] * 1024]
    embedder.usage_tokens = 2
    embedder.requests = 1
    return dense, lexical, embedder


@pytest.mark.asyncio
async def test_pair_build_ready_only_after_inventory_and_idempotent_reuse():
    snap = snapshot()
    dense, lexical, embedder = backends(snap)
    report = await build_pair(snap, RAGSettings(_env_file=None), dense, lexical, embedder)
    assert report["status"] == "ready"
    assert dense.write_manifest.call_args.args[0]["status"] == "ready"
    assert report["dense_version"] != report["es_version"]
    for store in [dense, lexical]:
        store.begin.return_value = False
    embedder.embed.reset_mock()
    assert (await build_pair(snap, RAGSettings(_env_file=None), dense, lexical, embedder))["reused"]
    embedder.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_write_failure_keeps_pair_unready_and_preserves_existing_dense():
    snap = snapshot()
    dense, lexical, embedder = backends(snap)
    dense.begin.return_value = False
    lexical.upsert.side_effect = RAGError("es_partial_bulk_failure")
    report = await build_pair(snap, RAGSettings(_env_file=None), dense, lexical, embedder)
    assert report["status"] == "failed"
    dense.write_manifest.assert_not_awaited()
    assert lexical.write_manifest.call_args.args[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_same_count_wrong_content_not_marked_ready():
    snap = snapshot()
    dense, lexical, embedder = backends(snap)
    lexical.inventory.return_value[0]["content_hash"] = "changed"
    report = await build_pair(snap, RAGSettings(_env_file=None), dense, lexical, embedder)
    assert report["error"] == "index_inventory_mismatch"
    assert dense.write_manifest.call_args.args[0]["status"] == "failed"


def test_metrics_equivalent_groups_multiclause_and_no_answer_denominators():
    snap = snapshot()
    item = snap["chunks"][0]
    loc = {"doc_id": "p", "document_version": "1", "section": "规则", "contains": "必须"}
    absent = {**loc, "section": "另一节"}
    assert coverage([item], [[loc, absent]]) == 0.5
    assert coverage([item], [[absent], [loc]]) == 1
    case = {"answerable": True, "evidence_alternatives": [[loc, absent]]}
    metrics = score_case(case, {"candidates": [item, item], "evidence": [item]})
    assert metrics["document_recall_at_5_chunks"] == 1
    assert metrics["evidence_recall_at_5"] == 0.5
    assert not metrics["all_required_evidence"]
    assert score_case({"answerable": False}, {"candidates": [item], "evidence": [item]}) == {
        "evidence_accepted_without_answer_label": True
    }


def test_dataset_groups_and_source_anchors_are_consistent():
    import json
    from pathlib import Path

    from rag.metrics import matches
    from rag.snapshot import snapshot_chunks

    root = Path(__file__).resolve().parents[2]
    dataset = json.loads((root / "evaluation/datasets/hybrid_v1.json").read_text(encoding="utf-8"))
    snap = json.loads((root / "evaluation/datasets/snapshot_v2.json").read_text(encoding="utf-8"))
    chunks = [c.model_dump() for c in snapshot_chunks(snap)]
    groups = {}
    for row in dataset["cases"]:
        groups.setdefault(row["group"], set()).add(row["split"])
        if row["answerable"]:
            assert row["evidence_alternatives"]
            for alternative in row["evidence_alternatives"]:
                assert all(any(matches(chunk, loc) for chunk in chunks) for loc in alternative), (
                    row["id"]
                )
        else:
            assert row["evidence_alternatives"] == []
    assert all(len(splits) == 1 for splits in groups.values())
    assert 100 <= len(dataset["cases"]) <= 200
