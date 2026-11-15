from __future__ import annotations

from experiments.memory_validation.confirmation_protocol_v1 import (
    CATEGORIES,
    CONTEXT_CHARS,
    EXCLUDED_CONVERSATIONS,
    QUESTIONS_PER_CATEGORY,
    reciprocal_rank_fusion,
    select_conversations,
    select_question_indices,
    session_metrics,
)


def _record(sample_id):
    qa = []
    for category in CATEGORIES:
        qa.extend(
            {
                "question": f"question-{sample_id}-{category}-{index}",
                "answer": "selection-must-not-read-this",
                "category": category,
                "evidence": [f"D{category}:{index}"],
            }
            for index in range(7)
        )
    return {"sample_id": sample_id, "qa": qa, "conversation": {}}


def test_conversation_and_question_selection_is_metadata_only_and_order_independent():
    records = [_record(f"conv-{index}") for index in range(30, 39)]
    chosen = select_conversations(records)
    assert len(chosen) == 2
    assert not set(chosen) & set(EXCLUDED_CONVERSATIONS)
    assert chosen == select_conversations(list(reversed(records)))
    by_id = {record["sample_id"]: record for record in records}
    first = select_question_indices(by_id[chosen[0]])
    assert first == select_question_indices(by_id[chosen[0]])
    assert len(first) == 3 * QUESTIONS_PER_CATEGORY
    assert {by_id[chosen[0]]["qa"][index]["category"] for index in first} == set(CATEGORIES)
    mutated = _record(chosen[0])
    for question in mutated["qa"]:
        question["question"] = "outcome-like text changed"
        question["answer"] = "other sentinel"
    assert select_question_indices(mutated) == first


def test_rrf_uses_fixed_k_top8_and_stable_key_tie_break():
    lexical = ["a", "b", "c"]
    semantic = ["b", "a", "d"]
    assert reciprocal_rank_fusion(lexical, semantic, k=60, top_k=8) == ["a", "b", "c", "d"]


def test_session_metrics_deduplicate_ranked_sessions():
    recall, mrr = session_metrics(["s1", "s1", "s2", "s3"], ["s1", "s3"])
    assert recall == 1.0
    assert mrr == 1.0
    recall, mrr = session_metrics(["s2", "s3"], ["s1", "s3"])
    assert recall == 0.5
    assert mrr == 0.5
    assert CONTEXT_CHARS == 6000
