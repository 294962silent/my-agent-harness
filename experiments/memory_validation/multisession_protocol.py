"""Pre-result LoCoMo split, full-session ingestion and matched retrieval corpus."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import random
import re

ARMS = ("raw_lexical", "raw_mem0", "facts_lexical", "facts_mem0")
CONVERSATIONS = (("dev", "conv-47", 2), ("test", "conv-41", 4), ("test", "conv-42", 4))
CATEGORIES = {1: "multi_hop", 2: "temporal", 4: "single_hop"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def freeze_selection(samples):
    """Fixed conversation IDs from metadata-only seed24 draw; never read gold.

    Select a fixed category quota using a question/index-derived seeded shuffle.
    IDs and original QA/evidence hashes permit readback without publishing text.
    Open-domain3 and adversarial5 are excluded before observing any outcome.
    """
    by_id = {row["sample_id"]: row for row in samples}
    if len(by_id) != len(samples):
        raise ValueError("duplicate conversation ID")
    result = []
    for split, conv, per_category in CONVERSATIONS:
        sample = by_id[conv]
        questions = []
        for category in CATEGORIES:
            eligible = [i for i, q in enumerate(sample["qa"]) if q["category"] == category and q.get("evidence")]
            if len(eligible) < per_category:
                raise ValueError("frozen conversation category has insufficient evidence-labeled QA")
            random.Random("locomo-v1:24:" + conv + ":" + str(category)).shuffle(eligible)
            for index in eligible[:per_category]:
                q = sample["qa"][index]
                identity = "q_" + sha([conv, index])[:24]
                questions.append(
                    {
                        "question_id": identity,
                        "qa_index": index,
                        "category": category,
                        "question_type": CATEGORIES[category],
                        "question_sha256": sha(q["question"]),
                        "evidence_sha256": sha(q["evidence"]),
                        "evidence_ids": list(q["evidence"]),
                    }
                )
        result.append(
            {"split": split, "conversation_id": conv, "questions": sorted(questions, key=lambda q: q["qa_index"])}
        )
    return result


def _sessions(sample):
    conversation = sample["conversation"]
    names = sorted(
        [k for k, v in conversation.items() if k.startswith("session_") and isinstance(v, list)],
        key=lambda k: int(k.split("_")[1]),
    )
    for name in names:
        yield name, str(conversation.get(name + "_date_time", "unavailable")), conversation[name]


def session_documents(sample, limit=7000):
    result = []
    for name, date, turns in _sessions(sample):
        header = "Session " + name + "; date " + date + "\n"
        text, ids, part = header, [], 0
        for turn in turns:
            line = "[" + turn["dia_id"] + "] " + turn["speaker"] + ": " + turn["text"] + "\n"
            if turn.get("blip_caption"):
                line = line.rstrip("\n") + " [Provided image caption: " + str(turn["blip_caption"]) + "]\n"
            if len(header + line) > limit:
                raise ValueError("a complete original turn exceeds ingestion bound")
            if len(text + line) > limit and ids:
                result.append(_document(sample["sample_id"], name, part, text, ids))
                part += 1
                text, ids = header, []
            text += line
            ids.append(turn["dia_id"])
        if ids:
            result.append(_document(sample["sample_id"], name, part, text, ids))
    return result


def _document(conv, session, part, text, ids):
    return {
        "key": "s_" + sha([conv, session, part])[:24],
        "session_id": session,
        "part": part,
        "text": text,
        "turn_ids": list(ids),
        "sha256": sha(text),
    }


def raw_chunks(sample, limit=800):
    """Identical original-turn chunks for raw lexical and raw Mem0.

    Greedy whole turns plus date header; no partial-turn evidence credit.
    """
    return session_documents(sample, limit=limit)


def bm25_order(corpus, query, top_k=8):
    """Standard BM25 k1=1.5,b=.75, no stop list or result-dependent tuning."""

    def tokenize(text):
        return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())

    terms = [Counter(tokenize(row["text"])) for row in corpus]
    lengths = [sum(row.values()) for row in terms]
    average = sum(lengths) / max(len(lengths), 1)
    query_terms = Counter(tokenize(query))
    scores = []
    for index, frequencies in enumerate(terms):
        score = 0.0
        for term, qf in query_terms.items():
            df = sum(term in row for row in terms)
            tf = frequencies.get(term, 0)
            idf = math.log(1 + (len(terms) - df + 0.5) / (df + 0.5))
            denominator = tf + 1.5 * (1 - 0.75 + 0.75 * lengths[index] / max(average, 1))
            score += qf * idf * tf * 2.5 / denominator if denominator else 0
        scores.append((score, corpus[index]["key"]))
    # Zero overlap remains a ranked candidate, matching the vector top-k policy.
    return [key for _, key in sorted(scores, key=lambda row: (-row[0], row[1]))[:top_k]]


def render_context(corpus, keys, limit=6000):
    by_id = {row["key"]: row for row in corpus}
    if len(keys) > 8 or len(set(keys)) != len(keys) or set(keys) - by_id.keys():
        raise ValueError("retrieval candidate identity differs")
    lines, included, turns, sessions = [], [], [], []
    for key in keys:
        row = by_id[key]
        line = "[" + key + "] " + row["text"]
        if len("\n".join(lines + [line])) > limit:
            continue
        lines.append(line)
        included.append(key)
        turns.extend(t for t in row.get("turn_ids", []) if t not in turns)
        if row.get("session_id") and row["session_id"] not in sessions:
            sessions.append(row["session_id"])
    return {
        "context": "\n".join(lines),
        "chunk_keys": included,
        "turn_ids": turns,
        "session_ids": sessions,
        "context_chars": len("\n".join(lines)),
    }
