"""记忆索引：把 MEMORY.md / USER.md 的自由文本条目与结构化事实统一检索。

设计取舍：
- **系统提示仍注入冻结快照**（`<MEMORY_CONTEXT>`），保证前缀稳定、KV-Cache 友好；
- **每轮额外按用户输入检索一小块**（`<RETRIEVED_MEMORY>`）注入用户消息，只带相关的
  事实与条目，避免把整份记忆重复塞进每一轮。

事实带时序有效性：同 `(subject, key)` 的新值把旧值的 `valid_to` 置为新值生效日期，
检索默认只返回仍然有效的事实。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "to", "of", "and", "or", "in", "on", "for",
        "with", "at", "by", "it", "this", "that", "i", "you", "my", "me", "we", "do", "does", "did",
        "what", "when", "which", "who", "how", "many", "much", "please", "can", "could", "would",
    }
)


def tokenize(text: str) -> list[str]:
    """轻量分词：英文按词、中文按字，去停用词。"""
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS]


def _today() -> str:
    return date.today().isoformat()


@dataclass
class Fact:
    """一条原子事实，带时序有效性。"""

    subject: str
    key: str
    value: str
    valid_from: str = field(default_factory=_today)
    valid_to: str | None = None
    importance: float = 0.6
    source: str | None = None

    @property
    def active(self) -> bool:
        return self.valid_to is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "key": self.key,
            "value": self.value,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "importance": self.importance,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fact":
        try:
            importance = float(data.get("importance", 0.6))
        except (TypeError, ValueError):
            importance = 0.6
        return cls(
            subject=str(data.get("subject") or "user"),
            key=str(data.get("key") or ""),
            value=str(data.get("value") or ""),
            valid_from=str(data.get("valid_from") or ""),
            valid_to=(str(data["valid_to"]) if data.get("valid_to") else None),
            importance=max(0.0, min(1.0, importance)),
            source=(str(data["source"]) if data.get("source") else None),
        )

    def render(self) -> str:
        span = self.valid_from or "?"
        if self.valid_to:
            return f"- [{span} → {self.valid_to}] {self.key}: {self.value} (superseded)"
        return f"- [{span}] {self.key}: {self.value}"

    def text(self) -> str:
        return f"{self.subject} {self.key} {self.value}"


class Consolidator:
    """维护 (subject, key) 的时序有效性：新值覆盖旧值。"""

    def __init__(self, facts: list[Fact] | None = None) -> None:
        self.facts: list[Fact] = list(facts or [])

    def add(self, fact: Fact) -> None:
        for old in self.facts:
            if (
                old.active
                and old.subject == fact.subject
                and old.key == fact.key
                and old.value.strip().lower() != fact.value.strip().lower()
            ):
                old.valid_to = fact.valid_from or _today()
        self.facts.append(fact)

    def active(self) -> list[Fact]:
        return [f for f in self.facts if f.active]


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
            freq = tf.get(term)
            if not freq:
                continue
            denom = freq + 1.5 * (0.25 + 0.75 * dl / (self.avgdl or 1))
            total += self.idf(term) * freq * 2.5 / denom
        return total


class MemoryIndex:
    """自由文本条目 + 结构化事实的统一检索视图。"""

    def __init__(self, entries: dict[str, list[str]] | None = None, facts: list[Fact] | None = None) -> None:
        self.entries = {k: list(v) for k, v in (entries or {}).items()}
        self.consolidator = Consolidator(facts)
        self._rebuild()

    def _rebuild(self) -> None:
        self._docs: list[tuple[str, str, float]] = []  # (kind, text, importance)
        for fact in self.consolidator.active():
            self._docs.append(("fact", fact.text(), fact.importance))
        for target, items in self.entries.items():
            for item in items:
                self._docs.append((f"entry:{target}", item, 0.5))
        self._bm25 = BM25([tokenize(text) for _, text, _ in self._docs])

    def add_fact(self, fact: Fact) -> None:
        self.consolidator.add(fact)
        self._rebuild()

    def set_entries(self, entries: dict[str, list[str]]) -> None:
        self.entries = {k: list(v) for k, v in entries.items()}
        self._rebuild()

    def retrieve(self, query: str, budget_chars: int = 1200) -> str | None:
        """按相关度取 top-k，拼成可注入的文本块；无内容时返回 None。"""
        q_tokens = tokenize(query)
        if not q_tokens or not self._docs:
            return None

        scored: list[tuple[float, str, str]] = []
        for i, (kind, text, importance) in enumerate(self._docs):
            bm = self._bm25.score(q_tokens, i)
            if bm <= 0:
                continue
            norm = bm / (1 + bm)
            scored.append((0.7 * norm + 0.3 * importance, kind, text))
        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)

        lines: list[str] = []
        used = 0
        for _, kind, text in scored:
            if len(lines) >= 8:
                break
            prefix = "- " if kind == "fact" else f"- ({kind.split(':')[-1]}) "
            line = prefix + text.strip().replace("\n", " ")
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1

        if not lines:
            return None
        return "<RETRIEVED_MEMORY>\n" + "\n".join(lines) + "\n</RETRIEVED_MEMORY>"


def parse_fact_line(line: str) -> Fact | None:
    """从 FACTS.jsonl 的一行解析 Fact（容错）。"""
    import json

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("key") or not data.get("value"):
        return None
    return Fact.from_dict(data)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
