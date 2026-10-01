from pathlib import Path

from rag.chunker import chunk_document
from rag.loader import load_document, read_manifest
from rag.models import Chunk, digest


def create_snapshot(root: Path, settings):
    manifest, specs = read_manifest(root)
    chunks = []
    for spec in specs:
        chunks.extend(
            chunk_document(
                spec, load_document(root, spec), settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP
            )
        )
    if not chunks or len({c.chunk_id for c in chunks}) != len(chunks):
        raise ValueError("Empty or duplicate snapshot")
    content = {
        "manifest": manifest,
        "chunks": [c.model_dump() for c in chunks],
        "chunk_contract": {
            k: v
            for k, v in settings.index_contract().items()
            if k in {"chunk_size", "overlap", "preprocessing", "chunker"}
        },
    }
    return {"snapshot_id": digest(content), **content}


def snapshot_chunks(snapshot):
    content = {k: v for k, v in snapshot.items() if k != "snapshot_id"}
    if digest(content) != snapshot["snapshot_id"]:
        raise ValueError("Snapshot hash mismatch")
    return [Chunk.model_validate(row) for row in snapshot["chunks"]]
