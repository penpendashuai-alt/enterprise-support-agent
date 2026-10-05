import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from eval_support.schema import (  # noqa: E402
    Case,
    Claim,
    Dataset,
    Point,
    Review,
    Turn,
    check_sources,
    digest,
    load_dataset,
)
from eval_support.scoring import costs, export_reviews, ratio, summarize  # noqa: E402


def sample(answer="有依据的回答 [1]", ability="answerable", status="completed"):
    case = Case(
        case_id="case",
        family_id="family",
        split="dev",
        category="knowledge",
        answerability=ability,
        turns=[Turn(text="question")],
        points=[Point(point_id="point", description="required point")],
    )
    record = {
        "case_id": "case",
        "run_id": "run",
        "attempt": 1,
        "status": status,
        "final_ticket_count": 0,
        "turns": [
            {
                "http_status": 200,
                "response": {"content": answer, "custom_data": {"citation_check": "valid_numbers"}},
                "tools": [],
            }
        ],
    }
    review = Review(
        run_id="run",
        case_id="case",
        record_sha256=digest(record),
        status="reviewed",
        reviewer="developer assistant",
        reviewed_at="2026-10-03",
        rationale="Evidence reviewed",
        points={"point": "yes"},
        evidence_complete="yes",
        semantic_success="yes",
        refusal="none",
        missing_citations=0,
        claims=[
            Claim(
                turn=0,
                quote=answer,
                supported="yes",
                reason="provided evidence",
                citation_numbers=[1],
            )
        ],
    )
    return case, record, review


def test_all_refusal_cannot_game_quality():
    case, record, review = sample("无法回答")
    review.refusal = "full"
    review.claims = []
    review.points = {"point": "no"}
    result = summarize([case], [record], [review])
    assert result["confirmed_task_success_lower_bound"]["numerator"] == 0
    assert result["incorrect_refusal_rate"]["value"] == 1
    assert result["unsupported_answer_rate"]["value"] is None


def test_empty_answer_never_passes():
    case, record, review = sample("")
    review.claims = []
    assert (
        summarize([case], [record], [review])["confirmed_task_success_lower_bound"]["numerator"]
        == 0
    )


@pytest.mark.parametrize("ability", ["answerable", "partial"])
def test_partial_answer_omission_is_not_success(ability):
    case, record, review = sample(ability=ability)
    review.points = {"point": "no"}
    result = summarize([case], [record], [review])
    assert result["point_coverage_micro"]["value"] == 0
    assert result["confirmed_task_success_lower_bound"]["value"] == 0


def test_no_answer_correct_refusal_can_succeed():
    case, record, review = sample("没有相关金额依据", ability="unanswerable")
    review.refusal = "full"
    review.claims = []
    result = summarize([case], [record], [review])
    assert result["confirmed_task_success_lower_bound"]["value"] == 1
    assert result["unsupported_answer_rate"]["value"] is None


def test_legal_citation_is_not_support():
    case, record, review = sample()
    review.claims[0].supported = "no"
    result = summarize([case], [record], [review])
    assert result["unsupported_answer_rate"]["value"] == 1
    assert result["citation_support"]["value"] == 0
    assert result["confirmed_task_success_lower_bound"]["value"] == 0


def test_pending_not_failure_or_success_and_zero_denominators():
    case, record, _ = sample()
    result = summarize([case], [record], [Review.model_validate(export_reviews([record])[0])])
    assert result["counts"]["pending_review"] == 1
    assert result["point_coverage_micro"]["value"] is None
    assert ratio(0, 0)["value"] is None
    assert summarize([case], [], [])["counts"]["not_run"] == 1


def test_infrastructure_and_attempt_denominators():
    case, record, review = sample(status="infrastructure_error")
    repeat = {**record, "attempt": 2, "status": "completed"}
    result = summarize([case], [record, repeat], [review])
    assert result["task_count"] == 1 and result["attempt_count"] == 2
    assert result["confirmed_task_success_lower_bound"]["value"] == 0
    assert result["counts"]["infrastructure_error"] == 1


def test_multiturn_counts_as_one_task():
    case, record, review = sample()
    case.turns.append(Turn(text="followup"))
    record["turns"].append(record["turns"][0])
    review.record_sha256 = digest(record)
    result = summarize([case], [record], [review])
    assert result["task_count"] == 1 and result["logical_requests"] == 2


def test_stale_review_and_duplicate_records_rejected():
    case, record, review = sample()
    with pytest.raises(ValueError, match="Duplicate"):
        summarize([case], [record, record], [review])
    review.record_sha256 = "wrong"
    with pytest.raises(ValueError, match="bound"):
        summarize([case], [record], [review])


def test_cost_dedup_unknown_is_not_zero():
    event = {"call_id": "a", "provider_kind": "chat", "estimated_cny": 1}
    unknown = {"call_id": "b", "provider_kind": "embedding", "estimated_cny": None}
    result = costs([event, event, unknown])
    assert result["chat"]["known_cny"] == 1 and result["chat"]["attempts"] == 1
    assert result["embedding"]["unknown_usage"] == 1


def test_dataset_sources_and_split_contract():
    root = Path(__file__).resolve().parents[2]
    dataset = load_dataset(root / "evaluation/datasets/agent_v1.json")
    check_sources(dataset, root / "data/knowledge_v2")
    assert len(dataset.cases) == 60 and sum(c.split == "dev" for c in dataset.cases) == 36
    a = dataset.cases[0]
    b = a.model_copy(update={"case_id": "other", "split": "heldout"})
    with pytest.raises(ValueError, match="crosses"):
        Dataset(version="test", description="test", cases=[a, b])


def test_fabricated_review_excerpt_is_rejected():
    case, record, review = sample()
    review.claims[0].quote = "not in actual answer"
    with pytest.raises(ValueError, match="excerpt"):
        summarize([case], [record], [review])


def test_disjoint_shards_do_not_mix_repeated_experiments():
    case, record, review = sample()
    other = case.model_copy(update={"case_id": "other"})
    other_record = {**record, "case_id": "other", "run_id": "shard2"}
    summary = summarize([case, other], [record, other_record], [review])
    assert summary["task_count"] == 2
    repeated = {**record, "run_id": "another-experiment"}
    with pytest.raises(ValueError, match="Repeated"):
        summarize([case], [record, repeated], [review])
    assert summarize([case], [record, other_record], [review])["logical_requests"] == 1
