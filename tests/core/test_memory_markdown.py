"""记忆存储与检索的进阶测试：Markdown 真相源、FTS5 后端、会话结束蒸馏。

对应三条设计：
1. 事实落成 `facts/<subject>/<key>.md`（带 frontmatter），可直接 grep / git diff；
2. 检索走 SQLite FTS5（不可用时回退 BM25）；
3. 会话结束时自动蒸馏出稳定事实，且抽取失败不影响主流程。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]
from my_agent_llm import Response, StreamChunk  # pyright: ignore[reportMissingImports]

from my_agent_core.agent import Agent
from my_agent_core.memory import MemoryStore
from my_agent_core.memory_index import MemoryIndex, Fact
from my_agent_core.session import Session


def _store(tmpdir: str) -> MemoryStore:
    store = MemoryStore(mem_dir=Path(tmpdir))
    store.load_from_disk()
    return store


def test_facts_are_markdown_files_with_frontmatter() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.remember("wifi_password", "9876", valid_from="2023-06-01", importance=0.8)

        path = Path(tmpdir) / "facts" / "user" / "wifi_password.md"
        assert path.exists(), "事实应落成 Markdown 文件"
        text = path.read_text(encoding="utf-8")

        assert text.startswith("---")
        assert 'key: "wifi_password"' in text
        assert 'value: "9876"' in text
        assert 'valid_from: "2023-06-01"' in text
        assert "valid_to: null" in text
        assert text.rstrip().endswith("9876"), "正文应保留人类可读的值"


def test_supersede_records_history_in_markdown() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.remember("wifi_password", "1234", valid_from="2023-01-01")
        store.remember("wifi_password", "9876", valid_from="2023-06-01")

        text = (Path(tmpdir) / "facts" / "user" / "wifi_password.md").read_text(encoding="utf-8")
        assert 'value: "9876"' in text
        assert "history:" in text
        assert 'value: "1234"' in text
        assert 'valid_to: "2023-06-01"' in text


def test_legacy_jsonl_is_migrated_to_markdown() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        legacy = Path(tmpdir) / "FACTS.jsonl"
        legacy.write_text(
            json.dumps(
                {"subject": "user", "key": "port", "value": "8080", "valid_from": "2023-01-01", "importance": 0.7}
            )
            + "\n",
            encoding="utf-8",
        )

        store = MemoryStore(mem_dir=Path(tmpdir))
        store.load_from_disk()

        assert (Path(tmpdir) / "facts" / "user" / "port.md").exists()
        assert not legacy.exists(), "迁移后旧文件应重命名，避免重复导入"
        assert any(f.value == "8080" for f in store._facts if f.active)


def test_fts5_backend_is_used_when_available() -> None:
    import sqlite3

    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    except sqlite3.Error:
        pytest.skip("该 Python 构建未启用 FTS5")

    index = MemoryIndex(
        entries={"memory": ["project uses pytest for tests"]},
        facts=[Fact(subject="user", key="editor", value="neovim", valid_from="2023-01-01")],
    )
    assert index.backend == "fts5"
    assert "neovim" in (index.retrieve("which editor do I use") or "")


class DistillLLM:
    """返回固定 JSON 的假模型，用于验证蒸馏链路。"""

    def __init__(self, payload: str, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def achat(self, messages, tools=None, model=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return Response(content=self.payload, model="test")

    async def achat_stream(self, messages, tools=None, model=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        yield StreamChunk(content=self.payload)


@pytest.mark.anyio
async def test_autodistill_writes_facts_on_session_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        mem_dir = workspace / ".my_agent_core" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        payload = '```json\n[{"key": "city", "value": "Nanjing"}, {"key": "editor", "value": "neovim"}]\n```'
        llm = DistillLLM(payload)
        agent = Agent(
            llm=llm,
            tools=[],
            session=Session(path=workspace / "s.jsonl"),
            memory_dir=mem_dir,
            memory_autodistill=True,
        )
        # 主对话用 stream，蒸馏用 achat
        agent.llm.achat_stream = DistillLLM("ok").achat_stream  # type: ignore[method-assign]
        for question in ("hello", "my city is Nanjing", "I use neovim", "thanks"):
            await agent.run(question)

        keys = {f.key: f.value for f in agent.memory_store._facts if f.active}
        assert keys.get("city") == "Nanjing"
        assert keys.get("editor") == "neovim"
        assert (mem_dir / "facts" / "user" / "city.md").exists()


@pytest.mark.anyio
async def test_autodistill_failure_is_silent() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        mem_dir = workspace / ".my_agent_core" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        llm = DistillLLM("", fail=True)
        agent = Agent(
            llm=llm,
            tools=[],
            session=Session(path=workspace / "s.jsonl"),
            memory_dir=mem_dir,
            memory_autodistill=True,
        )
        agent.llm.achat_stream = DistillLLM("ok").achat_stream  # type: ignore[method-assign]
        for question in ("one", "two", "three", "four"):
            await agent.run(question)  # 不应抛异常

        assert not agent.memory_store._facts, "抽取失败时不应写入任何事实"
