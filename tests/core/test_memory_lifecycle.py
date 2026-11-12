"""Offline lifecycle contracts for optional idle memory extraction."""
import asyncio
from unittest.mock import Mock

import pytest
from my_agent_llm import Response
from my_agent_core.agent import Agent
from my_agent_core.session import Session


class MemoryLLM:
    def __init__(self):
        self.extracts = []
        self.fail = False

    async def achat_stream(self, **kwargs):
        yield Response(content="done", model="fake")

    async def achat(self, messages, **kwargs):
        self.extracts.append(messages)
        if self.fail:
            raise RuntimeError("offline extraction failure")
        return Response(content='[{"key":"reply_language","value":"中文","evidence":"请始终使用中文回答"}]', model="fake")


def make_agent(tmp_path, **kwargs):
    llm = MemoryLLM()
    agent = Agent(llm=llm, tools=[], session=Session(path=tmp_path / "s.jsonl"),
                  memory_dir=tmp_path / "memory", memory_autodistill=True,
                  memory_idle_seconds=0, memory_distill_timeout=0.1, **kwargs)
    return agent, llm


@pytest.mark.anyio
async def test_idle_is_shared_by_run_and_stream_and_close_drains(tmp_path):
    agent, llm = make_agent(tmp_path)
    await agent.run("请始终使用中文回答")
    assert llm.extracts == []  # response does not await idle
    await agent.wait_memory_idle()
    assert agent.memory_store.retrieve("reply_language")
    assert len(llm.extracts) == 1
    async for _ in agent.prompt_stream("请始终使用中文回答"):
        pass
    await agent.wait_memory_idle()
    assert len(llm.extracts) == 1  # successful evidence is deduplicated
    await agent.aclose()
    assert agent._memory_distiller.task is None


@pytest.mark.anyio
async def test_failed_extraction_retries_and_reset_clears_success(tmp_path):
    agent, llm = make_agent(tmp_path)
    llm.fail = True
    await agent.run("请始终使用中文回答")
    await agent.wait_memory_idle()
    assert not agent._memory_distiller.successful
    llm.fail = False
    await agent.run("继续")
    await agent.wait_memory_idle()
    assert len(llm.extracts) == 2
    agent.reset()
    assert not agent._memory_distiller.successful
    await agent.aclose()


