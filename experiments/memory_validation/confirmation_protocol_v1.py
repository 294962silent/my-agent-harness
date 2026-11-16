"""Pre-result selection and rank-fusion rules for the raw confirmation set."""

from __future__ import annotations

from collections import Counter
import random


PROTOCOL = "locomo-raw-confirmation-v1"
SEED = 20261115
EXCLUDED_CONVERSATIONS = ("conv-41", "conv-42", "conv-47")
CATEGORIES = (1, 2, 4)
CATEGORY_NAMES = {1: "multi_hop", 2: "temporal", 4: "single_hop"}
QUESTIONS_PER_CATEGORY = 4
ARMS = ("raw_lexical", "raw_semantic_qwen", "raw_hybrid_rrf")
TOP_K = 8
CONTEXT_CHARS = 6000
RAW_CHUNK_CHARS = 800
RRF_K = 60


def select_conversations(records):
    """Select from IDs and category/evidence metadata only, never model outcomes."""
    if not isinstance(records, list) or not records:
        raise ValueError("LoCoMo records are unavailable")
    ids = [record.get("sample_id") for record in records]
    if any(not isinstance(sample_id, str) or not sample_id for sample_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("conversation identities differ")
    excluded = set(EXCLUDED_CONVERSATIONS)
    eligible = []
    for record in records:
        sample_id = record["sample_id"]
        if sample_id in excluded:
            continue
        counts = Counter(
            question.get("category")
            for question in record.get("qa", [])
            if question.get("evidence")
        )
        if all(counts[category] >= QUESTIONS_PER_CATEGORY for category in CATEGORIES):
            eligible.append(sample_id)
    eligible.sort()
    if len(eligible) < 2:
        raise ValueError("fewer than two metadata-eligible unused conversations")
    random.Random(f"{PROTOCOL}:{SEED}").shuffle(eligible)
    return eligible[:2]


def select_question_indices(sample):
    conversation_id = sample["sample_id"]
    selected = []
    for category in CATEGORIES:
        eligible = [
            index
            for index, question in enumerate(sample.get("qa", []))
            if question.get("category") == category and question.get("evidence")
        ]
        if len(eligible) < QUESTIONS_PER_CATEGORY:
            raise ValueError("selected conversation has insufficient category metadata")
        random.Random(f"{PROTOCOL}:{SEED}:{conversation_id}:{category}").shuffle(eligible)
        selected.extend(eligible[:QUESTIONS_PER_CATEGORY])
    return sorted(selected)


def freeze_selection(records):
    by_id = {record["sample_id"]: record for record in records}
    conversations = select_conversations(records)
    return [
        {
            "conversation_id": conversation_id,
            "indices": select_question_indices(by_id[conversation_id]),
        }
        for conversation_id in conversations
    ]


def reciprocal_rank_fusion(lexical_keys, semantic_keys, *, k=RRF_K, top_k=TOP_K):
    """Fuse the two frozen top-k lists while keeping the same output budget."""
    if not 0 < k <= 1000 or not 1 <= top_k <= 100:
        raise ValueError("RRF bounds differ")
    for name, keys in (("lexical", lexical_keys), ("semantic", semantic_keys)):
        if (
            not isinstance(keys, list)
            or len(keys) > top_k
            or len(keys) != len(set(keys))
            or any(not isinstance(key, str) or not key for key in keys)
        ):
            raise ValueError(name + " candidate list differs")
    scores = {}
    for keys in (lexical_keys, semantic_keys):
        for rank, key in enumerate(keys, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return [key for key, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]]


def session_metrics(retrieved_sessions, expected_sessions):
    expected = list(dict.fromkeys(expected_sessions))
    ranked = list(dict.fromkeys(retrieved_sessions))
    if not expected:
        return None, None
    recall = len(set(expected).intersection(ranked)) / len(expected)
    reciprocal = 0.0
    for rank, session in enumerate(ranked, 1):
        if session in expected:
            reciprocal = 1.0 / rank
            break
    return recall, reciprocal
