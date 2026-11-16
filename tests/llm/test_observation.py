"""Offline observation contracts; no provider network calls."""
import asyncio
from dataclasses import FrozenInstanceError

import pytest

from my_agent_llm import Config, LLM
from my_agent_llm.events import StreamDoneEvent, StreamErrorEvent
from my_agent_llm.models import Message, Response, StreamChunk
from my_agent_llm.observation import CallEnd, CallStart, InMemoryObserver, observation_context


def client(observer, provider):
    llm = LLM(Config(provider="openai", api_key="offline", model="fake"), observer=observer)
    llm._provider = provider
    return llm


def test_sync_context_usage_snapshot_and_observer_failure():
    recorder = InMemoryObserver()
    usage = {"prompt_tokens": 4, "cache_read_tokens": 3}
    class Provider:
        def chat(self, *args, **kwargs):
            assert "purpose" not in kwargs and "root_id" not in kwargs
            return Response(content="private body", model="fake", usage=usage)
    llm = client(recorder, Provider())
    with observation_context(root_id="run-1", purpose="summary"):
        response = llm.chat([Message(role="user", content="secret input")])
    start, end = recorder.records
    assert isinstance(start, CallStart) and isinstance(end, CallEnd)
    assert (end.root_id, end.purpose, end.parent_id, end.status) == ("run-1", "summary", None, "ok")
    assert end.call_id == start.call_id and end.duration_seconds >= 0
    response.usage["prompt_tokens"] = 999
    assert end.usage == usage
    with pytest.raises(TypeError):
        end.usage["prompt_tokens"] = 9
    with pytest.raises(FrozenInstanceError):
        end.status = "error"
    assert "private body" not in repr(recorder.records) and "secret input" not in repr(recorder.records)
    llm.observer = lambda record: (_ for _ in ()).throw(RuntimeError("observer only"))
    assert llm.chat([]).content == "private body"


def test_sync_stream_lazy_close_final_usage_and_error():
    recorder = InMemoryObserver()
    closed = []
    class Provider:
        def stream(self, *args, **kwargs):
            try:
                yield StreamChunk(content="x", usage={"prompt_tokens": 2})
                yield StreamChunk(content="", usage={"prompt_tokens": 7}, finish_reason="stop")
            finally:
                closed.append(True)
    llm = client(recorder, Provider())
    stream = llm.stream([])
    assert not recorder.records
    next(stream)
    stream.close()
    assert recorder.records[-1].status == "closed" and closed == [True]
    list(llm.stream([]))
    assert recorder.records[-1].status == "ok"
    assert recorder.records[-1].usage == {"prompt_tokens": 7}  # cumulative, not 2 + 7
    class Broken:
        def chat(self, *args, **kwargs):
            raise ValueError("private exception")
    llm._provider = Broken()
    with pytest.raises(ValueError, match="private exception"):
        llm.chat([])
    assert recorder.records[-1].error_type == "ValueError"
    assert "private exception" not in repr(recorder.records)


@pytest.mark.anyio
async def test_async_stream_cancel_and_events_error_cleanup():
    recorder = InMemoryObserver()
    entered, closed = asyncio.Event(), []
    class Provider:
        async def achat(self, *args, **kwargs):
            return Response(content="ok", model="fake")
        async def achat_stream(self, *args, **kwargs):
            try:
                yield StreamChunk(content="first")
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        async def astream_events(self, *args, **kwargs):
            try:
                yield StreamErrorEvent(error=Message(role="assistant", content="private error"))
            finally:
                closed.append(True)
    llm = client(recorder, Provider())
    await llm.achat([])
    assert recorder.records[-1].usage is None
    async def consume():
        async for _ in llm.achat_stream([]):
            pass
    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.records[-1].status == "cancelled" and closed == [True]
    events = llm.astream_events([])
    await anext(events)
    await events.aclose()
    assert recorder.records[-1].status == "error" and closed == [True, True]
    assert len([r for r in recorder.records if isinstance(r, CallEnd)]) == 3


@pytest.mark.anyio
async def test_nested_parent_and_concurrent_contexts_do_not_leak():
    recorder = InMemoryObserver()
    class Leaf:
        async def achat(self, *args, **kwargs):
            await asyncio.sleep(0)
            return Response(content="ok", model="fake")
    leaf = client(recorder, Leaf())
    class Outer:
        async def achat(self, *args, **kwargs):
            with observation_context(purpose="memory_distill"):
                return await leaf.achat([])
    outer = client(recorder, Outer())
    async def one(root):
        with observation_context(root_id=root, purpose="agent"):
            await outer.achat([])
    await asyncio.gather(one("a"), one("b"))
    starts = [r for r in recorder.records if isinstance(r, CallStart)]
    for root in ("a", "b"):
        parent = next(r for r in starts if r.root_id == root and r.purpose == "agent")
        child = next(r for r in starts if r.root_id == root and r.purpose == "memory_distill")
        assert child.parent_id == parent.call_id
    await leaf.achat([])
    assert recorder.records[-1].root_id not in ("a", "b")


@pytest.mark.anyio
async def test_all_stream_endpoints_terminal_status_usage_and_no_context_leak():
    recorder = InMemoryObserver()
    closed = []
    class Provider:
        def chat(self, *args, **kwargs):
            return Response(content="", model="fake", finish_reason="error")
        async def achat_stream(self, *args, **kwargs):
            try:
                yield StreamChunk(content="", usage={"completion_tokens": 4}, finish_reason="cancelled")
            finally:
                closed.append("chunks")
        async def astream_events(self, *args, **kwargs):
            try:
                yield StreamDoneEvent(message=Message(role="assistant", content="body"), usage={"output_tokens": 7})
            finally:
                closed.append("events")
    llm = client(recorder, Provider())
    llm.chat([])
    assert recorder.records[-1].status == "error"
    stream = llm.achat_stream([])
    await anext(stream)
    # A suspended stream must not leave its call context in the consumer task.
    llm.chat([])
    assert recorder.records[-1].parent_id is None
    await stream.aclose()
    assert recorder.records[-1].status == "cancelled"
    assert recorder.records[-1].usage == {"completion_tokens": 4}
    async for _ in llm.astream_events([]):
        pass
    assert recorder.records[-1].usage == {"output_tokens": 7}
    assert recorder.records[-1].status == "ok" and closed == ["chunks", "events"]
