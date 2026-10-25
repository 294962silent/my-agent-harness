"""记忆索引：Markdown 事实存储 + SQLite FTS5 检索 + 时序有效性。

三条设计（对齐 agent-memory 的思路，实现独立）：

1. **Markdown 是真相源**：每条事实一个文件 `facts/<subject>/<key>.md`，带 YAML frontmatter，
   可直接 grep / git diff / 手工编辑；索引只是可重建的缓存。
2. **SQLite FTS5 检索**：比手写 BM25 更强（短语、前缀、排序），标准库自带；FTS5 不可用时
   自动回退到内置 BM25，行为保持一致。
3. **时序有效性**：同 `(subject, key)` 的新值把旧值压进 `history`，检索只返回当前有效值。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")
_KEY_SAFE_RE = re.compile(r"[^a-z0-9_\-]+")

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


def slugify_key(key: str) -> str:
    """把 key 规范化成安全的文件名（小写、仅 [a-z0-9_-]）。"""
    slug = _KEY_SAFE_RE.sub("-", (key or "").strip().lower()).strip("-")
    return slug or "untitled"


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
    history: list[dict[str, Any]] = field(default_factory=list)

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
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fact":
        try:
            importance = float(data.get("importance", 0.6))
        except (TypeError, ValueError):
            importance = 0.6
        history = data.get("history")
        return cls(
            subject=str(data.get("subject") or "user"),
            key=str(data.get("key") or ""),
            value=str(data.get("value") or ""),
            valid_from=str(data.get("valid_from") or ""),
            valid_to=(str(data["valid_to"]) if data.get("valid_to") else None),
            importance=max(0.0, min(1.0, importance)),
            source=(str(data["source"]) if data.get("source") else None),
            history=[h for h in history if isinstance(h, dict)] if isinstance(history, list) else [],
        )

    def render(self) -> str:
        span = self.valid_from or "?"
        if self.valid_to:
            return f"- [{span} → {self.valid_to}] {self.key}: {self.value} (superseded)"
        return f"- [{span}] {self.key}: {self.value}"

    def text(self) -> str:
        return f"{self.subject} {self.key} {self.value}"


class Consolidator:
    """维护 (subject, key) 的时序有效性：新值把旧值推进 history。"""

    def __init__(self, facts: list[Fact] | None = None) -> None:
        self.facts: list[Fact] = []
        for fact in facts or []:
            self.add(fact, replay=True)

    def add(self, fact: Fact, replay: bool = False) -> None:
        for old in self.facts:
            if (
                old.active
                and old.subject == fact.subject
                and old.key == fact.key
                and old.value.strip().lower() != fact.value.strip().lower()
            ):
                cutoff = fact.valid_from or _today()
                old.valid_to = cutoff
                fact.history.insert(
                    0,
                    {
                        "value": old.value,
                        "valid_from": old.valid_from,
                        "valid_to": cutoff,
                        "source": old.source,
                    },
                )
                fact.history.extend(old.history)
        self.facts.append(fact)

    def active(self) -> list[Fact]:
        return [f for f in self.facts if f.active]


class BM25:
    """内置 BM25（k1=1.5, b=0.75），FTS5 不可用时的回退实现。"""

    def __init__(self, docs: list[list[str]]) -> None:
        import math

        self.docs = docs
        self.n = len(docs)
        self.avgdl = sum(len(d) for d in docs) / self.n if self.n else 0.0
        self.df: dict[str, int] = {}
        for doc in docs:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1
        self._log = math.log

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return self._log(1 + (self.n - df + 0.5) / (df + 0.5))

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


class FtsIndex:
    """SQLite FTS5 内存索引；不可用时标记 supported=False，调用方回退 BM25。"""

    def __init__(self, docs: list[tuple[str, str, float]]) -> None:
        self.supported = False
        self.docs = docs
        self._conn: sqlite3.Connection | None = None
        if not docs:
            return
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body, kind UNINDEXED)")
            conn.executemany(
                "INSERT INTO docs(body, kind) VALUES (?, ?)",
                [(text, kind) for kind, text, _ in docs],
            )
            conn.commit()
            self._conn = conn
            self.supported = True
        except sqlite3.Error:
            self.supported = False

    def search(self, query: str, limit: int = 32) -> list[int]:
        """返回命中的 doc 下标（按 FTS5 相关性降序）。"""
        if not self.supported or self._conn is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        # 用 OR 连接，避免任一 token 缺失就零命中
        match_expr = " OR ".join(f'"{t}"' for t in tokens)
        try:
            rows = self._conn.execute(
                "SELECT rowid FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
                (match_expr, limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [int(r[0]) - 1 for r in rows if 0 <= int(r[0]) - 1 < len(self.docs)]


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
        self._fts = FtsIndex(self._docs)

    @property
    def backend(self) -> str:
        """当前检索后端：fts5 或 bm25。"""
        return "fts5" if self._fts.supported else "bm25"

    def add_fact(self, fact: Fact) -> None:
        self.consolidator.add(fact)
        self._rebuild()

    def set_entries(self, entries: dict[str, list[str]]) -> None:
        self.entries = {k: list(v) for k, v in entries.items()}
        self._rebuild()

    def _candidates(self, query: str) -> list[int]:
        """候选下标：FTS5 命中优先；FTS5 不可用时退化为全量（由 BM25 打分）。"""
        if self._fts.supported:
            return self._fts.search(query)
        return list(range(len(self._docs)))

    def retrieve(self, query: str, budget_chars: int = 1200) -> str | None:
        """按相关度取 top-k，拼成可注入的文本块；无内容时返回 None。"""
        q_tokens = tokenize(query)
        if not q_tokens or not self._docs:
            return None

        scored: list[tuple[float, str, str]] = []
        for i in self._candidates(query):
            if i < 0 or i >= len(self._docs):
                continue
            kind, text, importance = self._docs[i]
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


# ── Markdown 事实存储（真相源） ────────────────────────────────────────────────


def _yaml_scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def fact_to_markdown(fact: Fact) -> str:
    """把事实渲染成带 frontmatter 的 Markdown。"""
    lines = [
        "---",
        f"key: {_yaml_scalar(fact.key)}",
        f"subject: {_yaml_scalar(fact.subject)}",
        f"value: {_yaml_scalar(fact.value)}",
        f"valid_from: {_yaml_scalar(fact.valid_from)}",
        f"valid_to: {_yaml_scalar(fact.valid_to) if fact.valid_to else 'null'}",
        f"importance: {fact.importance}",
        f"source: {_yaml_scalar(fact.source) if fact.source else 'null'}",
    ]
    if fact.history:
        lines.append("history:")
        for item in fact.history:
            lines.append(f"  - value: {_yaml_scalar(item.get('value'))}")
            lines.append(f"    valid_from: {_yaml_scalar(item.get('valid_from'))}")
            lines.append(f"    valid_to: {_yaml_scalar(item.get('valid_to'))}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {fact.key}")
    lines.append("")
    lines.append(fact.value)
    return "\n".join(lines) + "\n"


def markdown_to_fact(text: str) -> Fact | None:
    """解析 frontmatter；格式不对返回 None（不抛异常，避免一条坏文件拖垮记忆）。"""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    header = parts[1]
    data: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in header.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            current = {}
            history.append(current)
            key, _, val = stripped[2:].partition(":")
            current[key.strip()] = _parse_scalar(val)
            continue
        if line.startswith("    ") and current is not None and ":" in line:
            key, _, val = stripped.partition(":")
            current[key.strip()] = _parse_scalar(val)
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "history":
                continue
            if val == "null":
                data[key] = None
            elif key == "importance":
                try:
                    data[key] = float(val)
                except ValueError:
                    data[key] = 0.6
            else:
                data[key] = _parse_scalar(val)
    if not data.get("key") or data.get("value") in (None, ""):
        return None
    return Fact.from_dict({**data, "history": history})


def load_facts_from_dir(root: Path) -> list[Fact]:
    """从 `facts/<subject>/<key>.md` 读取全部事实（一条坏文件不影响其余）。"""
    if not root.exists():
        return []
    facts: list[Fact] = []
    for path in sorted(root.rglob("*.md")):
        try:
            fact = markdown_to_fact(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if fact is not None:
            facts.append(fact)
    return facts


def fact_path(root: Path, fact: Fact) -> Path:
    return root / slugify_key(fact.subject) / f"{slugify_key(fact.key)}.md"


def save_fact(root: Path, fact: Fact) -> Path:
    """写单条事实（幂等：内容相同则不写，避免污染 mtime / git diff）。"""
    path = fact_path(root, fact)
    text = fact_to_markdown(fact)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse_fact_line(line: str) -> Fact | None:
    """兼容旧的 FACTS.jsonl 行格式（迁移用）。"""
    import json

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("key") or not data.get("value"):
        return None
    return Fact.from_dict(data)
