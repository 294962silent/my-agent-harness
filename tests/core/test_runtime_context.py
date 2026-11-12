# pyright: reportArgumentType=false
"""Deterministic tests for runtime-context projections entering real Agent input."""

from __future__ import annotations

import pytest
from my_agent_llm import Response

from my_agent_core.agent import Agent
from my_agent_core.runtime_context import CLEAR_KIND, SNAPSHOT_KIND
from my_agent_core.session import SessionStore


class CapturingLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def achat_stream(self, *, messages, tools=None, model=None, **kwargs):
        _ = (tools, model, kwargs)
        self.calls.append([m.model_copy(deep=True) for m in messages])
        yield Response(content="ok", model="fake")


def _agent(tmp_path, llm, name="s"):
    store = SessionStore(tmp_path / name)
    session = store.create()
    return Agent(llm=llm, tools=[], session=session, system_prompt="SYS", context_budget=100000)


async def _run(agent, text="hi"):
    events = [e async for e in agent.prompt_stream(text)]
    return events


def _projections(messages: list) -> list:
    return [m for m in messages if isinstance(m.metadata, dict) and m.metadata.get("kind") in (SNAPSHOT_KIND, CLEAR_KIND)]


@pytest.mark.anyio
async def test_c1_repeat_c2_and_clear_sequence(tmp_path):
    llm = CapturingLLM()
    agent = _agent(tmp_path, llm)
    agent.runtime_context.set("cwd", "/w")
    agent.runtime_context.set("gpu", "2")

    await _run(agent)
    first = _projections(llm.calls[0])
    assert len(first) == 1 and first[0].metadata["kind"] == SNAPSHOT_KIND
    assert "cwd: /w" in first[0].text_content and first[0].text_content.startswith("[RUNTIME CONTEXT SNAPSHOT]")
    first_text = first[0].text_content

    await _run(agent)
    assert len(_projections(llm.calls[1])) == 1  # unchanged: not repeated

    agent.runtime_context.set("gpu", "0")
    await _run(agent)
    second = _projections(llm.calls[2])
    assert len(second) == 2
    assert second[0].text_content == first_text  # old prefix not rewritten
    assert "gpu: 0" in second[1].text_content

    agent.runtime_context.clear()
    await _run(agent)
    cleared = _projections(llm.calls[3])
    assert len(cleared) == 3 and cleared[-1].metadata["kind"] == CLEAR_KIND
    assert "No runtime facts" in cleared[-1].text_content

    await _run(agent)
    assert len(_projections(llm.calls[4])) == 3  # clear marker not repeated


@pytest.mark.anyio
async def test_no_dynamic_state_keeps_old_behaviour(tmp_path):
    llm = CapturingLLM()
    agent = _agent(tmp_path, llm)
    await _run(agent, "plain")
    assert _projections(llm.calls[0]) == []
    roles = [m.role for m in llm.calls[0]]
    assert roles == ["system", "user"]


@pytest.mark.anyio
async def test_snapshot_is_at_tail_before_new_user_turn(tmp_path):
    llm = CapturingLLM()
    agent = _agent(tmp_path, llm)
    agent.runtime_context.set("step", "3")
    await _run(agent, "task")
    roles = [m.role for m in llm.calls[0]]
    assert roles[-1] == "user"
    assert _projections(llm.calls[0])[-1] is llm.calls[0][-1]


@pytest.mark.anyio
async def test_new_session_reprojects_same_facts(tmp_path):
    llm_a, llm_b = CapturingLLM(), CapturingLLM()
    a = _agent(tmp_path, llm_a, "a")
    b = _agent(tmp_path, llm_b, "b")
    for agent in (a, b):
        agent.runtime_context.set("same", "fact")
        await _run(agent)
    assert len(_projections(llm_a.calls[0])) == 1
    assert len(_projections(llm_b.calls[0])) == 1  # fresh session is not deduped against another session


@pytest.mark.anyio
async def test_projection_persisted_with_metadata(tmp_path):
    llm = CapturingLLM()
    agent = _agent(tmp_path, llm)
    agent.runtime_context.set("k", "v")
    await _run(agent)
    restored = agent.session.get_full_history_messages()
    projected = [m for m in restored if isinstance(m.metadata, dict) and m.metadata.get("kind") == SNAPSHOT_KIND]
    assert len(projected) == 1
    assert projected[0].metadata["snapshot_hash"]


class ToolCallingLLM:
    """Yields a tool call, then a final answer; captures each request's messages."""

    def __init__(self, tool_name: str = "bump") -> None:
        self.calls: list[list] = []
        self.tool_name = tool_name

    async def achat_stream(self, *, messages, tools=None, model=None, **kwargs):
        _ = (tools, model, kwargs)
        self.calls.append([m.model_copy(deep=True) for m in messages])
        if len(self.calls) <= 2:
            yield Response(
                content="",
                model="fake",
                tool_calls=[
                    {
                        "id": f"call_{len(self.calls)}",
                        "type": "function",
                        "function": {"name": self.tool_name, "arguments": "{}"},
                    }
                ],
            )
        else:
            yield Response(content="done", model="fake")


@pytest.mark.anyio
async def test_c2_visible_on_second_model_request_without_new_user_prompt(tmp_path):
    from my_agent_core.tools import tool

    llm = ToolCallingLLM()

    @tool(name="bump", description="bump runtime state")
    def bump() -> str:
        agent.runtime_context.set("phase", "2")
        return "bumped"

    store = SessionStore(tmp_path / "mid")
    agent = Agent(llm=llm, tools=[bump], session=store.create(), system_prompt="SYS", context_budget=100000)
    agent.runtime_context.set("phase", "1")
    await _run(agent, "go")

    assert len(llm.calls) == 3
    second = _projections(llm.calls[1])
    assert len(second) == 2  # C1 + full C2 in the SAME single run
    assert "phase: 1" in second[0].text_content
    assert "phase: 2" in second[1].text_content
    third = _projections(llm.calls[2])
    assert len(third) == 2  # repeated C2 not re-injected
    assert [p.text_content for p in third] == [p.text_content for p in second]
    # tool/result pairing stays intact: assistant tool_calls and tool result are adjacent
    roles = [m.role for m in llm.calls[1]]
    assert "tool" in roles
    idx = roles.index("tool")
    assert roles[idx - 1] == "assistant"


@pytest.mark.anyio
async def test_clear_visible_mid_run_once(tmp_path):
    from my_agent_core.tools import tool

    llm = ToolCallingLLM()

    @tool(name="bump", description="clear runtime state")
    def bump() -> str:
        agent.runtime_context.clear()
        return "cleared"

    store = SessionStore(tmp_path / "mid2")
    agent = Agent(llm=llm, tools=[bump], session=store.create(), system_prompt="SYS", context_budget=100000)
    agent.runtime_context.set("phase", "1")
    await _run(agent, "go")

    second = _projections(llm.calls[1])
    assert [p.metadata["kind"] for p in second] == [SNAPSHOT_KIND, CLEAR_KIND]
    third = _projections(llm.calls[2])
    assert len(third) == 2  # clear marker not repeated
