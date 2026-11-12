"""Branch-bound compaction snapshots: only the current path may supply a cache."""

from __future__ import annotations

import pytest
from my_agent_llm import Response

from my_agent_core.agent import Agent
from my_agent_core.session import Session, SessionStore
from my_agent_core.session.entries import CompactionEntry


class CapturingLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def achat_stream(self, *, messages, tools=None, model=None, **kwargs):
        _ = (tools, model, kwargs)
        self.calls.append([m.model_copy(deep=True) for m in messages])
        yield Response(content="ok", model="fake")


async def _run_agent(agent, text):
    return [e async for e in agent.prompt_stream(text)]


def _session(tmp_path) -> Session:
    s = Session(path=tmp_path / "session.jsonl", cwd=str(tmp_path))
    s.add_message("system", "SYS")
    s.add_message("user", "u1")
    s.add_message("assistant", "a1")
    return s


def test_other_branch_deeper_summary_is_not_selected(tmp_path):
    s = _session(tmp_path)
    branch_point = s.tree.current_id
    # Branch A: more messages, then a deep compaction snapshot.
    for i in range(4):
        s.add_message("user", f"a-{i}")
    s.add_summary_cache("A-SUMMARY", covered_count=4, retained_tail=[], tokens_before=100)
    assert s.get_latest_compaction_cache()["summary"] == "A-SUMMARY"

    # Branch B starts at the older branch point and is shallower.
    s.tree.rewind(branch_point)
    s.add_message("user", "b-1")
    assert s.get_latest_compaction_cache() is None  # A's cache is off-path, not reused

    s.add_summary_cache("B-SUMMARY", covered_count=1, retained_tail=[], tokens_before=10)
    assert s.get_latest_compaction_cache()["summary"] == "B-SUMMARY"


def test_provenance_and_legacy_entries(tmp_path):
    s = _session(tmp_path)
    s.add_summary_cache("FRESH", covered_count=2, retained_tail=[], tokens_before=50)
    latest = [e for e in s.tree.entries.values() if isinstance(e, CompactionEntry)][-1]
    md = latest.metadata
    assert md["provenance_version"] == 1
    assert md["covered_ids"] and md["source_path_id"]
    assert s.get_latest_compaction_cache()["summary"] == "FRESH"

    # Legacy entry without provenance is accepted only on the current path.
    legacy = CompactionEntry(parent_id=s.tree.current_id, summary="LEGACY", replaces_entry_ids=[], metadata={"covered_count": 1})
    s.tree.entries[legacy.id] = legacy
    assert s.get_latest_compaction_cache()["summary"] == "LEGACY"

    # Reload keeps the same on-path selection.
    s.save()
    loaded = Session.load(s.path)
    assert loaded.get_latest_compaction_cache()["summary"] == "LEGACY"


def test_implausible_coverage_is_rejected(tmp_path):
    s = _session(tmp_path)
    s.add_message("user", "only-one-message-so-far")
    bogus = CompactionEntry(
        parent_id=s.tree.current_id,
        summary="BOGUS",
        replaces_entry_ids=[],
        metadata={"covered_count": 99},
    )
    s.tree.entries[bogus.id] = bogus
    assert s.get_latest_compaction_cache() is None


def test_ordered_coverage_identity_is_required(tmp_path):
    s = _session(tmp_path)
    s.add_summary_cache("FRESH", covered_count=2, retained_tail=[], tokens_before=5)
    entry = [e for e in s.tree.entries.values() if isinstance(e, CompactionEntry)][-1]
    entry.metadata["covered_ids"] = list(reversed(entry.metadata["covered_ids"]))
    assert s.get_latest_compaction_cache() is None


def test_foreign_floor_does_not_lock_other_branch(tmp_path):
    s = _session(tmp_path)
    branch_point = s.tree.current_id
    s.add_message("user", "a-1")
    s.add_message("user", "a-2")
    s.add_summary_cache("A", covered_count=1, retained_tail=[], tokens_before=5)
    assert s.compaction_floor == s.tree.current_id
    # switch to a sibling branch: floor belongs to A, must not block rewinds here
    s.tree.rewind(branch_point)
    s.add_message("user", "b-1")
    s.rewind(branch_point)  # would raise before the ownership fix
    # valid floor on its own branch still guards
    s.tree.rewind(branch_point)
    s.add_message("user", "a-again")
    s.add_summary_cache("A2", covered_count=1, retained_tail=[], tokens_before=5)
    s.add_message("user", "post-floor")
    with pytest.raises(ValueError):
        s.rewind(branch_point)


# ── product-level: same Agent across branches, resume, repeated prepare ──


