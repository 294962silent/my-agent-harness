"""记忆系统 v2：抽取 → 巩固 → 检索 三层。

设计（对应 LongMemEval 的五项记忆能力）：

1. **抽取**：把每个会话切成 chunk，同时用一次 LLM 调用把整段历史抽成原子事实
   ``{subject, key, value, valid_from, importance}``。
2. **巩固**：同 ``(subject, key)`` 的新事实会 supersede 旧事实（``valid_to`` 置为新事实
   的日期），检索时默认只返回仍然有效的事实 —— 对应 Knowledge Updates。
3. **检索**：``score = 0.6*BM25 + 0.3*recency + 0.1*importance``，在 token 预算内取 top-k，
   pin 住的事实（preference / 明确约定）永远注入。

与上游 `MemoryStore` 的区别：上游是全量注入 + 字符上限裁剪，这里是按相关性检索注入。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")

_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "and",
    "or",
    "in",
    "on",
    "for",
    "with",
    "at",
    "by",
    "it",
    "this",
    "that",
    "i",
    "you",
    "my",
    "me",
    "we",
    "did",
    "do",
    "does",
    "what",
    "when",
    "which",
    "who",
    "how",
    "many",
    "much",
}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS]


def parse_date(value: str) -> datetime | None:
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


@dataclass
class Chunk:
    """一个可检索的文本片段（来自某个会话）。"""

    session_id: str
    date: str
    text: str
    tokens: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = tokenize(self.text)


@dataclass
class Fact:
    """一条原子事实，带时序有效性。"""

    subject: str
    key: str
    value: str
    valid_from: str
    importance: float = 0.5
    valid_to: str | None = None
    source_session: str | None = None

    @property
    def active(self) -> bool:
        return self.valid_to is None

    def render(self) -> str:
        span = f"[{self.valid_from}]" if not self.valid_to else f"[{self.valid_from} → {self.valid_to}]"
        mark = "" if self.active else " (superseded)"
        return f"- {span} {self.subject}.{self.key} = {self.value}{mark}"


class BM25:
    """极简 BM25（k1=1.5, b=0.75），避免引入依赖。"""

    def __init__(self, docs: list[list[str]]) -> None:
        self.docs = docs
        self.n = len(docs)
        self.avgdl = sum(len(d) for d in docs) / self.n if self.n else 0.0
        self.df: dict[str, int] = {}
        for doc in docs:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: list[str], index: int) -> float:
        doc = self.docs[index]
        if not doc:
            return 0.0
        tf: dict[str, int] = {}
        for term in doc:
            tf[term] = tf.get(term, 0) + 1
        total = 0.0
        dl = len(doc)
        for term in query_tokens:
            if term not in tf:
                continue
            freq = tf[term]
            denom = freq + 1.5 * (1 - 0.75 + 0.75 * dl / (self.avgdl or 1))
            total += self.idf(term) * freq * 2.5 / denom
        return total


def chunk_record(record: dict[str, Any], window_chars: int = 1200) -> list[Chunk]:
    """把会话切成 ~1200 字符的滑窗 chunk。

    整会话当一个 chunk 太粗（一个会话上万字符），BM25 只能命中"整段"，
    噪音大且挤占预算；切成小窗后能精确定位到具体几轮对话。
    """
    chunks: list[Chunk] = []
    for date, session, sid in zip(
        record["haystack_dates"], record["haystack_sessions"], record["haystack_session_ids"], strict=False
    ):
        lines: list[str] = []
        for msg in session:
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            lines.append(f"{msg.get('role', 'user')}: {content}")

        buf: list[str] = []
        size = 0
        for line in lines:
            buf.append(line)
            size += len(line) + 1
            if size >= window_chars:
                chunks.append(Chunk(session_id=f"{sid}#{len(buf)}", date=date, text="\n".join(buf)))
                buf, size = [], 0
        if buf:
            chunks.append(Chunk(session_id=f"{sid}#tail", date=date, text="\n".join(buf)))
    return chunks


class Consolidator:
    """按 (subject, key) 维护事实的时序有效性。"""

    def __init__(self) -> None:
        self.facts: list[Fact] = []

    def add(self, fact: Fact) -> None:
        for old in self.facts:
            if (
                old.active
                and old.subject == fact.subject
                and old.key == fact.key
                and old.value.strip().lower() != fact.value.strip().lower()
            ):
                old.valid_to = fact.valid_from
        self.facts.append(fact)

    def active(self) -> list[Fact]:
        return [f for f in self.facts if f.active]


def build_fact_extraction_prompt(history: str, max_facts: int = 60) -> str:
    return (
        "You extract durable facts from a user's chat history.\n"
        f"Return AT MOST {max_facts} atomic facts as a JSON array. Each item:\n"
        '{"subject": "user|assistant|project", "key": "short_snake_case", '
        '"value": "the fact", "valid_from": "YYYY-MM-DD", "importance": 0.0-1.0}\n\n'
        "Rules:\n"
        "- One fact per item; keys must be stable so later facts can supersede earlier ones.\n"
        "- Keep dates, numbers, names exact.\n"
        "- importance: 1.0 for stable preferences/decisions, 0.3 for incidental details.\n"
        "- Output JSON only, no prose.\n\n"
        f"History:\n{history}"
    )


def parse_facts(raw: str) -> list[Fact]:
    """从模型输出里稳健地解析事实数组。"""
    text = raw.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    facts: list[Fact] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            continue
        date = str(item.get("valid_from") or "").strip()
        try:
            importance = float(item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        facts.append(
            Fact(
                subject=str(item.get("subject") or "user"),
                key=key,
                value=value,
                valid_from=date,
                importance=max(0.0, min(1.0, importance)),
            )
        )
    return facts


class MemoryV2:
    """三层记忆：事实（语义）+ 会话 chunk（情节）+ 检索。"""

    def __init__(self, recency_half_life_days: float = 180.0) -> None:
        self.chunks: list[Chunk] = []
        self.consolidator = Consolidator()
        self.half_life = recency_half_life_days
        self._bm25: BM25 | None = None
        self._reference_date: datetime | None = None
        self.last_chunks: list[Chunk] = []

    def ingest(self, record: dict[str, Any], facts: list[Fact] | None = None) -> None:
        self.chunks = chunk_record(record)
        self._bm25 = BM25([c.tokens for c in self.chunks])
        for fact in facts or []:
            self.consolidator.add(fact)
        self._reference_date = parse_date(record["question_date"])

    # ── 检索 ──
    def _recency(self, date_str: str) -> float:
        if not self._reference_date:
            return 0.5
        dt = parse_date(date_str)
        if not dt:
            return 0.5
        days = abs((self._reference_date - dt).days)
        return 0.5 ** (days / self.half_life)

    def retrieve(self, query: str, k_facts: int = 12, k_chunks: int = 4) -> dict[str, list[str]]:
        q_tokens = tokenize(query)

        scored_facts: list[tuple[float, Fact]] = []
        for fact in self.consolidator.active():
            overlap = len(set(q_tokens) & set(tokenize(fact.value + " " + fact.key)))
            bm = overlap / (len(set(q_tokens)) or 1)
            score = 0.6 * bm + 0.3 * self._recency(fact.valid_from) + 0.1 * fact.importance
            scored_facts.append((score, fact))
        scored_facts.sort(key=lambda x: x[0], reverse=True)

        # 命中 chunk 后回退到父会话（同一 session_id 前缀），避免只给模型看半句话
        scored_chunks: list[tuple[float, Chunk]] = []
        if self._bm25:
            for i, chunk in enumerate(self.chunks):
                bm = self._bm25.score(q_tokens, i)
                norm = bm / (1 + bm) if bm > 0 else 0.0
                score = 0.6 * norm + 0.3 * self._recency(chunk.date) + 0.1 * 0.5
                scored_chunks.append((score, chunk))
            scored_chunks.sort(key=lambda x: x[0], reverse=True)

        picks = scored_chunks[:k_chunks]
        self.last_chunks = [c for _, c in picks]
        expanded: list[Chunk] = []
        seen: set[str] = set()
        for _, chunk in picks:
            parent_id = chunk.session_id.split("#", 1)[0]
            if parent_id in seen:
                continue
            seen.add(parent_id)
            siblings = [c for c in self.chunks if c.session_id.split("#", 1)[0] == parent_id]
            # 命中的片段必须排在最前：父会话可能超长，按时间顺序截断会把它切掉
            ordered = [chunk] + [c for c in siblings if c is not chunk]
            merged = ""
            for c in ordered:
                if len(merged) + len(c.text) + 1 > 6000:
                    if not merged:
                        merged = c.text[:6000]
                    break
                merged += ("\n" if merged else "") + c.text
            expanded.append(Chunk(session_id=parent_id, date=chunk.date, text=merged[:6000]))

        return {
            "facts": [f.render() for _, f in scored_facts[:k_facts]],
            "chunks": [f"### {c.date} (session {c.session_id})\n{c.text}" for c in expanded],
        }

    def render_timeline(self, max_chars: int = 2000) -> str:
        """生成"会话时间线骨架"：日期 + 每个会话首条用户消息的摘要。

        纯本地计算，不调用模型；用于补 temporal-reasoning 需要的时间锚点。
        """
        if not self.chunks:
            return ""
        order: list[str] = []
        first_text: dict[str, str] = {}
        dates: dict[str, str] = {}
        for chunk in self.chunks:
            parent = chunk.session_id.split("#", 1)[0]
            if parent not in first_text:
                order.append(parent)
                dates[parent] = chunk.date
                first_text[parent] = chunk.text.replace("\n", " ")[:120]

        lines = []
        used = len("## Timeline of past sessions\n")
        for parent in order:
            line = f"- {dates[parent]} ({parent}): {first_text[parent]}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "## Timeline of past sessions\n" + "\n".join(lines) if lines else ""

    def pick_sessions(self, max_sessions: int = 6) -> list[tuple[str, str]]:
        """按检索得分顺序返回父会话 (date, text)，供"局部事实抽取"使用。"""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for chunk in self.last_chunks:
            parent = chunk.session_id.split("#", 1)[0]
            if parent in seen:
                continue
            seen.add(parent)
            text = "\n".join(c.text for c in self.chunks if c.session_id.split("#", 1)[0] == parent)
            out.append((chunk.date, text[:4000]))
            if len(out) >= max_sessions:
                break
        return out

    def render_recent(self, n: int = 2, max_chars: int = 3000) -> str:
        """强制注入最近 n 个会话：长历史里问题常指向近期事件，纯相似度会漏掉。"""
        if not self.chunks:
            return ""
        recent_ids: list[str] = []
        for chunk in self.chunks:
            parent = chunk.session_id.split("#", 1)[0]
            if parent not in recent_ids:
                recent_ids.append(parent)
        tail = recent_ids[-n:]
        blocks = []
        for parent in tail:
            text = "\n".join(c.text for c in self.chunks if c.session_id.split("#", 1)[0] == parent)
            blocks.append(f"### {parent}\n{text[:max_chars]}")
        return "## Most recent sessions\n" + "\n\n".join(blocks) if blocks else ""

    def render_context(self, retrieved: dict[str, list[str]], max_chars: int = 7000) -> str:
        """按字符预算打包：事实优先（紧凑），再用高分 chunk 填充，超出预算即截断。"""
        budget = max_chars
        parts: list[str] = []

        fact_lines = retrieved.get("facts") or []
        if fact_lines:
            header = "## Remembered facts (newest wins)\n"
            body = ""
            for line in fact_lines:
                if len(header) + len(body) + len(line) + 1 > budget * 0.6:
                    break
                body += line + "\n"
            if body:
                parts.append(header + body.rstrip())
                budget -= len(header) + len(body)

        chunk_texts = retrieved.get("chunks") or []
        if chunk_texts and budget > 500:
            header = "## Relevant past sessions\n"
            body = ""
            for chunk in chunk_texts:
                # 单个 chunk 超预算时截断（保留头部，通常含会话日期与关键句）
                remain = budget - len(header) - len(body) - 2
                if remain <= 300:
                    break
                piece = chunk if len(chunk) <= remain else chunk[:remain] + "\n…(truncated)"
                body += piece + "\n\n"
            if body:
                parts.append(header + body.rstrip())

        return "\n\n".join(parts) if parts else "(no relevant memory found)"