@pytest.mark.anyio
async def test_fullmatch_trivial_retrieval_and_multimodal_preserved(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.memory_autodistill = False
    agent.memory_store.retrieve = Mock(return_value=None)
    await agent.run("继续")
    assert not agent.memory_store.retrieve.called
    content = [{"type":"text", "text":"继续修改端口8080"},
               {"type":"image", "data":"eA==", "mime_type":"image/png"}]
    await agent.run(content)
    agent.memory_store.retrieve.assert_called_once()
    assert any(isinstance(m.content, list) and any(p.get("type") == "image" for p in m.content) for m in agent.messages)
    await agent.aclose()


@pytest.mark.anyio
async def test_abort_and_branch_change_prevent_idle_write(tmp_path):
    agent, llm = make_agent(tmp_path)
    await agent.run("请始终使用中文回答")
    agent.abort()
    await agent.wait_memory_idle()
    assert not llm.extracts
    await agent.run("请始终使用中文回答")
    agent.session.add_message("user", "different current tip")
    await agent.wait_memory_idle()
    assert not llm.extracts
    await agent.aclose()


@pytest.mark.anyio
async def test_timeout_and_inflight_branch_change_do_not_write(tmp_path):
    agent, llm = make_agent(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    async def slow(messages, **kwargs):
        started.set()
        await release.wait()
        return Response(content='[{"key":"reply_language","value":"中文","evidence":"请始终使用中文回答"}]', model="fake")
    llm.achat = slow
    await agent.run("请始终使用中文回答")
    await started.wait()
    agent.session.add_message("user", "new branch tip")
    release.set()
    await agent.wait_memory_idle()
    assert not agent.memory_store._facts
    assert not agent._memory_distiller.successful
    started.clear()
    release.clear()
    await agent.run("请始终使用中文回答")
    await started.wait()
    await agent.wait_memory_idle()  # configured 0.1s timeout
    assert not agent.memory_store._facts
    assert not agent._memory_distiller.successful
    await agent.aclose()


@pytest.mark.anyio
async def test_new_prompt_reset_and_close_reap_inflight_extraction(tmp_path):
    agent, llm = make_agent(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def slow(messages, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    llm.achat = slow
    await agent.run("请始终使用中文回答")
    await started.wait()
    await agent.run("继续修改端口8080")
    assert cancelled.is_set()
    agent.reset()
    await agent.aclose()
    assert not agent._memory_distiller._tasks
    assert not agent.memory_store._facts


@pytest.mark.anyio
async def test_extraction_uses_original_user_evidence_and_redacts_before_send(tmp_path):
    agent, llm = make_agent(tmp_path)
    agent.memory_store.retrieve = Mock(return_value="RETRIEVED-ASSISTANT-CLAIM")
    agent.runtime_context.set("phase", "RUNTIME-SHOULD-NOT-BECOME-MEMORY")
    await agent.run("请始终使用中文回答; api_key=supersecret123")
    await agent.wait_memory_idle()
    payload = llm.extracts[-1][0].content
    assert "supersecret123" not in payload
    assert "[REDACTED]" in payload
    assert "RETRIEVED-ASSISTANT-CLAIM" not in payload
    assert "RUNTIME-SHOULD-NOT-BECOME-MEMORY" not in payload
    assert "assistant" in payload  # policy excludes assistant claims; no assistant transcript is supplied
    await agent.aclose()


@pytest.mark.anyio
async def test_default_disabled_and_memory_read_registered(tmp_path):
    agent = Agent(llm=MemoryLLM(), tools=[], session=Session(path=tmp_path / "s.jsonl"), memory_dir=tmp_path / "m")
    assert agent.registry.get("memory_read") is not None
    await agent.run("请始终使用中文回答")
    assert agent._memory_distiller.task is None
    assert agent.context_manager.before_compact is None
    await agent.aclose()


@pytest.mark.parametrize("text", ["hi", "hello", "done???", "继续", "谢谢！", "OK."])
def test_trivial_complete_match(text):
    from my_agent_core.memory_distillation import is_trivial_memory_query
    assert is_trivial_memory_query(text)


@pytest.mark.parametrize("text", ["hi, please change port 8080", "继续修改端口8080", "done? next update tests"])
def test_requests_are_not_mistaken_for_trivial(text):
    from my_agent_core.memory_distillation import is_trivial_memory_query
    assert not is_trivial_memory_query(text)


@pytest.mark.anyio
@pytest.mark.parametrize("payload", ['[{"key":"language","value":"English"}]',
                                     '[{"key":"language","value":"English","evidence":"not stated by user"}]'])
async def test_unsupported_nonempty_extraction_is_retryable(tmp_path, payload):
    agent, llm = make_agent(tmp_path)
    async def unsupported(messages, **kwargs):
        return Response(content=payload, model="fake")
    llm.achat = unsupported
    await agent.run("请始终使用中文回答")
    await agent.wait_memory_idle()
    assert not agent._memory_distiller.successful
    assert agent._distill_cursor == 0
    assert not agent.memory_store._facts
    await agent.aclose()


@pytest.mark.anyio
async def test_valid_empty_scan_advances_success(tmp_path):
    agent, llm = make_agent(tmp_path)
    async def empty(messages, **kwargs):
        return Response(content="[]", model="fake")
    llm.achat = empty
    await agent.run("请始终使用中文回答")
    await agent.wait_memory_idle()
    assert agent._memory_distiller.successful
    assert agent._distill_cursor == 1
    assert not agent.memory_store._facts
    await agent.aclose()
