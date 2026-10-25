"""LongMemEval 跑批入口。

用法：
    uv run python experiments/longmemeval/run_eval.py --arm full --per-type 8
    uv run python experiments/longmemeval/run_eval.py --arm truncate --per-type 8 --budget-tokens 2000
    uv run python experiments/longmemeval/run_eval.py --arm v2 --per-type 8

arm 说明：
    full      全量历史塞进上下文（上限参照，代表"上下文够大"时）
    truncate  只保留最近 N token（朴素滑窗，代表"不做记忆"）
    v2        抽取-巩固-检索三层记忆（本项目的改动）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.longmemeval.common import (  # noqa: E402
    DEFAULT_MODEL,
    build_answer_prompt,
    load_dataset,
    make_client,
    render_sessions,
    truncate_tokens,
)
from experiments.longmemeval.memory_v2 import (  # noqa: E402
    MemoryV2,
    build_fact_extraction_prompt,
    parse_facts,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def expand_query(client, question: str, model: str) -> tuple[str, int]:
    """把问题改写成 3 条检索关键词（含同义词/时间词），提升 BM25 召回。"""
    prompt = (
        "Rewrite the question below into 3 short keyword search queries that would help find "
        "the answer in a chat history. Include synonyms and any dates/entities. "
        "Output one query per line, no numbering, no prose.\n\nQuestion: " + question
    )
    try:
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=256
        )
        text = (resp.choices[0].message.content or "").strip()
        tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
        queries = [line.strip() for line in text.splitlines() if line.strip()][:3]
        return " ".join([question, *queries]), tokens
    except Exception:  # noqa: BLE001
        return question, 0


def extract_scoped_facts(client, sessions: list[tuple[str, str]], model: str) -> tuple[list, int]:
    """只对检索到的会话抽事实，成本与检索结果成正比（不随历史长度增长）。"""
    from experiments.longmemeval.memory_v2 import parse_facts

    history = "\n\n".join(f"### {date}\n{text}" for date, text in sessions)
    prompt = (
        "You extract durable facts from a user's chat history. Each session is dated.\n"
        "Return AT MOST 40 atomic facts as a JSON array, each item:\n"
        '{"subject": "user|assistant|project", "key": "short_snake_case", '
        '"value": "the fact with exact numbers/dates", "valid_from": "YYYY-MM-DD", "importance": 0.0-1.0}\n'
        "Rules: one fact per item; stable keys so later values supersede earlier ones; "
        "importance 1.0 for preferences/decisions, 0.3 for incidental. JSON only.\n\n"
        f"History:\n{history}"
    )
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=2048
    )
    raw = resp.choices[0].message.content or ""
    tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
    return parse_facts(raw), tokens


def extract_profile(client, sessions: list[tuple[str, str]], model: str) -> tuple[str, int]:
    """从检索到的会话里抽取"用户画像"：偏好、约束、长期项目。"""
    history = "\n\n".join(f"### {date}\n{text}" for date, text in sessions)
    prompt = (
        "From the chat sessions below, write a short profile of the user: stable preferences, "
        "constraints, ongoing projects, and facts about their life. "
        "Up to 8 bullets, each one line, only what the sessions explicitly support. "
        "No preamble, bullets only.\n\n" + history
    )
    # 该模型会先输出 reasoning，预算给小了 content 会是空字符串（静默失败）
    for budget in (1024, 2048):
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=budget
        )
        text = (resp.choices[0].message.content or "").strip()
        tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
        if text:
            return text[:900], tokens
    return "", tokens


def prepare_context(client, record, arm: str, budget_tokens: int, model: str, k_chunks: int = 10) -> tuple[str, int]:
    """返回 (注入上下文, 额外消耗的 prompt tokens)。"""
    history = render_sessions(record)
    if arm == "full":
        return history, 0
    if arm == "rag":
        # 纯 BM25 检索（不做 LLM 抽取）：长历史下唯一负担得起的最强基线
        memory = MemoryV2()
        memory.ingest(record)
        query, expand_tokens = expand_query(client, record["question"], model)
        retrieved = memory.retrieve(query, k_facts=0, k_chunks=12)
        body = memory.render_context(retrieved, max_chars=budget_tokens * 3)
        recent = memory.render_recent(n=2, max_chars=2500)
        context = (recent + "\n\n" + body) if recent else body
        return context, expand_tokens
    if arm == "truncate":
        return truncate_tokens(history, budget_tokens), 0
    if arm == "v3":
        # 完整三层：画像(抽取) + 时间线 + 事实(巩固,带时序) + 检索
        query, t1 = expand_query(client, record["question"], model)
        memory = MemoryV2()
        memory.ingest(record)
        memory.retrieve(query, k_facts=0, k_chunks=k_chunks)
        sessions = memory.pick_sessions(max_sessions=8)
        profile, t2 = extract_profile(client, sessions, model)
        facts, t3 = extract_scoped_facts(client, sessions, model)
        memory.consolidator.facts.clear()
        for fact in facts:
            memory.consolidator.add(fact)
        timeline = memory.render_timeline(max_chars=2200)
        body = memory.render_context(memory.retrieve(query, k_facts=16, k_chunks=k_chunks), max_chars=budget_tokens * 3)
        parts = []
        if profile:
            parts.append("## User profile (from past sessions)\n" + profile)
        if timeline:
            parts.append(timeline)
        parts.append(body)
        return "\n\n".join(parts), t1 + t2 + t3
    if arm == "v2p":
        # 画像 + 时间线 + 检索：分别补 preference 与 temporal 两个短板
        query, t_expand = expand_query(client, record["question"], model)
        memory = MemoryV2()
        memory.ingest(record)
        memory.retrieve(query, k_facts=0, k_chunks=12)
        sessions = memory.pick_sessions(max_sessions=8)
        profile, t_profile = extract_profile(client, sessions, model)
        timeline = memory.render_timeline(max_chars=2200)
        body = memory.render_context(memory.retrieve(query, k_facts=0, k_chunks=k_chunks), max_chars=budget_tokens * 3)
        parts = []
        if profile:
            parts.append("## User profile (from past sessions)\n" + profile)
        if timeline:
            parts.append(timeline)
        parts.append(body)
        return "\n\n".join(parts), t_expand + t_profile
    if arm == "v2s":
        # 先检索再抽取：把抽取成本压到"检索到的 6 个会话"，而不是整段历史
        query, t_expand = expand_query(client, record["question"], model)
        memory = MemoryV2()
        memory.ingest(record)
        memory.retrieve(query, k_facts=0, k_chunks=12)
        sessions = memory.pick_sessions(max_sessions=6)
        facts, t_extract = extract_scoped_facts(client, sessions, model)
        memory.consolidator.facts.clear()
        for f in facts:
            memory.consolidator.add(f)
        enriched = memory.retrieve(query, k_facts=16, k_chunks=k_chunks)
        body = memory.render_context(enriched, max_chars=budget_tokens * 3)
        recent = memory.render_recent(n=1, max_chars=1500)
        ctx = (recent + "\n\n" + body) if recent else body
        return ctx, t_expand + t_extract
    if arm == "v2":
        prompt = build_fact_extraction_prompt(history)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2048,
        )
        raw = resp.choices[0].message.content or ""
        extra = getattr(resp.usage, "prompt_tokens", 0) or 0
        facts = parse_facts(raw)
        memory = MemoryV2()
        memory.ingest(record, facts)
        retrieved = memory.retrieve(record["question"], k_facts=20, k_chunks=8)
        return memory.render_context(retrieved, max_chars=budget_tokens * 3), extra
    raise SystemExit(f"未知 arm: {arm}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", default="full", choices=["full", "truncate", "rag", "v2", "v2s", "v2p", "v3"])
    parser.add_argument("--dataset", default="oracle", choices=["oracle", "s", "m"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--per-type", type=int, default=0, help="每类抽 N 题（分层抽样，优先于 --limit）")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--budget-tokens", type=int, default=2000)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--answer-style", default="default", choices=["default", "memory"])
    parser.add_argument("--k-chunks", type=int, default=10)
    args = parser.parse_args()

    data = load_dataset(args.dataset)
    if args.per_type:
        by_type: dict[str, list[dict]] = {}
        for rec in data:
            by_type.setdefault(rec["question_type"], []).append(rec)
        subset = []
        for qtype in sorted(by_type):
            subset.extend(by_type[qtype][: args.per_type])
        subset.sort(key=lambda r: r["question_id"])
        limit_tag = f"{args.dataset}_pt{args.per_type}"
    else:
        subset = data[args.offset : args.offset + args.limit]
        limit_tag = f"{args.dataset}_{args.limit}"
    client = make_client()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = args.out or RESULTS_DIR / f"hyp_{args.arm}_{limit_tag}.jsonl"

    t0 = time.time()
    with out_path.open("w", encoding="utf-8") as fh:
        for i, record in enumerate(subset, 1):
            try:
                context, extra_tokens = prepare_context(
                    client, record, args.arm, args.budget_tokens, args.model, args.k_chunks
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{i}] CONTEXT ERROR {record['question_id']}: {exc}", file=sys.stderr)
                context, extra_tokens = "", 0

            messages = build_answer_prompt(record, context, style=args.answer_style)
            try:
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=messages,
                    temperature=args.temperature,
                    max_tokens=512,
                )
                hypothesis = (resp.choices[0].message.content or "").strip()
                usage = resp.usage
                tokens = getattr(usage, "prompt_tokens", None) if usage else None
            except Exception as exc:  # noqa: BLE001
                hypothesis = ""
                tokens = None
                print(f"[{i}] ANSWER ERROR {record['question_id']}: {exc}", file=sys.stderr)

            fh.write(
                json.dumps(
                    {
                        "question_id": record["question_id"],
                        "question_type": record["question_type"],
                        "arm": args.arm,
                        "hypothesis": hypothesis,
                        "context_chars": len(context),
                        "prompt_tokens": tokens,
                        "extraction_tokens": extra_tokens,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            if i % 5 == 0 or i == len(subset):
                print(f"[{args.arm}] {i}/{len(subset)}  {time.time() - t0:.0f}s", file=sys.stderr)

    print(f"完成 {len(subset)} 题 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
