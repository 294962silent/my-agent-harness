"""Observer subscriptions and runtime ownership; all clients/providers are offline."""

import asyncio
import builtins
from threading import Event

import pytest

from my_agent_llm import Config, LLM
from my_agent_llm.events import StreamDoneEvent
from my_agent_llm.models import Message, Response, StreamChunk
from my_agent_llm.observation import CallEnd, InMemoryObserver, observation_context


def offline_llm(observer=None):
    llm = LLM(Config(provider="openai", api_key="offline", model="fake"), observer=observer)

    class Provider:
        def chat(self, *args, **kwargs):
            return Response(content="PRIVATE", model="fake")

        def stream(self, *args, **kwargs):
            yield StreamChunk(content="PRIVATE")

        async def achat(self, *args, **kwargs):
            return self.chat()

        async def achat_stream(self, *args, **kwargs):
            yield StreamChunk(content="PRIVATE")

        async def astream_events(self, *args, **kwargs):
            yield StreamDoneEvent(message=Message(role="assistant", content="PRIVATE"))

    llm._provider = Provider()
    return llm


def test_subscriptions_preserve_base_and_snapshot_stream_lifetime():
    base, added, later = InMemoryObserver(), InMemoryObserver(), InMemoryObserver()
    llm = offline_llm(base)
    subscription = llm.add_observer(added)
    stream = llm.stream([])
    next(stream)
    subscription.close()
    subscription.close()
    llm.add_observer(later)
    stream.close()
    assert llm.observer is base
    assert [r.status for r in added.records if isinstance(r, CallEnd)] == ["closed"]
    assert not later.records  # subscriptions are captured when this call starts
    llm.chat([])
    assert len(added.records) == 2 and len(later.records) == 2 and len(base.records) == 4


@pytest.mark.anyio
async def test_all_endpoints_and_auxiliary_purpose_isolate_each_observer_failure():
    base, added = InMemoryObserver(), InMemoryObserver()
    llm = offline_llm(base)
    llm.add_observer(lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE")))
    subscription = llm.add_observer(added)
    with observation_context(root_id="root", purpose="memory_distill"):
        llm.chat([])
        list(llm.stream([]))
        await llm.achat([])
        async for _ in llm.achat_stream([]):
            pass
        async for _ in llm.astream_events([]):
            pass
    ends = [r for r in added.records if isinstance(r, CallEnd)]
    assert {r.method for r in ends} == {"chat", "stream", "achat", "achat_stream", "astream_events"}
    assert all(r.root_id == "root" and r.purpose == "memory_distill" for r in ends)
    assert len(base.records) == len(added.records) == 10
    subscription.close()
    llm.chat([])
    assert len(added.records) == 10


@pytest.mark.anyio
async def test_subscription_keeps_async_cancel_terminal_record():
    added = InMemoryObserver()
    llm = offline_llm()
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    llm._provider.achat = blocked
    subscription = llm.add_observer(added)
    task = asyncio.create_task(llm.achat([]))
    await entered.wait()
    subscription.close()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert added.records[-1].status == "cancelled"


def test_disabled_runtime_does_not_import_sdk_or_construct_sink(monkeypatch):
    from my_agent_llm.observation_runtime import create_observation_runtime

    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "langfuse"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert create_observation_runtime() is None
    with pytest.raises(ValueError, match="keys"):
        create_observation_runtime(enabled=True)


def test_factory_runtime_owns_only_its_sink_and_detaches_from_shared_llm(monkeypatch):
    from my_agent_llm.langfuse_export import LangfuseSink
    from my_agent_llm.observation_runtime import create_observation_runtime

    events, constructed = [], []

    class Span:
        id = "0" * 16

        def update(self, **kwargs):
            events.append(kwargs)

        def end(self):
            pass

    class Client:
        shutdown_calls = 0

        def start_observation(self, **kwargs):
            events.append(kwargs)
            return Span()

        def flush(self):
            pass

        def shutdown(self):
            self.shutdown_calls += 1

    client = Client()

    def factory(**kwargs):
        constructed.append(kwargs)
        return LangfuseSink(client, own_client=True)

    monkeypatch.setattr(LangfuseSink, "from_credentials", factory)
    runtime = create_observation_runtime(enabled=True, public_key="pk", secret_key="sk", network_timeout=2)
    base = InMemoryObserver()
    llm = offline_llm(base)
    subscription = runtime.attach(llm)
    assert runtime.attach(llm) is subscription
    llm.chat([])
    assert runtime.close(timeout=2)
    assert runtime.close(timeout=2)
    assert client.shutdown_calls == 1 and not runtime.is_alive
    assert not runtime.export_errors and not runtime.close_errors
    before = len(events)
    llm.chat([])
    assert len(events) == before and len(base.records) == 4
    assert runtime.dropped == 0 and llm.observer is base
    assert constructed[0]["timeout"] == 2
    assert "PRIVATE" not in repr(events) and "sk" not in repr(events)
    with pytest.raises(RuntimeError, match="closed"):
        runtime.attach(llm)


def test_runtime_timeout_remains_observable_and_later_close_finishes():
    from my_agent_llm.observation_export import BoundedExporter
    from my_agent_llm.observation_runtime import ObservationRuntime

    entered, release = Event(), Event()

    def slow(_):
        entered.set()
        release.wait(3)

    runtime = ObservationRuntime(BoundedExporter(slow))
    llm = offline_llm()
    runtime.attach(llm)
    llm.chat([])
    assert entered.wait(1)
    try:
        assert not runtime.close(timeout=0.01)
        assert runtime.is_alive
        llm.chat([])  # detached: closed exporter must not remain subscribed
        assert runtime.dropped == 0
    finally:
        release.set()
    assert runtime.close(timeout=2) and not runtime.is_alive