@pytest.mark.anyio
async def test_agent_a_then_b_does_not_leak_a_summary(tmp_path):
    from types import SimpleNamespace
    from my_agent_llm.request_projection import RequestProjection

    store = SessionStore(tmp_path / "sess")
    session = store.create()
    session.add_message("system", "SYS")
    session.add_message("user", "base")
    branch_point = session.tree.current_id
    session.add_message("user", "a-history")
    a_tail = [session.get_current_path_messages()[-1].model_dump()]
    session.add_summary_cache("A-SUMMARY", covered_count=2, retained_tail=a_tail, tokens_before=9)

    llm = CapturingLLM()
    projection = RequestProjection("fake")
    llm._provider = SimpleNamespace(request_projection=projection)
    agent = Agent(llm=llm, tools=[], session=session, system_prompt="SYS", context_budget=100000)
    await _run_agent(agent, "go-a")
    assert any("A-SUMMARY" in (m.text_content or "") for m in llm.calls[-1])

    # switch to sibling branch B with different history, same Agent
    session.tree.rewind(branch_point)
    session.add_message("user", "b-history")
    await _run_agent(agent, "go-b")
    last = llm.calls[-1]
    assert not any("A-SUMMARY" in (m.text_content or "") for m in last)
    assert any("b-history" in (m.text_content or "") for m in last)
    rec = projection.observe(model="m", system=None, tools=None, messages=[{"role": "user", "content": "x"}])
    assert any("summary_dropped" in r for r in rec.reasons)


@pytest.mark.anyio
async def test_resume_and_repeated_prepare_keep_single_tail(tmp_path):
    store = SessionStore(tmp_path / "sess2")
    session = store.create()
    session.add_message("system", "SYS")
    session.add_message("user", "u1")
    session.add_message("assistant", "a1")
    session.add_message("user", "u2")
    tail = [session.get_current_path_messages()[-1].model_dump()]
    session.add_summary_cache("SUM", covered_count=3, retained_tail=tail, tokens_before=5)
    session.save()

    loaded = Session.load(session.path)
    llm = CapturingLLM()
    agent = Agent(llm=llm, tools=[], session=loaded, system_prompt="SYS", context_budget=100000)
    await _run_agent(agent, "one")
    await _run_agent(agent, "two")
    assert len(llm.calls) == 2
    for call in llm.calls:
        texts = [m.text_content or "" for m in call]
        assert sum("SUM" in t for t in texts) == 1  # summary injected once, not repeated
        assert sum(t == "u2" for t in texts) == 1   # retained tail not duplicated
    assert len(loaded.tree.get_current_path()) >= 4


class SummarizingLLM(CapturingLLM):
    def __init__(self):
        super().__init__()
        self.summary_calls = 0

    async def achat(self, **kwargs):
        self.summary_calls += 1
        return Response(content="<summary>VERIFIED-SUMMARY</summary>", model="fake")


@pytest.mark.anyio
async def test_empty_tail_resume_uses_canonical_coverage(tmp_path):
    session = Session(path=tmp_path / "canonical.jsonl")
    session.add_message("user", "first")
    session.add_message("assistant", "answer")
    llm = SummarizingLLM()
    agent = Agent(llm=llm, tools=[], session=session, system_prompt="HOST SYSTEM")
    await agent.compact()
    cache = session.get_latest_compaction_cache()
    assert cache["covered_ids"] == [e.id for e in session.tree.get_current_path()]
    assert len(cache["covered_message_keys"]) == 2  # injected system has no tree entry
    loaded = Session.load(session.path)
    resumed = Agent(llm=llm, tools=[], session=loaded, system_prompt="HOST SYSTEM")
    await _run_agent(resumed, "next")
    await _run_agent(resumed, "again")
    assert llm.summary_calls == 1
    for call in llm.calls:
        assert sum("VERIFIED-SUMMARY" in m.text_content for m in call) == 1
        assert not any(m.text_content == "first" for m in call)


@pytest.mark.anyio
async def test_session_replacement_with_same_entry_ids_reloads_summary(tmp_path):
    import copy

    first = _session(tmp_path)
    tail = [first.get_current_path_messages()[-1].model_dump()]
    first.add_summary_cache("FIRST", covered_count=2, retained_tail=tail, tokens_before=5)
    second = Session(path=tmp_path / "different.jsonl")
    second.tree = copy.deepcopy(first.tree)
    for entry in second.tree.entries.values():
        if isinstance(entry, CompactionEntry):
            entry.summary = "SECOND"
    llm = CapturingLLM()
    agent = Agent(llm=llm, tools=[], session=first, system_prompt="SYS")
    await _run_agent(agent, "one")
    agent.session = second
    await _run_agent(agent, "two")
    assert any("SECOND" in m.text_content for m in llm.calls[-1])
    assert not any("FIRST" in m.text_content for m in llm.calls[-1])


