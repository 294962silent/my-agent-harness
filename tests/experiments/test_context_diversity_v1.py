from __future__ import annotations

from experiments.memory_validation.context_diversity_protocol_v1 import (
    CATEGORIES,
    CONTEXT_CHARS,
    EXCLUDED_CONVERSATIONS,
    QUESTIONS_PER_CATEGORY,
    freeze_selection,
    mmr_select,
    select_conversations,
    session_cap_select,
    session_metrics,
)


def _record(sample_id):
    qa = []
    for category in CATEGORIES:
        qa.extend(
            {
                "question": "selection-must-not-read-this",
                "answer": "selection-must-not-read-this",
                "category": category,
                "evidence": [f"D{category}:{index}"],
            }
            for index in range(7)
        )
    return {"sample_id": sample_id, "qa": qa, "conversation": {}}


def test_selection_uses_fixed_seed_and_metadata_only():
    records = [_record(f"conv-{index}") for index in range(20, 30)]
    chosen = select_conversations(records)
    assert len(chosen) == 2
    assert not set(chosen) & set(EXCLUDED_CONVERSATIONS)
    assert chosen == select_conversations(list(reversed(records)))
    frozen = freeze_selection(records)
    assert sum(len(item["indices"]) for item in frozen) == 24
    mutated = [_record(record["sample_id"]) for record in records]
    for record in mutated:
        for question in record["qa"]:
            question["question"] = "outcome-like text changed"
            question["answer"] = "other sentinel"
    assert freeze_selection(mutated) == frozen
    assert QUESTIONS_PER_CATEGORY == 8
    assert CONTEXT_CHARS == 6000


def test_mmr_formula_and_deterministic_tie_break():
    ranking = [(1.0, "a"), (0.9, "b"), (0.8, "c")]
    vectors = {"a": [1.0, 0.0], "b": [0.98, 0.2], "c": [0.0, 1.0]}
    selected, detail = mmr_select(ranking, vectors, lambda_=0.7, top_k=2)
    assert selected == ["a", "c"]
    assert detail[1]["mmr"] == 0.56
    tied = mmr_select([(0.5, "z"), (0.5, "a")], {"z": [1.0], "a": [1.0]}, top_k=2)
    assert tied[0] == ["a", "z"]


def test_session_cap_and_explicit_backfill():
    ranking = [(1.0, "a"), (0.9, "b"), (0.8, "c")]
    corpus = [
        {"key": "a", "session_id": "s1"},
        {"key": "b", "session_id": "s1"},
        {"key": "c", "session_id": "s1"},
    ]
    selected, detail = session_cap_select(ranking, corpus, cap=2, top_k=3)
    assert selected == ["a", "b", "c"]
    assert detail["backfilled"] == ["c"]


def test_session_metrics_deduplicate_ranked_sessions():
    assert session_metrics(["s1", "s1", "s2"], ["s1", "s2"]) == (1.0, 1.0)
    assert session_metrics(["s2", "s3"], ["s1", "s3"]) == (0.5, 0.5)
