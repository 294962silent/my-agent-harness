"""Context 压缩边界稳定性测试（缺陷 1）。

压缩缓存不再用「消息列表裸下标」记录边界，而是用稳定消息 key（tool_call_id / 内容指纹）
定位。历史清洗（增删断头工具结果、剔除空失败轮次）后，下一次 prepare 的视图必须
既不重复也不丢失消息；边界被整体删除时必须安全重建而不是猜下标。
"""

from __future__ import annotations

import pytest  # pyright: ignore[reportMissingImports]
from my_agent_llm import Message, Response  # pyright: ignore[reportMissingImports]

from my_agent_core.context import ContextManager
from my_agent_core.tool_history import clean_provider_context

SUMMARY_TEXT = "## Goal\nstabilize compaction boundary"


class FakeLLM:
    """摘要替身：始终返回固定摘要，记录调用次数。"""

    def __init__(self) -> None:
        self.default = Response(content=SUMMARY_TEXT, model="fake")
        self.calls: list[list[Message]] = []

    async def achat(self, *, messages, tools=None, **_kwargs) -> Response:
        self.calls.append(list(messages))
        return self.default


def _msg(role: str, content: str, **metadata) -> Message:
    return Message(role=role, content=content, metadata=metadata or None)


def _history(turns: int = 12) -> list[Message]:
    """system + 12 轮 user/assistant（每条约 320 字符），确保超过小预算阈值。"""
    messages = [_msg("system", "you are a coding agent")]
    for i in range(turns):
        messages.append(_msg("user", f"user-{i} " + "u" * 300))
        messages.append(_msg("assistant", f"assistant-{i} " + "a" * 300))
    return messages


def _ctx(llm: FakeLLM, *, budget: int = 2000, keep_recent_tokens: int = 150) -> ContextManager:
    return ContextManager(budget=budget, llm=llm, keep_recent_tokens=keep_recent_tokens)


def _contents(messages: list[Message]) -> list[str]:
    return [str(m.content) for m in messages]


def _assert_tail_once(contents: list[str], ctx: ContextManager) -> None:
    """快照尾部的每条消息在视图中恰好出现一次（不重复、不丢失）。"""
    assert ctx._retained_tail is not None
    for snapshot in ctx._retained_tail:
        assert contents.count(str(snapshot["content"])) == 1


@pytest.mark.anyio
async def test_synthetic_tool_insertion_before_boundary_not_duplicated() -> None:
    """压缩后在「已覆盖前缀」插入断头修复的合成 tool 消息（列表变长）→ 视图不重复、不丢失。

    裸下标实现会把尾部快照再拼一次（重复最后 N 条）；key 定位必须还原出正确新增段。
    """
    llm = FakeLLM()
    ctx = _ctx(llm)
    history = _history()
    view1 = await ctx.prepare(history)
    assert len(llm.calls) == 1
    assert ctx._retained_tail
    assert ctx._covered_count is not None
    tail_len = len(ctx._retained_tail)

    # 压缩后继续追加一轮新对话
    history.append(_msg("user", "new-question"))
    history.append(_msg("assistant", "new-answer"))

    # 在被摘要覆盖的前缀里制造断头工具调用：clean 会就地补一条合成 tool 结果（列表变长）
    history.insert(2, _msg("assistant", "", tool_calls=[{"id": "call-x", "name": "bash", "args": {}}]))
    cleaned = clean_provider_context(history)
    assert len(cleaned) == len(history) + 1  # 合成结果确实被插入

    view2 = await ctx.prepare(cleaned)
    assert len(llm.calls) == 1  # 缓存命中，未重新摘要
    contents = _contents(view2)
    assert contents.count("new-question") == 1
    assert contents.count("new-answer") == 1
    _assert_tail_once(contents, ctx)
    # system + 摘要 + 尾部快照 + 2 条新增
    assert len(view2) == 1 + 1 + tail_len + 2
    # 压缩建立的静态前缀（system + 摘要 + 尾部）字节不变
    assert view2[: len(view1)] == view1


@pytest.mark.anyio
async def test_synthetic_tool_insertion_inside_tail_rebuilds_cleanly() -> None:
    """合成 tool 消息插在尾部快照中间（列表变长）→ 用边界 key 锚定重建，不重复/不丢失。"""
    llm = FakeLLM()
    ctx = _ctx(llm)
    history = _history()
    await ctx.prepare(history)
    assert ctx._retained_tail
    covered = ctx._covered_count
    assert covered is not None

    history.append(_msg("user", "tail-question"))
    history.append(_msg("assistant", "tail-answer"))
    # 边界（第一条保留尾部消息）之后、尾部其余消息之前，插入一个断头 assistant
    broken = _msg("assistant", "", tool_calls=[{"id": "call-tail", "name": "edit", "args": {}}])
    history.insert(covered + 1, broken)
    cleaned = clean_provider_context(history)
    assert any(
        m.role == "tool" and (m.metadata or {}).get("tool_call_id") == "call-tail" for m in cleaned
    )

    view = await ctx.prepare(cleaned)
    assert len(llm.calls) == 1
    contents = _contents(view)
    assert contents.count("tail-question") == 1
    assert contents.count("tail-answer") == 1
    # 尾部快照中仍然存在（未被改写）的消息各出现一次；被插入的断头 assistant 也可继续可见
    _assert_tail_once(contents, ctx)
    assert contents.count("") == 1  # 断头 assistant 本体出现一次，合成 tool 结果紧跟其后


