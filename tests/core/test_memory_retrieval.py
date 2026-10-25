"""记忆检索与事实时序有效性的集成测试（主仓库 memory.py）。

覆盖：
- `remember` 写入事实，同 (subject, key) 的新值让旧值失效；
- 事实落盘后可跨实例恢复，失效状态一并保留；
- `retrieve` 只返回相关且仍然有效的内容，并按预算截断；
- Agent 在开启记忆时，把检索结果注入到**本轮用户消息**（而不是整份记忆）；
- `memory` 工具的 `remember` 动作可用。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]
from my_agent_llm import Response, StreamChunk  # pyright: ignore[reportMissingImports]

from my_agent_core.agent import Agent
from my_agent_core.memory import MemoryStore, make_memory_tool
from my_agent_core.session import Session


class CapturingLLM:
    """记录每次调用收到的消息，返回固定答复。"""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def achat(self, messages, tools=None, model=None):
        self.calls.append({"messages": messages, "tools": tools})
        return Response(content=self.reply)

    async def achat_stream(self, messages, tools=None, model=None):
        self.calls.append({"messages": messages, "tools": tools})
        yield StreamChunk(content=self.reply)


def _store(tmpdir: str) -> MemoryStore:
    store = MemoryStore(mem_dir=Path(tmpdir))
    store.load_from_disk()
    return store


def test_remember_supersedes_previous_value() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.remember("wifi_password", "1234", valid_from="2023-01-01")
        result = store.remember("wifi_password", "9876", valid_from="2023-06-01")

        assert "superseded" in result
        active = [f for f in store._facts if f.active]
        assert [f.value for f in active] == ["9876"]
        assert [f.value for f in store._facts] == ["1234", "9876"]  # 旧值保留可审计

        context = store.retrieve("what is my wifi password?")
        assert context is not None
        assert "9876" in context
        assert "1234" not in context, "被取代的事实不应注入"


def test_facts_survive_reload() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.remember("port", "8080", valid_from="2023-01-01")
        store.remember("port", "9000", valid_from="2023-02-01")

        reloaded = MemoryStore(mem_dir=Path(tmpdir))
        reloaded.load_from_disk()
        active = [f.value for f in reloaded._facts if f.active]
        assert active == ["9000"]
        assert reloaded.retrieve("which port should I use?") is not None


def test_retrieve_returns_none_for_irrelevant_query() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.remember("favourite_colour", "blue", valid_from="2023-01-01")
        assert store.retrieve("how do I configure nginx upstream?") is None


def test_retrieve_respects_budget() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        for i in range(10):
            store.remember(f"key_{i}", f"value number {i} about deployment", valid_from="2023-01-01")
        small = store.retrieve("deployment value", budget_chars=60)
        assert small is not None
        assert len(small) < 400, "预算应生效，不能把全部事实都塞进去"


def test_entries_and_facts_are_both_searchable() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        mem_dir = Path(tmpdir)
        (mem_dir / "MEMORY.md").write_text("Project uses pytest for tests", encoding="utf-8-sig")
        store = _store(tmpdir)
        store.remember("test_framework", "pytest", valid_from="2023-01-01")

        context = store.retrieve("which test framework does the project use?")
        assert context is not None
        assert "pytest" in context


@pytest.mark.anyio
async def test_agent_injects_retrieved_memory_into_user_message() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        mem_dir = workspace / ".my_agent_core" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        store = MemoryStore(mem_dir=mem_dir)
        store.load_from_disk()
        store.remember("wifi_password", "9876", valid_from="2023-06-01")

        llm = CapturingLLM("9876")
        agent = Agent(llm=llm, tools=[], session=Session(path=workspace / "s.jsonl"), memory_dir=mem_dir)
        await agent.run("what is my wifi password?")

        user_msgs = [m for m in llm.calls[0]["messages"] if m.role == "user"]
        assert user_msgs, "至少要有一条用户消息"
        assert "<RETRIEVED_MEMORY>" in user_msgs[-1].content
        assert "9876" in user_msgs[-1].content


@pytest.mark.anyio
async def test_memory_tool_remember_action() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        tool = make_memory_tool(store)
        result = await tool.execute({"action": "remember", "target": "user", "key": "city", "value": "Nanjing"})
        assert result.ok, result.error
        assert "Nanjing" in str(result.data)
        assert [f.value for f in store._facts if f.active] == ["Nanjing"]
