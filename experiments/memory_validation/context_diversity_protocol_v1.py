"""Pre-result selection and dense-diversity retrieval rules for LoCoMo context v1."""

from __future__ import annotations

from collections import Counter
import math
import random


PROTOCOL = "locomo-context-diversity-v1"
SEED = 20261116
EXCLUDED_CONVERSATIONS = ("conv-30", "conv-41", "conv-42", "conv-43", "conv-47")
CATEGORIES = (1, 2, 4)
CATEGORY_NAMES = {1: "multi_hop", 2: "temporal", 4: "single_hop"}
QUESTIONS_PER_CATEGORY_PER_CONVERSATION = 4
QUESTIONS_PER_CATEGORY = 8
ARMS = ("semantic_top8", "semantic_mmr_top8", "semantic_session_cap2")
TOP_K = 8
CONTEXT_CHARS = 6000
RAW_CHUNK_CHARS = 800
MMR_LAMBDA = 0.7
SESSION_CAP = 2
SCORE_DECIMALS = 12


def select_conversations(records):
    """Use only IDs and category/evidence metadata, never questions or outcomes."""
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
        if all(counts[category] >= QUESTIONS_PER_CATEGORY_PER_CONVERSATION for category in CATEGORIES):
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
        if len(eligible) < QUESTIONS_PER_CATEGORY_PER_CONVERSATION:
            raise ValueError("selected conversation has insufficient category metadata")
        random.Random(f"{PROTOCOL}:{SEED}:{conversation_id}:{category}").shuffle(eligible)
        selected.extend(eligible[:QUESTIONS_PER_CATEGORY_PER_CONVERSATION])
    return sorted(selected)


def freeze_selection(records):
    by_id = {record["sample_id"]: record for record in records}
    conversations = select_conversations(records)
    result = [
        {
            "conversation_id": conversation_id,
            "indices": select_question_indices(by_id[conversation_id]),
        }
        for conversation_id in conversations
    ]
    if Counter(
        by_id[item["conversation_id"]]["qa"][index]["category"]
        for item in result
        for index in item["indices"]
    ) != Counter({category: QUESTIONS_PER_CATEGORY for category in CATEGORIES}):
        raise ValueError("question selection is not category balanced")
    return result


def cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimensions differ")
    score = sum(a * b for a, b in zip(left, right, strict=True))
    if not math.isfinite(score):
        raise ValueError("embedding similarity is not finite")
    return round(score, SCORE_DECIMALS)


def semantic_ranking(query_vector, corpus, vectors):
    """Rank every raw chunk by rounded cosine, then lexical chunk key."""
    by_key = {row["key"]: row for row in corpus}
    if len(by_key) != len(corpus) or set(by_key) != set(vectors):
        raise ValueError("corpus and vector identities differ")
    ranked = [(cosine(query_vector, vectors[key]), key) for key in by_key]
    return sorted(ranked, key=lambda item: (-item[0], item[1]))


def take_top(ranking, *, top_k=TOP_K):
    if not 1 <= top_k <= len(ranking):
        raise ValueError("top-k boundary differs")
    return [key for _, key in ranking[:top_k]]


def mmr_select(ranking, vectors, *, lambda_=MMR_LAMBDA, top_k=TOP_K):
    """Select MMR with lambda*relevance-(1-lambda)*max-selected-similarity.

    The first item is the highest semantic-ranked chunk. Later ties are broken by
    higher relevance, then lexicographically smaller chunk key.
    """
    if not 0 <= lambda_ <= 1 or not 1 <= top_k <= len(ranking):
        raise ValueError("MMR bounds differ")
    if len({key for _, key in ranking}) != len(ranking):
        raise ValueError("MMR ranking identities differ")
    remaining = sorted(ranking, key=lambda item: (-item[0], item[1]))
    selected = [remaining.pop(0)[1]]
    details = [{"key": selected[0], "step": 1, "relevance": ranking[0][0], "max_selected_similarity": None, "mmr": None}]
    while len(selected) < top_k:
        candidates = []
        for relevance, key in remaining:
            max_similarity = max(cosine(vectors[key], vectors[chosen]) for chosen in selected)
            mmr = round(lambda_ * relevance - (1 - lambda_) * max_similarity, SCORE_DECIMALS)
            candidates.append((mmr, relevance, key, max_similarity))
        mmr, relevance, key, max_similarity = min(candidates, key=lambda item: (-item[0], -item[1], item[2]))
        selected.append(key)
        details.append(
            {
                "key": key,
                "step": len(selected),
                "relevance": relevance,
                "max_selected_similarity": max_similarity,
                "mmr": mmr,
            }
        )
        remaining = [item for item in remaining if item[1] != key]
    return selected, details


def session_cap_select(ranking, corpus, *, cap=SESSION_CAP, top_k=TOP_K):
    """Take semantic order with at most cap chunks per session, then backfill."""
    if not 1 <= cap <= top_k <= len(ranking):
        raise ValueError("session-cap bounds differ")
    sessions = {row["key"]: row["session_id"] for row in corpus}
    selected, counts, capped_out = [], Counter(), 0
    for _, key in ranking:
        session = sessions[key]
        if counts[session] >= cap:
            capped_out += 1
            continue
        selected.append(key)
        counts[session] += 1
        if len(selected) == top_k:
            break
    backfilled = []
    if len(selected) < top_k:
        for _, key in ranking:
            if key not in selected:
                selected.append(key)
                backfilled.append(key)
                if len(selected) == top_k:
                    break
    if len(selected) != top_k or len(set(selected)) != top_k:
        raise ValueError("session-cap selection cannot fill top-k")
    return selected, {"capped_out_before_fill": capped_out, "backfilled": backfilled}


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
