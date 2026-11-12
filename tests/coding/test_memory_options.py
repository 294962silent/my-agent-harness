"""Coding facade forwards optional memory lifecycle settings."""
import pytest
from my_agent_core.session import Session
from my_agent_llm import Response
from my_coding_agent.agent import CodingAgent
from my_coding_agent.permissions import READONLY_TOOLS


class LLM:
    async def achat_stream(self, **kwargs):
        yield Response(content="ok", model="fake")


@pytest.mark.anyio
async def test_memory_options_and_context_exit(tmp_path):
    async with CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                           auto_load_mcp=False, memory_dir=tmp_path / "memory", memory_autodistill=True,
                           memory_idle_seconds=60, memory_distill_timeout=0.2) as coding:
        assert coding.agent._memory_distiller.idle_seconds == 60
        assert coding.agent._memory_distiller.timeout == 0.2
        assert coding.agent.registry.get("memory_read") is not None
        assert "memory_read" in READONLY_TOOLS
        await coding.run("请始终使用中文回答")
        assert coding.agent._memory_distiller.task is not None
    assert not coding.agent._memory_distiller._tasks


@pytest.mark.anyio
async def test_existing_rpc_close_mcp_path_reaps_memory_task(tmp_path):
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         auto_load_mcp=False, memory_dir=tmp_path / "m", memory_autodistill=True)
    await coding.run("请始终使用中文回答")
    assert coding.agent._memory_distiller.task is not None
    await coding.close_mcp()
    assert not coding.agent._memory_distiller._tasks
    await coding.aclose()
