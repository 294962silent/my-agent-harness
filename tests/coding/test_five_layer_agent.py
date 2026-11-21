"""Product-path defaults for the five-layer context projection."""

from __future__ import annotations

from my_agent_llm import Response

from my_coding_agent.agent import CodingAgent
from my_coding_agent.settings import CompactionSettings


class _LLM:
    async def achat_stream(self, **_kwargs):
        yield Response(content="ok", model="fake")


def test_coding_agent_enables_five_layers_by_default(tmp_path) -> None:
    agent = CodingAgent(
        workspace=tmp_path,
        llm=_LLM(),
        session=tmp_path / "session.jsonl",
        system_prompt="SYSTEM",
        auto_load_mcp=False,
        skill_dirs=[],
        subagent_dirs=[],
        extension_dirs=[],
        plugin_dirs=[],
        memory_dir=False,
        task_store=False,
    )

    assert agent.context_manager.compression_config.enabled is True
    assert agent.context_manager.compression_config.five_layers_enabled is True


def test_coding_agent_forwards_compaction_overrides(tmp_path) -> None:
    settings = CompactionSettings(
        enabled=False,
        reserveTokens=2048,
        keepRecentTokens=1234,
        toolResultMaxChars=2222,
        toolRoundMaxChars=8888,
        microcompactMinChars=111,
    )
    agent = CodingAgent(
        workspace=tmp_path,
        llm=_LLM(),
        session=tmp_path / "custom.jsonl",
        system_prompt="SYSTEM",
        compaction_settings=settings,
        auto_load_mcp=False,
        skill_dirs=[],
        subagent_dirs=[],
        extension_dirs=[],
        plugin_dirs=[],
        memory_dir=False,
        task_store=False,
    )

    config = agent.context_manager.compression_config
    assert config.enabled is False
    assert config.five_layers_enabled is True
    assert config.reserve_tokens == 2048
    assert config.keep_recent_tokens == 1234
    assert config.tool_result_max_chars == 2222
    assert config.tool_round_max_chars == 8888
    assert config.microcompact_min_chars == 111
