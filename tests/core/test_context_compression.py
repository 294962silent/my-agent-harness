"""Contracts for the five-layer context projection pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from my_agent_core.artifacts import ArtifactStore
from my_agent_core.agent import Agent
from my_agent_core.context import ContextManager, ContextSessionBridge
from my_agent_core.context_compression import (
    CompressionConfig,
    apply_collapse,
    apply_microcompact,
    apply_snip,
    apply_tool_result_budget,
    project_messages,
    select_collapse_range,
    select_snip_ids,
    validate_projection_plan,
)
from my_agent_core.session import Session, SessionState
from my_agent_core.events import ContextCompacted
from my_agent_llm.models import Message
from my_agent_llm.models import Response


def _estimate(messages: list[Message]) -> int:
    return sum(max(1, len(str(message.content)) // 4) for message in messages)


def _tool_call(call_id: str, name: str = "read") -> dict:
    return {"id": call_id, "type": "function", "name": name, "args": {}}


def test_projection_ids_are_stable_and_duplicate_safe() -> None:
    messages = [
        Message(role="user", content="same"),
        Message(role="user", content="same"),
        Message(role="assistant", content="answer"),
    ]

    first = project_messages(messages)
    second = project_messages(messages)

    assert [item.projection_id for item in first] == [
        item.projection_id for item in second
    ]
    assert first[0].projection_id != first[1].projection_id
    assert first[0].projection_id.endswith(":0")
    assert first[1].projection_id.endswith(":1")


def test_compression_config_rejects_cross_field_errors() -> None:
    with pytest.raises(ValueError, match="tool_round_max_chars"):
        CompressionConfig(
            tool_result_max_chars=6000,
            tool_round_max_chars=5000,
        )
    with pytest.raises(ValueError, match="context_trigger_ratio"):
        CompressionConfig(context_trigger_ratio=0)


def _projection_plan() -> dict[str, object]:
    return {
        "version": 1,
        "source_count": 4,
        "source_hash": "a" * 64,
        "snip_ids": ["projection:user:old:0"],
        "microcompact_projection_ids": [],
        "collapse": None,
        "layers": ["layer2:snip"],
        "cache_epoch": 3,
        "budget_exceeded": True,
    }


def test_projection_plan_validation_is_bounded_and_strict() -> None:
    assert validate_projection_plan(_projection_plan())

    bad_hash = _projection_plan()
    bad_hash["source_hash"] = "../not-a-hash"
    assert not validate_projection_plan(bad_hash)

    bad_layers = _projection_plan()
    bad_layers["layers"] = ["layer9:unknown"]
    assert not validate_projection_plan(bad_layers)

    bad_collapse = _projection_plan()
    bad_collapse["layers"] = ["layer4:collapse"]
    bad_collapse["collapse"] = None
    assert not validate_projection_plan(bad_collapse)


def test_tool_result_budget_enforces_single_and_round_limits(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    messages = [
        Message(
            role="assistant",
            content="",
            metadata={"tool_calls": [_tool_call("a"), _tool_call("b"), _tool_call("c")]},
        ),
        Message(role="tool", content="a" * 4000, metadata={"tool_call_id": "a", "tool_name": "bash"}),
        Message(role="tool", content="b" * 5000, metadata={"tool_call_id": "b", "tool_name": "bash"}),
        Message(role="tool", content="c" * 6000, metadata={"tool_call_id": "c", "tool_name": "bash"}),
    ]
    config = CompressionConfig(
        tool_result_max_chars=5500,
        tool_round_max_chars=12000,
    )

    view, metrics = apply_tool_result_budget(
        messages,
        artifact_store=store,
        results_dir=None,
        config=config,
    )

    assert metrics["changed"] is True
    assert metrics["layer"] == "layer1:tool_budget"
    assert view[0].metadata["tool_calls"] == messages[0].metadata["tool_calls"]
    assert "[ARTIFACT" in view[2].content
    assert "[ARTIFACT" in view[3].content
    assert not view[1].content.startswith("[ARTIFACT")
    assert [view[index].metadata["tool_call_id"] for index in (1, 2, 3)] == [
        "a",
        "b",
        "c",
    ]


def test_snip_keeps_task_and_recent_tail() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="task constraints"),
        Message(role="assistant", content="ack"),
        Message(role="user", content="old work " * 20),
        Message(role="assistant", content="old answer " * 20),
        Message(role="user", content="recent request"),
        Message(role="assistant", content="recent answer"),
    ]
    items = project_messages(messages)
    snip_ids = select_snip_ids(
        items,
        keep_recent_tokens=10,
        ratio=0.25,
    )

    view = apply_snip(items, set(snip_ids))

    assert messages[1].content in [message.content for message in view]
    assert messages[5].content in [message.content for message in view]
    assert messages[6].content in [message.content for message in view]
    assert messages[3].content not in [message.content for message in view]
    assert messages[4].content not in [message.content for message in view]


def test_microcompact_removes_only_old_tool_body() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content="",
            metadata={"tool_calls": [_tool_call("old")]},
        ),
        Message(
            role="tool",
            content="x" * 500,
            metadata={"tool_call_id": "old", "tool_name": "bash"},
        ),
        Message(role="user", content="latest request"),
        Message(role="assistant", content="latest answer " * 20),
    ]
    items = project_messages(messages)

    view, changed_ids = apply_microcompact(
        items,
        keep_recent_tokens=30,
        min_chars=200,
        ratio=0.25,
    )

    assert changed_ids == ["old"]
    assert view[2].role == "assistant"
    assert view[2].metadata["tool_calls"] == messages[2].metadata["tool_calls"]
    assert view[3].role == "tool"
    assert view[3].metadata["tool_call_id"] == "old"
    assert view[3].metadata["microcompacted"] is True
    assert view[3].content == "[microcompacted tool result; tool_call_id=old]"
    assert view[4:] == messages[4:]


def test_context_collapse_targets_only_middle_history() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="task constraints"),
        Message(role="assistant", content="ack"),
        Message(role="user", content="middle one " * 20),
        Message(role="assistant", content="middle answer one " * 20),
        Message(role="user", content="middle two " * 20),
        Message(role="assistant", content="middle answer two " * 20),
        Message(role="user", content="recent request"),
        Message(role="assistant", content="recent answer"),
    ]
    items = project_messages(messages)

    selected = select_collapse_range(
        items,
        keep_recent_tokens=10,
        ratio=0.25,
    )
    assert selected is not None
    start, end = selected
    view = apply_collapse(messages, start, end, "middle summary")

    assert view[:3] == messages[:3]
    assert view[-2:] == messages[-2:]
    assert view[3].role == "user"
    assert view[3].content == "[Context collapse]\n\nmiddle summary"
    assert messages[3:7] and all(message not in view[3:4] for message in messages[3:7])


class _Summarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, **_kwargs):
        self.calls += 1
        return Response(content="middle summary", model="fake")


class _StreamingLLM:
    async def achat_stream(self, **_kwargs):
        yield Response(content="done", model="fake")


@pytest.mark.anyio
async def test_projection_snapshot_restores_without_summary_or_floor(tmp_path: Path) -> None:
    session = Session(path=tmp_path / "session.jsonl")
    messages = [Message(role="system", content="system")]
    for index in range(6):
        user = Message(role="user", content=f"group-{index} " + "x" * 3000)
        assistant = Message(role="assistant", content=f"answer-{index}")
        messages.extend((user, assistant))
        session.add_message(user.role, user.content)
        session.add_message(assistant.role, assistant.content)
    recent = Message(role="user", content="recent " + "z" * 3000)
    messages.append(recent)
    session.add_message(recent.role, recent.content)
    branch_tip = session.tree.current_id

    llm = _Summarizer()
    config = CompressionConfig(five_layers_enabled=True)
    context = ContextManager(
        llm=llm,
        budget=3000,
        keep_recent_tokens=100,
        compression_config=config,
    )
    first = await context.prepare(messages)
    assert len(first) < len(messages)
    assert context.pending_context_diagnostics["layers_applied"] == ("layer2:snip",)
    assert context.pending_projection_plan is not None
    assert llm.calls == 0

    entry_id = ContextSessionBridge(session).write_projection(context)
    assert entry_id is not None
    assert session.tree.current_id == branch_tip
    assert session.compaction_floor is None
    assert session.get_latest_compaction_cache() is None
    folded = SessionState.from_entries(
        list(session.tree.entries.values()),
        leaf_id=entry_id,
    )
    assert not any(message.content == "" for message in folded.messages)
    persisted = session.get_latest_projection_plan()
    assert persisted is not None
    assert persisted["entry_id"] == entry_id
    assert persisted["layers"] == ["layer2:snip"]

    restored = ContextManager(
        llm=llm,
        budget=3000,
        keep_recent_tokens=100,
        compression_config=config,
    )
    assert ContextSessionBridge(session).restore_projection_plan(restored) is not None
    second = await restored.prepare(messages)
    assert [message.model_dump() for message in second] == [
        message.model_dump() for message in first
    ]
    assert restored.pending_projection_plan is None
    assert llm.calls == 0

    session.tree.rewind(session.tree.get_current_path()[2].id)
    assert session.get_latest_projection_plan() is None


@pytest.mark.anyio
async def test_provider_cache_miss_reopens_microcompact_epoch(tmp_path: Path) -> None:
    context = ContextManager(
        llm=_Summarizer(),
        budget=10_000,
        compression_config=CompressionConfig(five_layers_enabled=True),
    )
    context._cache_epoch_invalidated = False
    epoch = context._cache_epoch

    context.record_usage(
        {
            "prompt_tokens": 100,
            "prompt_cache_miss_tokens": 80,
        }
    )

    assert context._cache_epoch_invalidated is True
    assert context._cache_epoch == epoch + 1
    assert context._last_cache_invalidation == "provider_cache_miss"


@pytest.mark.anyio
async def test_five_layer_pipeline_makes_context_collapse_reachable(tmp_path: Path) -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="task constraints"),
        Message(
            role="assistant",
            content="",
            metadata={"tool_calls": [_tool_call("old")]},
        ),
        Message(
            role="tool",
            content="T" * 5000,
            metadata={"tool_call_id": "old", "tool_name": "read"},
        ),
    ]
    for index in range(6):
        messages.extend(
            (
                Message(role="user", content=f"old-{index} " + "x" * 2500),
                Message(role="assistant", content="ok"),
            )
        )
    messages.append(Message(role="user", content="recent " + "z" * 3000))

    llm = _Summarizer()
    context = ContextManager(
        llm=llm,
        budget=1600,
        keep_recent_tokens=100,
        compression_config=CompressionConfig(five_layers_enabled=True),
    )

    view = await context.prepare(messages)

    assert context.pending_context_diagnostics["layers_applied"] == (
        "layer2:snip",
        "layer3:microcompact",
        "layer4:collapse",
    )
    assert any(message.content.startswith("[Context collapse]") for message in view)
    assert llm.calls == 1
    plan = context.pending_projection_plan
    assert plan is not None
    assert plan["collapse"]["summary"] == "middle summary"


@pytest.mark.anyio
async def test_five_layer_pipeline_uses_auto_compact_as_final_fallback(
    tmp_path: Path,
) -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="protected-head " + "h" * 5000),
        Message(role="assistant", content="ok"),
    ]
    for index in range(5):
        messages.extend(
            (
                Message(role="user", content=f"middle-{index} " + "m" * 2500),
                Message(role="assistant", content="ok"),
            )
        )
    messages.append(Message(role="user", content="recent " + "r" * 3000))

    llm = _Summarizer()
    context = ContextManager(
        llm=llm,
        budget=1600,
        keep_recent_tokens=100,
        compression_config=CompressionConfig(five_layers_enabled=True),
    )

    await context.prepare(messages)

    assert "layer5:auto_compact" in context.pending_context_diagnostics["layers_applied"]
    assert context.pending_compaction is not None
    assert context.pending_projection_plan is None
    assert llm.calls == 2


@pytest.mark.anyio
async def test_agent_persists_projection_only_event_without_moving_history(
    tmp_path: Path,
) -> None:
    session = Session(path=tmp_path / "agent-session.jsonl")
    for index in range(6):
        session.add_message("user", f"group-{index} " + "x" * 3000)
        session.add_message("assistant", f"answer-{index}")
    branch_tip = session.tree.current_id
    events: list[ContextCompacted] = []
    agent = Agent(
        llm=_StreamingLLM(),
        tools=[],
        session=session,
        system_prompt="SYSTEM",
        context_budget=2500,
        keep_recent_tokens=100,
        skill_dirs=[],
        memory_dir=False,
        task_store=False,
        compression_config=CompressionConfig(five_layers_enabled=True),
    )
    agent.subscribe(
        lambda event: events.append(event)
        if isinstance(event, ContextCompacted)
        else None
    )

    await agent.run("latest " + "z" * 3000)

    assert events
    assert events[0].projection_only is True
    assert "layer2:snip" in events[0].layers_applied
    assert session.get_latest_compaction_cache() is None
    assert session.compaction_floor is None
    assert session.get_latest_projection_plan() is not None
    assert session.tree.current_id != branch_tip
