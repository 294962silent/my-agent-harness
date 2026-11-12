"""Optional pre-compress callback uses canonical coverage and fails open."""
import asyncio

import pytest
from my_agent_llm import Message, Response
from my_agent_core.context import ContextManager


class SummaryLLM:
    async def achat(self, **kwargs):
        return Response(content="summary", model="fake")


@pytest.mark.anyio
async def test_precompress_canonical_coverage_retry_and_cancellation():
    calls = []
    async def callback(messages, identity):
        calls.append((messages, identity))
        raise RuntimeError("best effort")
    ctx = ContextManager(llm=SummaryLLM(), before_compact=callback, keep_recent_tokens=20000)
    messages = [Message(role="system", content="host"), Message(role="user", content="first durable preference")]
    assert (await ctx.force_compact(messages))[1].content.endswith("summary")
    assert calls[0][0][0].content == "first durable preference"
    assert calls[0][1]["covered_message_keys"]
    await ctx.force_compact(messages)
    assert len(calls) == 2
    async def cancel(messages, identity):
        raise asyncio.CancelledError()
    ctx.before_compact = cancel
    with pytest.raises(asyncio.CancelledError):
        await ctx.force_compact(messages)


@pytest.mark.anyio
async def test_agent_precompress_covers_start_redacts_and_deduplicates(tmp_path):
    from my_agent_core.agent import Agent
    from my_agent_core.session import Session
    from my_agent_core.memory_distillation import SOURCE_TEXT

    class LLM:
        def __init__(self):
            self.extractions = []
        async def achat(self, messages, **kwargs):
            if "Extract only durable" in messages[0].content:
                self.extractions.append(messages[0].content)
                return Response(content='[{"key":"language","value":"中文","evidence":"始终使用中文回答"}]', model="fake")
            return Response(content="summary", model="fake")
    llm = LLM()
    session = Session(path=tmp_path / "s.jsonl")
    for i in range(40):
        text = ("始终使用中文回答; token=secret123" if i == 0 else f"long evidence {i}: " + "x" * 1500)
        session.add_message("user", text, **{SOURCE_TEXT: text})
        session.add_message("assistant", "UNSUPPORTED-ASSISTANT-CLAIM")
    agent = Agent(llm=llm, tools=[], session=session, memory_dir=tmp_path / "m", memory_autodistill=True,
                  keep_recent_tokens=100000)
    await agent.compact()
    assert len(llm.extractions) == 1
    sent = llm.extractions[0]
    assert "始终使用中文回答" in sent  # earliest canonical coverage, not merely last 12 messages
    assert "secret123" not in sent
    assert "UNSUPPORTED-ASSISTANT-CLAIM" not in sent
    assert "omitted" in sent
    assert "long evidence 39" in sent  # bounded first/last message sampling
    assert agent.memory_store.retrieve("language")
    await agent.compact()
    assert len(llm.extractions) == 1
    await agent.aclose()


def test_sampling_has_hard_bounds_and_excludes_unattributed_messages():
    from my_agent_core.memory_distillation import bounded_evidence, SOURCE_TEXT, MAX_TOTAL_CHARS, MAX_TEXT_CHARS
    messages = [Message(role="user", content="unattributed legacy fact")]
    messages += [Message(role="user", content="irrelevant rendered text", metadata={SOURCE_TEXT: f"user {i} " + "x" * 5000}) for i in range(80)]
    evidence = bounded_evidence(messages)
    assert len(evidence) == 24
    assert "user 0 " in evidence[0][1]
    assert "user 79 " in evidence[-1][1]
    assert all(len(text) <= MAX_TEXT_CHARS for _, text in evidence)
    assert sum(len(text) for _, text in evidence) <= MAX_TOTAL_CHARS


@pytest.mark.anyio
async def test_precompress_timeout_is_best_effort():
    async def callback(messages, identity):
        await asyncio.Event().wait()
    ctx = ContextManager(llm=SummaryLLM(), before_compact=callback, before_compact_timeout=0.01)
    view = await ctx.force_compact([Message(role="user", content="input")])
    assert view[0].content.endswith("summary")