@pytest.mark.anyio
async def test_empty_assistant_removal_before_boundary_not_lost() -> None:
    """压缩后清洗剔除前缀里的空失败 assistant（列表变短）→ 新增消息不丢失、尾部不重复。"""
    llm = FakeLLM()
    ctx = _ctx(llm)
    history = [_msg("system", "you are a coding agent")]
    history.append(_msg("user", "u0 " + "u" * 300))
    history.append(_msg("assistant", "", stop_reason="error"))  # 空失败轮次：清洗时被剔除
    for i in range(1, 12):
        history.append(_msg("user", f"u{i} " + "u" * 300))
        history.append(_msg("assistant", f"a{i} " + "a" * 300))

    await ctx.prepare(history)
    assert ctx._retained_tail
    tail_len = len(ctx._retained_tail)

    history.append(_msg("user", "after-compact-question"))
    history.append(_msg("assistant", "after-compact-answer"))
    cleaned = clean_provider_context(history)
    assert len(cleaned) == len(history) - 1  # 空失败轮次确实被剔除

    view = await ctx.prepare(cleaned)
    assert len(llm.calls) == 1
    contents = _contents(view)
    assert contents.count("after-compact-question") == 1
    assert contents.count("after-compact-answer") == 1
    _assert_tail_once(contents, ctx)
    assert len(view) == 1 + 1 + tail_len + 2


@pytest.mark.anyio
async def test_deleted_boundary_falls_back_to_safe_rebuild() -> None:
    """边界消息被整体删除 → 放弃缓存安全重建（不崩溃、不重复、不丢当前消息）。"""
    llm = FakeLLM()
    ctx = _ctx(llm, budget=100_000, keep_recent_tokens=150)
    history = _history()
    await ctx.force_compact(history)  # 强制建立缓存（绕过阈值门控）
    assert ctx._summary is not None
    assert ctx._retained_tail
    boundary_content = str(ctx._retained_tail[0]["content"])

    surviving = [m for m in history if str(m.content) != boundary_content]
    assert len(surviving) == len(history) - 1

    view = await ctx.prepare(surviving)
    assert len(llm.calls) == 1  # 重建走无缓存路径；低于阈值时不再烧摘要
    assert ctx._summary is None
    assert ctx._retained_tail is None
    assert view == surviving  # 原样返回当前列表：不重复、不丢失
    assert _contents(view).count(boundary_content) == 0


@pytest.mark.anyio
async def test_empty_tail_compaction_anchors_on_last_covered_message() -> None:
    """空尾强制压缩（全部消息被摘要）用最后一条已覆盖消息的 key 锚定，后续新增不重复。"""
    llm = FakeLLM()
    ctx = _ctx(llm, budget=100_000, keep_recent_tokens=20_000)  # 尾部远未达预算 → cut=None
    history = [_msg("user", "short-question"), _msg("assistant", "short-answer")]

    await ctx.force_compact(history)  # 强制摘要全部非 system 消息，不保留尾部
    assert ctx._retained_tail == []
    assert ctx._end_key is not None

    history.append(_msg("user", "follow-up-question"))
    view = await ctx.prepare(history)
    assert len(llm.calls) == 1
    assert _contents(view) == [
        f"[Context summary — earlier conversation compacted]\n\n{SUMMARY_TEXT}",
        "follow-up-question",
    ]


@pytest.mark.anyio
async def test_tail_mismatch_anchors_on_boundary_key() -> None:
    """快照尾部与当前列表对不上（尾部中间消息被删）→ 用边界 key 锚定重建，视图正确且幂等。"""
    llm = FakeLLM()
    ctx = _ctx(llm)
    history = _history()
    await ctx.prepare(history)
    assert ctx._retained_tail is not None and len(ctx._retained_tail) >= 2
    boundary_content = str(ctx._retained_tail[0]["content"])
    removed_content = str(ctx._retained_tail[1]["content"])

    mutated = [m for m in history if str(m.content) != removed_content]
    view = await ctx.prepare(mutated)
    assert len(llm.calls) == 1
    contents = _contents(view)
    assert SUMMARY_TEXT in contents[1]  # 摘要被保留
    assert contents.count(boundary_content) == 1
    assert removed_content not in contents
    assert len(view) == len(mutated) - ctx._covered_count + 2  # system + 摘要 + 当前尾部

    repeated = await ctx.prepare(mutated)
    assert repeated == view  # 连续 prepare 稳定（幂等）


@pytest.mark.anyio
async def test_repeated_prepare_is_idempotent_and_prefix_stable() -> None:
    """连续多轮 prepare 结果稳定；追加新消息后静态前缀字节不变且只追加一次。"""
    llm = FakeLLM()
    ctx = _ctx(llm)
    history = _history()

    view1 = await ctx.prepare(history)
    view2 = await ctx.prepare(history)
    view3 = await ctx.prepare(history)
    assert view1 == view2 == view3
    assert len(llm.calls) == 1

    history.append(_msg("user", "next-question"))
    history.append(_msg("assistant", "next-answer"))
    view4 = await ctx.prepare(history)
    assert len(llm.calls) == 1
    assert view4[: len(view1)] == view1  # 静态前缀不变（KV-Cache 友好）
    assert _contents(view4[-2:]) == ["next-question", "next-answer"]
    assert _contents(view4).count("next-question") == 1

    view5 = await ctx.prepare(history)
    assert view5 == view4
