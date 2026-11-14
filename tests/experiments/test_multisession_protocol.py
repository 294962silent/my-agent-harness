"""Freeze selection without reading gold; shared corpus and retrieval equality."""

import pytest
from experiments.memory_validation.multisession_protocol import (
    freeze_selection,
    session_documents,
    raw_chunks,
    bm25_order,
    render_context,
)


class Gold(dict):
    def __getitem__(self, key):
        if key in ("answer", "adversarial_answer"):
            raise AssertionError("selection inspected gold")
        return super().__getitem__(key)


def samples():
    result = []
    for conv in ("conv-47", "conv-41", "conv-42"):
        result.append(
            {
                "sample_id": conv,
                "conversation": {
                    "session_1_date_time": "date",
                    "session_1": [
                        {"dia_id": "D1:1", "speaker": "A", "text": "astronomy telescope hobby"},
                        {"dia_id": "D1:2", "speaker": "B", "text": "art and music"},
                    ],
                },
                "qa": [Gold(question=f"q{c}-{i}", category=c, evidence=["D1:1"]) for c in (1, 2, 4) for i in range(6)],
            }
        )
    return result


def test_frozen_selection_is_balanced_gold_independent_and_order_invariant():
    chosen = freeze_selection(samples())
    assert chosen == freeze_selection(list(reversed(samples())))
    assert [len(x["questions"]) for x in chosen] == [6, 12, 12]
    assert [x["split"] for x in chosen] == ["dev", "test", "test"]
    assert len({(r["conversation_id"], q["qa_index"]) for r in chosen for q in r["questions"]}) == 30


def test_selected_original_question_evidence_change_is_detectable():
    first = freeze_selection(samples())
    changed = samples()
    changed[0]["qa"][first[0]["questions"][0]["qa_index"]]["evidence"] = ["D1:2"]
    assert first != freeze_selection(changed)


def test_sessions_and_raw_chunks_preserve_complete_turns_and_ids():
    sample = samples()[0]
    docs = session_documents(sample)
    chunks = raw_chunks(sample)
    assert len(docs) == 1 and docs[0]["turn_ids"] == ["D1:1", "D1:2"]
    assert "[D1:1] A: astronomy" in docs[0]["text"]
    assert [t for c in chunks for t in c["turn_ids"]] == ["D1:1", "D1:2"]
    context = render_context(chunks, [c["key"] for c in chunks], 6000)
    assert context["turn_ids"] == ["D1:1", "D1:2"]


def test_strong_bm25_and_whole_chunk_context_bound():
    corpus = [
        {"key": "a", "text": "astronomy telescope", "turn_ids": ["D1:1"], "session_id": "s1"},
        {"key": "b", "text": "music music music", "turn_ids": ["D1:2"], "session_id": "s1"},
    ]
    assert bm25_order(corpus, "astronomy telescope", 8)[0] == "a"
    rendered = render_context(corpus, ["a", "b"], len("[a] astronomy telescope") + 1)
    assert rendered["chunk_keys"] == ["a"] and rendered["turn_ids"] == ["D1:1"]
    assert len(rendered["context"]) <= len("[a] astronomy telescope") + 1


def test_long_turn_is_rejected_instead_of_silently_truncated():
    sample = samples()[0]
    sample["conversation"]["session_1"][0]["text"] = "x" * 7001
    with pytest.raises(ValueError):
        session_documents(sample)
