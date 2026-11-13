"""Layered local projections preserve canonical history and budget boundaries."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from my_agent_core.artifacts import ArtifactStore
from my_agent_core.context import ContextManager, estimate_tokens
from my_agent_core.context_compaction import CompactionPolicy, ContextBudgetExceeded
from my_agent_llm import Message


class LLM:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def achat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("offline summarizer failure")
        return SimpleNamespace(
            content="<summary>Goals: test\nConstraints: preserve state\nDecisions: done\nFiles: none\nValidation: passed\nPending: next\nArtifacts: none</summary>",
            usage={},
            model="fake",
        )


def transcript():
    return [
        Message(role="system", content="system"),
        Message(role="user", content="task"),
        Message(role="assistant", content="", metadata={"tool_calls": [{"id": "a", "name": "shell", "arguments": {}}]}),
        Message(
            role="tool",
            content=json.dumps({"exit_code": 0, "stdout": "x" * 4000}),
            metadata={"tool_call_id": "a", "tool_name": "shell"},
        ),
        Message(role="assistant", content="done"),
        Message(role="user", content="next"),
    ]


def test_micro_is_readable_stable_and_keeps_canonical_and_tool_pair(tmp_path):
    messages = transcript()
    before = [m.model_dump() for m in messages]
    store = ArtifactStore(tmp_path)
    llm = LLM()
    ctx = ContextManager(
        budget=10000,
        llm=llm,
        artifact_store=store,
        compaction_policy=CompactionPolicy(mode="layered", recent_tool_groups=0),
    )
    first = asyncio.run(ctx.prepare(messages))
    second = asyncio.run(ctx.prepare(messages))
    assert [m.model_dump() for m in messages] == before
    assert [m.model_dump() for m in first] == [m.model_dump() for m in second]
    assert [m.role for m in first] == [m.role for m in messages]
    tool = first[3]
    assert len(tool.content) < 600 and tool.metadata["tool_call_id"] == "a"
    assert store.read_page(tool.metadata["artifact_id"])["text"] == messages[3].content
    assert not llm.calls and ctx.compaction_report["micro_groups"] == 1


def test_micro_keeps_recent_error_unclosed_and_save_failure(tmp_path, monkeypatch):
    messages = transcript()
    store = ArtifactStore(tmp_path)
    recent = ContextManager(
        budget=10000, llm=LLM(), artifact_store=store, compaction_policy=CompactionPolicy(mode="layered")
    )
    assert asyncio.run(recent.prepare(messages))[3].content == messages[3].content
    ctx = ContextManager(
        budget=10000,
        llm=LLM(),
        artifact_store=store,
        compaction_policy=CompactionPolicy(mode="layered", recent_tool_groups=0),
    )
    bad = transcript()
    bad[3].content = json.dumps({"exit_code": 1, "stderr": "x" * 4000})
    assert asyncio.run(ctx.prepare(bad))[3].content == bad[3].content
    assert asyncio.run(ctx.prepare(messages[:3])) == messages[:3]

    def fail(*a, **kw):
        raise OSError("offline disk failure")

    monkeypatch.setattr(store, "save", fail)
    assert asyncio.run(ctx.prepare(messages))[3].content == messages[3].content


def test_prefix_fold_uses_fingerprints_and_micro_does_not_break_cache(tmp_path):
    from my_agent_core.session import Session
    from my_agent_core.context import ContextSessionBridge

    session = Session(path=tmp_path / "session.jsonl")
    for m in transcript():
        session.add_message(m.role, m.content, **(m.metadata or {}))
    messages = session.get_current_path_messages()
    llm = LLM()
    store = ArtifactStore(tmp_path)
    policy = CompactionPolicy(mode="layered", recent_tool_groups=0)
    ctx = ContextManager(budget=10000, llm=llm, artifact_store=store, compaction_policy=policy)
    ctx.register_completed_prefix(messages, 5, "Goals: prior stage complete; Pending: next; Artifacts: none")
    view = asyncio.run(ctx.prepare(messages))
    assert len(view) < len(messages) and not llm.calls
    ContextSessionBridge(session).write_compaction(ctx)
    restored = ContextManager(budget=10000, llm=llm, artifact_store=store, compaction_policy=policy)
    ContextSessionBridge(Session.load(session.path)).restore_cache(restored)
    assert [m.content for m in asyncio.run(restored.prepare(messages))] == [m.content for m in view]
    changed = [m.model_copy(deep=True) for m in messages]
    changed[1].content = "other branch"
    rebuilt = asyncio.run(restored.prepare(changed))
    assert not any("prior stage complete" in str(m.content) for m in rebuilt)


def test_layered_fallback_never_returns_oversized_view_and_bounds_summary():
    llm = LLM(fail=True)
    messages = [Message(role="user", content="x" * 12000), Message(role="user", content="tail")]
    policy = CompactionPolicy(
        mode="layered", model_context_window=3000, output_reserve=500, safety_margin=100, summary_max_tokens=300
    )
    ctx = ContextManager(budget=9000, llm=llm, keep_recent_tokens=1, compaction_policy=policy)
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ctx.prepare(messages))
    assert all(c["max_tokens"] == 300 for c in llm.calls)
    assert ctx.budget_threshold <= 2400


def test_parallel_groups_reused_call_ids_and_large_save_failure(tmp_path, monkeypatch):
    messages = transcript()
    messages[2].metadata["tool_calls"].append({"id": "b", "name": "shell", "arguments": {}})
    messages.insert(
        4,
        Message(
            role="tool",
            content=json.dumps({"exit_code": 0, "stdout": "y" * 4000}),
            metadata={"tool_call_id": "b", "tool_name": "shell"},
        ),
    )
    store = ArtifactStore(tmp_path)
    ctx = ContextManager(
        budget=10000,
        llm=LLM(),
        artifact_store=store,
        compaction_policy=CompactionPolicy(mode="layered", recent_tool_groups=0),
    )
    view = asyncio.run(ctx.prepare(messages))
    assert view[3].metadata["artifact_id"] != view[4].metadata["artifact_id"]
    missing = messages[:4] + messages[5:]
    assert asyncio.run(ctx.prepare(missing))[3].content == missing[3].content
    changed = transcript()
    changed[3].content = json.dumps({"exit_code": 0, "stdout": "z" * 4000})
    assert asyncio.run(ctx.prepare(changed))[3].metadata["artifact_id"] != view[3].metadata["artifact_id"]
    huge = transcript()
    huge[3].content = json.dumps({"exit_code": 0, "stdout": "x" * 8000})

    def fail(*a, **kw):
        raise OSError("disk failure")

    monkeypatch.setattr(store, "save", fail)
    assert asyncio.run(ctx.prepare(huge))[3].content == huge[3].content


def test_manual_layered_compact_cannot_return_overflow():
    ctx = ContextManager(
        budget=1200,
        llm=LLM(fail=True),
        compaction_policy=CompactionPolicy(
            mode="layered", output_reserve=100, safety_margin=100, summary_max_tokens=100
        ),
    )
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ctx.force_compact([Message(role="user", content="x" * 10000)]))


def test_bounded_structured_summary_uses_same_injected_client():
    llm = LLM()
    ctx = ContextManager(
        budget=3000,
        llm=llm,
        keep_recent_tokens=1,
        compaction_policy=CompactionPolicy(
            mode="layered", model_context_window=10000, output_reserve=100, safety_margin=100, summary_max_tokens=200
        ),
    )
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="goal and constraints"),
        Message(role="assistant", content="old " * 2500),
        Message(role="user", content="current"),
    ]
    view = asyncio.run(ctx.prepare(messages))
    assert len(llm.calls) == 1 and llm.calls[0]["max_tokens"] == 200
    assert "Goals" in llm.calls[0]["messages"][0].content
    assert estimate_tokens(view) <= ctx.budget_threshold


def test_dynamic_micro_tail_survives_real_session_restore(tmp_path):
    from my_agent_core.context import ContextSessionBridge
    from my_agent_core.session import Session

    source = [
        Message(role="system", content="system"),
        Message(role="user", content="old stage"),
        Message(role="assistant", content="stage complete"),
        *transcript()[1:],
    ]
    session = Session(path=tmp_path / "session.jsonl")
    for m in source:
        session.add_message(m.role, m.content, **(m.metadata or {}))
    canonical = session.get_current_path_messages()
    policy = CompactionPolicy(mode="layered", recent_tool_groups=0)
    store = ArtifactStore(tmp_path)
    ctx = ContextManager(budget=10000, llm=LLM(), artifact_store=store, compaction_policy=policy)
    ctx.register_completed_prefix(canonical, 3, "Goals: old stage complete; Pending: current task")
    view = asyncio.run(ctx.prepare(canonical))
    assert any((m.metadata or {}).get("micro_projection") for m in view)
    assert any(len(str(m["content"])) > 3000 for m in ctx.pending_compaction.retained_tail)
    ContextSessionBridge(session).write_compaction(ctx)
    again = ContextManager(budget=10000, llm=LLM(), artifact_store=store, compaction_policy=policy)
    ContextSessionBridge(Session.load(session.path)).restore_cache(again)
    assert [m.model_dump() for m in asyncio.run(again.prepare(canonical))] == [m.model_dump() for m in view]
    assert again.pending_compaction is None


def test_stage_fold_rejects_incomplete_group_and_oversized_summary(tmp_path):
    ctx = ContextManager(
        budget=10000,
        llm=LLM(),
        artifact_store=ArtifactStore(tmp_path),
        compaction_policy=CompactionPolicy(mode="layered"),
    )
    messages = transcript()
    with pytest.raises(ValueError):
        ctx.register_completed_prefix(messages, 3, "not complete")
    ctx.register_completed_prefix(messages, 5, "x" * 80000)
    view = asyncio.run(ctx.prepare(messages))
    assert len(view) == len(messages) and ctx.pending_compaction is None and not ctx.llm.calls


def test_explicit_stage_fold_preserves_initial_user_constraints():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="Goal: edit parser. Constraint: never delete user files."),
        Message(role="assistant", content="attempt evaluated"),
        Message(role="user", content="next attempt"),
    ]
    ctx = ContextManager(budget=10000, llm=LLM(), compaction_policy=CompactionPolicy(mode="layered"))
    ctx.register_completed_prefix(messages, 3, "Attempt finished; outcome failed; continue investigation.")
    view = asyncio.run(ctx.prepare(messages))
    assert messages[1].content in view[1].content
    assert view[-1].content == "next attempt" and ctx.compaction_report["phase_fold"] is True


@pytest.mark.parametrize("manual", [False, True])
def test_failed_recompaction_preserves_only_valid_previous_cache(tmp_path, manual):
    policy = CompactionPolicy(
        mode="layered", model_context_window=4000, output_reserve=200, safety_margin=100, summary_max_tokens=200
    )
    llm = LLM(fail=True)
    ctx = ContextManager(budget=4000, llm=llm, artifact_store=ArtifactStore(tmp_path), compaction_policy=policy)
    base = [
        Message(role="system", content="system"),
        Message(role="user", content="protected goal"),
        Message(role="assistant", content="prior complete"),
        Message(role="user", content="next"),
    ]
    ctx.register_completed_prefix(base, 3, "valid old stage summary")
    asyncio.run(ctx.prepare(base))
    previous = (ctx._summary, ctx._covered_count, ctx._retained_tail, ctx._cache_identity)
    grown = [*base, Message(role="assistant", content="x" * 20000), Message(role="user", content="continue")]
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ctx.force_compact(grown) if manual else ctx.prepare(grown))
    assert ctx.pending_compaction is None
    assert (ctx._summary, ctx._covered_count, ctx._retained_tail, ctx._cache_identity) == previous
    changed = [m.model_copy(deep=True) for m in grown]
    changed[1].content = "other branch"
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ctx.force_compact(changed) if manual else ctx.prepare(changed))
    assert ctx._summary is None and ctx.pending_compaction is None


def test_summary_does_not_discard_unresolved_tool_group():
    llm = LLM()
    ctx = ContextManager(
        budget=3000,
        llm=llm,
        keep_recent_tokens=1,
        compaction_policy=CompactionPolicy(
            mode="layered", model_context_window=10000, output_reserve=100, safety_margin=100, summary_max_tokens=200
        ),
    )
    source = [
        Message(role="system", content="system"),
        Message(role="user", content="goal"),
        Message(
            role="assistant",
            content="x" * 10000,
            metadata={"tool_calls": [{"id": "unfinished", "name": "shell", "arguments": {}}]},
        ),
        Message(role="user", content="continue"),
    ]
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ctx.prepare(source))
    assert not llm.calls and source[2].metadata["tool_calls"][0]["id"] == "unfinished"