@pytest.mark.anyio
async def test_bridge_rejects_summary_from_unrelated_input(tmp_path):
    from my_agent_core.context import ContextManager, ContextSessionBridge
    from my_agent_llm import Message

    session = Session(path=tmp_path / "unrelated.jsonl")
    session.add_message("user", "q1")
    ctx = ContextManager(llm=SummarizingLLM(), budget=1000, keep_recent_tokens=100)
    await ctx.prepare([Message(role="user", content="x" * 300) for _ in range(20)])
    ContextSessionBridge(session).write_compaction(ctx)
    assert session.compaction_floor is None
    assert session.get_latest_compaction_cache() is None


@pytest.mark.anyio
async def test_artifact_projection_precedes_budget_and_preserves_summary_identity(tmp_path):
    from my_agent_core.artifacts import ArtifactStore
    from my_agent_core.context import ContextManager, ContextSessionBridge
    from my_agent_llm import Message

    session = Session(path=tmp_path / "artifact.jsonl")
    session.add_message("user", "task")
    session.add_message("assistant", "", tool_calls=[{"id": "big", "type": "function", "function": {"name": "big", "arguments": "{}"}}])
    session.add_message("tool", "X" * 100_000 + "ORIGINAL-END", tool_call_id="big", tool_name="big")
    session.add_message("user", "continue")
    store = ArtifactStore(tmp_path)
    llm = SummarizingLLM()
    ctx = ContextManager(llm=llm, budget=10000, keep_recent_tokens=100, artifact_store=store)
    messages = [Message(role="system", content="SYS"), *session.get_full_history_messages()]
    view = await ctx.prepare(messages)
    assert llm.summary_calls == 0  # preview fits; raw 100KB would have forced compaction
    tool = next(m for m in view if m.role == "tool")
    assert tool.metadata["artifact_id"]
    assert session.get_full_history_messages()[2].content.endswith("ORIGINAL-END")
    assert [m.model_dump() for m in await ctx.prepare(messages)] == [m.model_dump() for m in view]
    ctx.keep_recent_tokens = 20_000  # cover the projected tool result, not just the user prefix
    await ctx.force_compact(messages)
    assert ctx._retained_tail == []
    from my_agent_core.context import _message_fingerprint
    assert _message_fingerprint(messages[3]) in ctx.cache_identity["covered_message_keys"]
    assert _message_fingerprint(tool) not in ctx.cache_identity["covered_message_keys"]
    ContextSessionBridge(session).write_compaction(ctx)
    compacted = await ctx.prepare(messages)
    assert any("VERIFIED-SUMMARY" in m.text_content for m in compacted)
    assert await ctx.prepare(messages) == compacted
    assert llm.summary_calls == 1  # canonical hashes do not mistake stable previews for a rewrite
    assert session.get_latest_compaction_cache() is not None
    restored = ContextManager(llm=llm, budget=10000, artifact_store=store)
    ContextSessionBridge(session).restore_cache(restored)
    replay = await restored.prepare(messages)
    assert any("VERIFIED-SUMMARY" in m.text_content for m in replay)
    assert llm.summary_calls == 1


def test_returning_to_a_branch_restores_its_floor(tmp_path):
    session = _session(tmp_path)
    base = session.tree.current_id
    session.add_message("user", "a")
    a_tip = session.tree.current_id
    session.add_summary_cache("A", covered_count=1, retained_tail=[], tokens_before=5)
    session.tree.rewind(base)
    session.add_message("user", "b")
    session.add_summary_cache("B", covered_count=1, retained_tail=[], tokens_before=5)
    session.tree.rewind(a_tip)
    with pytest.raises(ValueError, match="compaction floor"):
        session.rewind(base)


@pytest.mark.anyio
async def test_compacted_runtime_facts_and_clear_remain_visible_without_history_duplicates(tmp_path):
    from my_agent_core.runtime_context import SNAPSHOT_KIND, CLEAR_KIND

    session = Session(path=tmp_path / "runtime.jsonl")
    llm = SummarizingLLM()
    agent = Agent(llm=llm, tools=[], session=session, system_prompt="SYS")
    agent.runtime_context.set("phase", "active")
    await _run_agent(agent, "one")
    await agent.compact()
    await _run_agent(agent, "two")
    assert sum((m.metadata or {}).get("kind") == SNAPSHOT_KIND for m in llm.calls[-1]) == 1
    assert sum((m.metadata or {}).get("kind") == SNAPSHOT_KIND for m in session.get_full_history_messages()) == 1
    agent.runtime_context.clear()
    await _run_agent(agent, "three")
    assert sum((m.metadata or {}).get("kind") == CLEAR_KIND for m in llm.calls[-1]) == 1
    await agent.compact()
    await _run_agent(agent, "four")
    assert sum((m.metadata or {}).get("kind") == CLEAR_KIND for m in llm.calls[-1]) == 1
    assert sum((m.metadata or {}).get("kind") == CLEAR_KIND for m in session.get_full_history_messages()) == 1
