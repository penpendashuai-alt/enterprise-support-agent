from rag.models import Candidate, RAGError


def unique(items: list[Candidate]) -> list[Candidate]:
    result = {}
    for item in items:
        result.setdefault(item.chunk_id, item)
    return list(result.values())


def reciprocal_rank_fusion(
    dense: list[Candidate], lexical: list[Candidate], c: int = 60
) -> list[Candidate]:
    if c < 1:
        raise ValueError("RRF constant must be positive")
    combined = {}
    for source, rows in [("dense", dense), ("bm25", lexical)]:
        for rank, item in enumerate(unique(rows), 1):
            if item.chunk_id in combined:
                old = combined[item.chunk_id]
                if (old.content_hash, old.snapshot_id) != (item.content_hash, item.snapshot_id):
                    raise RAGError("index_candidate_mismatch")
            else:
                combined[item.chunk_id] = item.model_copy(update={"rrf_score": 0.0})
            merged = combined[item.chunk_id]
            setattr(merged, f"{source}_rank", rank)
            setattr(merged, f"{source}_score", getattr(item, f"{source}_score"))
            merged.rrf_score += 1 / (c + rank)
            merged.score = merged.rrf_score
            merged.score_type = "rrf"
    return sorted(combined.values(), key=lambda item: (-item.score, item.chunk_id))
