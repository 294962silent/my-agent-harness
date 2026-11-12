# pyright: reportArgumentType=false
"""End-to-end: CodingAgent assembles artifact tools; Agent loop previews long tool output."""

from __future__ import annotations

import re

import pytest
from my_agent_llm import Response

from my_agent_core.artifacts import ArtifactStore
from my_agent_core.session import Session, SessionStore
from my_agent_core.tools import tool
from my_coding_agent.agent import CodingAgent
from my_coding_agent.tools import build_coding_tools
from my_coding_agent.permissions import PermissionGate

BIG = "FULLTEXT_START\n" + ("line of log output\n" * 8000) + "ERROR_AT_END\n"


@tool(name="biglog", description="emit a large log")
def biglog() -> str:
    return BIG


class ArtifactAwareLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []
        self.artifact_read_result = ""

    async def achat_stream(self, *, messages, tools=None, model=None, **kwargs):
        _ = (tools, model, kwargs)
        self.calls.append([m.model_copy(deep=True) for m in messages])
        if len(self.calls) == 1:
            yield Response(
                content="",
                model="fake",
                tool_calls=[{"id": "c1", "type": "function", "function": {"name": "biglog", "arguments": "{}"}}],
            )
        elif len(self.calls) == 2:
            tool_msgs = [m for m in messages if m.role == "tool"]
            handle = re.search(r"artifact:[0-9a-f]+:[0-9a-f]{32}", tool_msgs[-1].text_content or "")
            if not handle:
                raise AssertionError("preview did not expose an artifact handle")
            yield Response(
                content="",
                model="fake",
                tool_calls=[
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "artifact_read",
                            "arguments": f'{{"id": "{handle.group(0)}", "cursor": 0}}',
                        },
                    }
                ],
            )
        else:
            self.artifact_read_result = [m for m in messages if m.role == "tool"][-1].text_content or ""
            yield Response(content="done", model="fake")


@pytest.mark.anyio
async def test_long_tool_output_preview_then_artifact_read(tmp_path):
    llm = ArtifactAwareLLM()
    agent = CodingAgent(
        workspace=tmp_path,
        llm=llm,
        extra_tools=[biglog],
        session=SessionStore(tmp_path / "s").create(),
        system_prompt="SYS",
        context_budget=200000,
        auto_load_mcp=False,
        permission_gate=PermissionGate(mode="review", workspace=tmp_path, session_approved={"biglog"}),
    )
    events = [e async for e in agent.run_stream("go")]
    assert events  # real lifecycle events flowed
    assert len(llm.calls) == 3

    second = llm.calls[1]
    preview = [m for m in second if m.role == "tool"][-1].text_content or ""
    assert preview.startswith("[ARTIFACT artifact:")
    assert "FULLTEXT_START" in preview and "ERROR_AT_END" in preview
    assert len(preview) < 8000  # bounded, full body not in transcript

    third = llm.calls[2]
    read_result = [m for m in third if m.role == "tool"][-1].text_content or ""
    assert "FULLTEXT_START" in read_result
    assert "[next_cursor=" in read_result
    assert not read_result.startswith("[ARTIFACT ")
    assert len(list(agent.artifact_store._owner_dir.glob("*.txt"))) == 1
    assert "Error:" not in read_result.splitlines()[0]
    # Session keeps the original full tool result, not the preview
    stored = [m for m in agent.messages if m.role == "tool"]
    assert any(m.text_content == BIG for m in stored)
    old_handle = re.search(r"artifact:[0-9a-f]+:[0-9a-f]{32}", preview).group(0)
    resumed_session = Session.load(agent.session.path)
    forked_session = resumed_session.fork(resumed_session.tree.current_id)
    for history in (resumed_session, forked_session):
        reopened = CodingAgent(workspace=tmp_path, llm=llm, session=history, system_prompt="SYS", auto_load_mcp=False)
        assert reopened.artifact_store.read_page(old_handle)["text"].startswith("FULLTEXT_START")
        reread = reopened.artifact_store.read_page(old_handle, cursor=0, max_bytes=256)
        assert reread["next_cursor"] == 256


def test_coding_agent_registers_artifact_tools_at_construction(tmp_path):
    class DummyLLM:
        pass

    store = ArtifactStore(tmp_path)
    tools = build_coding_tools(tmp_path, artifact_store=store)
    names = [t.name for t in tools]
    assert names[-2:] == ["artifact_read", "artifact_search"]
    assert len(names) == 9
    agent = CodingAgent(
        workspace=tmp_path,
        llm=DummyLLM(),
        session=SessionStore(tmp_path / "s2").create(),
        auto_load_mcp=False,
    )
    registered = set(agent.agent.registry._tools.keys())
    assert {"artifact_read", "artifact_search", "read", "bash"} <= registered


@pytest.mark.anyio
async def test_bounded_artifact_pages_preserve_multiline_unicode(tmp_path):
    from my_agent_core.artifacts import READ_MAX_BYTES_HARD

    store = ArtifactStore(tmp_path)
    text = "汉字🙂\n" * 20000
    ref = store.save(text, tool_name="bash", tool_call_id="u")
    read_tool = build_coding_tools(tmp_path, artifact_store=store)[-2]
    result = await read_tool.execute({"id": ref.handle(), "max_bytes": 10**9})
    serialized = result.serialize()
    assert len(serialized.encode("utf-8")) <= READ_MAX_BYTES_HARD + 200
    assert "Output truncated" not in serialized
    assert "[next_cursor=" in serialized
    page = store.read_page(ref.handle(), max_bytes=10**9)
    assert page["text"] in serialized
    assert page["cursor"] == len(page["text"].encode("utf-8"))


@pytest.mark.anyio
async def test_default_gate_allows_only_artifact_reads(tmp_path):
    from my_agent_core.hooks import ToolCallHook

    gate = PermissionGate(workspace=tmp_path)
    for name in ("artifact_read", "artifact_search"):
        assert not (await gate(ToolCallHook(tool_call_id="r", tool_name=name, args={"id": "invalid"}))).block
    assert (await gate(ToolCallHook(tool_call_id="w", tool_name="write", args={"path": "x", "content": "x"}))).block


@pytest.mark.anyio
async def test_ordinary_read_still_rejects_external_file(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-secret.txt")
    outside.write_text("private", encoding="utf-8")
    read_tool = build_coding_tools(tmp_path)[0]
    result = await read_tool.execute({"path": str(outside)})
    assert not result.ok
    assert "private" not in result.serialize()
