"""Single-owner, cancellation and asynchronous deadline integration."""
import asyncio
import contextlib
import pytest
from my_agent_llm import Response
from my_agent_core.agent import Agent
from my_agent_core.session import Session
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.hooks import UserInputHook


class BlockingLLM:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.block = True
        self.calls = 0
    async def achat_stream(self, **kwargs):
        self.calls += 1
        try:
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            yield Response(content="done", model="fake")
        finally:
            self.closed.set()


def agent(tmp_path, llm, **kwargs):
    return Agent(llm=llm, tools=[], session=Session(path=tmp_path / "s.jsonl"), memory_dir=False, **kwargs)


@pytest.mark.anyio
async def test_concurrent_owner_is_rejected_and_abort_releases_for_next_run(tmp_path):
    llm = BlockingLLM()
    a = agent(tmp_path, llm)
    running = asyncio.create_task(a.run("one"))
    await llm.started.wait()
    first_id = a.active_run_id
    assert first_id
    with pytest.raises(RuntimeError, match="already running"):
        await a.run("two")
    a.abort()
    assert await running == "(cancelled)"
    assert llm.closed.is_set()
    assert a.active_run_id is None
    llm.block = False
    assert await a.run("three") == "done"
    await a.aclose()


@pytest.mark.anyio
async def test_deadline_interrupts_model_wait_and_reports_budget(tmp_path):
    llm = BlockingLLM()
    a = agent(tmp_path, llm, run_limits=RunLimits(max_seconds=0.03))
    events = [e async for e in a.prompt_stream("one")]
    assert events[-1].stop_reason == "budget_exhausted"
    assert events[-1].limit_details["kind"] == "max_seconds"
    assert events[-1].run_id
    assert llm.closed.is_set()
    assert a.active_run_id is None
    await a.aclose()


@pytest.mark.anyio
async def test_deadline_covers_input_hook_and_external_cancel_propagates(tmp_path):
    llm = BlockingLLM()
    a = agent(tmp_path, llm, run_limits=RunLimits(max_seconds=0.03))
    async def hook(event):
        await asyncio.Event().wait()
    a.hooks.register(UserInputHook, hook)
    events = [e async for e in a.prompt_stream("one")]
    assert events[-1].stop_reason == "budget_exhausted"
    assert not llm.started.is_set()
    a.hooks._hooks.clear()
    a.run_limits = RunLimits()
    running = asyncio.create_task(a.run("cancel"))
    await llm.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert a.active_run_id is None
    assert llm.closed.is_set()
    await a.aclose()


@pytest.mark.anyio
async def test_generator_close_releases_owner(tmp_path):
    llm = BlockingLLM()
    a = agent(tmp_path, llm)
    stream = a.prompt_stream("one")
    async with contextlib.aclosing(stream):
        await anext(stream)
        assert a.active_run_id
    assert a.active_run_id is None
    llm.block = False
    assert await a.run("next") == "done"
    await a.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["prepare", "summary", "tool", "tool_hook"])
async def test_deadline_interrupts_each_awaited_runtime_stage(tmp_path, stage, monkeypatch):
    from my_agent_core import run_lifecycle
    from my_agent_core.hooks import ToolCallHook
    from my_agent_core.tools import tool
    started, stopped = asyncio.Event(), asyncio.Event()
    now = [0.0]

    class StageBudget(run_lifecycle.RunBudget):
        def __init__(self, limits):
            super().__init__(limits, clock=lambda: now[0])

    async def wait_for_stage_deadline(tasks, *, timeout):
        # A virtual timer expires only after the selected awaited stage starts.
        # Earlier events/setup may take arbitrary wall time without stealing the
        # deadline this test specifically needs to exercise at that stage.
        tasks = set(tasks)
        entered = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait(tasks | {entered}, return_when=asyncio.FIRST_COMPLETED)
            if entered not in done:
                return done & tasks, tasks - done
            assert timeout is not None
            now[0] += timeout
            return await asyncio.wait(tasks, timeout=0)
        finally:
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)

    class StageAsyncio:
        wait = staticmethod(wait_for_stage_deadline)

        def __getattr__(self, name):
            return getattr(asyncio, name)

    monkeypatch.setattr(run_lifecycle, "RunBudget", StageBudget)
    monkeypatch.setattr(run_lifecycle, "asyncio", StageAsyncio())

    async def block():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    class LLM(BlockingLLM):
        async def achat(self, **kwargs):
            await block()
        async def achat_stream(self, **kwargs):
            if stage in {"tool", "tool_hook"}:
                yield Response(content="", model="fake", tool_calls=[{"id":"wait", "type":"function", "function":{"name":"waiter", "arguments":"{}"}}])
            else:
                yield Response(content="done", model="fake")
    @tool(name="waiter", description="await cancellation")
    async def waiter():
        await block()
        return "unreachable"
    llm = LLM()
    a = Agent(llm=llm, tools=[waiter], session=Session(path=tmp_path / "s.jsonl"), memory_dir=False,
              run_limits=RunLimits(max_seconds=0.08), context_budget=100000)
    async def slow_input(event):
        # Deterministic reproduction: the old 80ms wall timer expires here,
        # before prepare/summary/tool/hook has any chance to enter block().
        await asyncio.sleep(0.12)
    a.hooks.register(UserInputHook, slow_input)
    if stage == "prepare":
        async def prepare(messages):
            await block()
        a.context_manager.prepare = prepare
    elif stage == "summary":
        for i in range(10):
            a.session.add_message("user", str(i) + "x" * 500)
            a.session.add_message("assistant", "answer" * 80)
        a.context_manager.set_budget(100)
        a.context_manager.keep_recent_tokens = 20
    elif stage == "tool_hook":
        async def hook(event):
            await block()
        a.hooks.register(ToolCallHook, hook)
    async def collect():
        return [event async for event in a.prompt_stream("execute")]
    try:
        # Fail fast if setup never reaches the selected stage. This watchdog is
        # separate from the unchanged 80ms virtual deadline under test.
        events = await asyncio.wait_for(collect(), timeout=5)
        assert started.is_set()
        assert stopped.is_set()
        assert events[-1].stop_reason == "budget_exhausted"
        assert events[-1].limit_details["kind"] == "max_seconds"
        assert events[-1].limit_details["observed"] >= events[-1].limit_details["limit"] == 0.08
        assert a.active_run_id is None
    finally:
        await a.aclose()


@pytest.mark.anyio
async def test_aclose_cancels_run_without_awaiting_unrelated_owner_work(tmp_path):
    llm = BlockingLLM()
    a = agent(tmp_path, llm)
    finished_run = asyncio.Event()
    unrelated = asyncio.Event()
    async def caller():
        assert await a.run("one") == "(cancelled)"
        finished_run.set()
        await unrelated.wait()
    task = asyncio.create_task(caller())
    await llm.started.wait()
    await asyncio.wait_for(a.aclose(), 0.5)
    assert finished_run.is_set()
    assert not task.done()
    unrelated.set()
    await task


@pytest.mark.anyio
async def test_generator_close_after_model_partial_closes_source_immediately(tmp_path):
    from my_agent_core.events import MessageUpdate
    from my_agent_llm import StreamChunk
    closed = asyncio.Event()
    class Partial:
        async def achat_stream(self, **kwargs):
            try:
                yield StreamChunk(content="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()
    a = agent(tmp_path, Partial())
    async with contextlib.aclosing(a.prompt_stream("one")) as events:
        async for event in events:
            if isinstance(event, MessageUpdate):
                break
    assert closed.is_set()
    assert a.active_run_id is None
    await a.aclose()


@pytest.mark.anyio
async def test_runtime_scopes_main_summary_and_idle_memory_to_same_root(tmp_path):
    from my_agent_llm.observation import CallObservation, InMemoryObserver, CallStart
    recorder = InMemoryObserver()
    class Observed:
        async def achat_stream(self, **kwargs):
            call = CallObservation(recorder, provider="fake", model="fake", method="achat_stream")
            try:
                yield Response(content="done", model="fake")
            finally:
                call.finish()
        async def achat(self, messages, **kwargs):
            call = CallObservation(recorder, provider="fake", model="fake", method="achat")
            try:
                if "Extract only durable" in messages[0].content:
                    return Response(content="[]", model="fake")
                return Response(content="summary", model="fake")
            finally:
                call.finish()
    a = Agent(llm=Observed(), tools=[], session=Session(path=tmp_path / "s.jsonl"),
              memory_dir=tmp_path / "m", memory_autodistill=True, memory_idle_seconds=0,
              context_budget=100000, keep_recent_tokens=2, system_prompt="SYS")
    await a.run("Please always answer in Chinese")
    await a.wait_memory_idle()
    first = [r for r in recorder.records if type(r) is CallStart]
    assert {r.purpose for r in first} == {"agent", "memory_distill"}
    assert len({r.root_id for r in first}) == 1
    a.context_manager.set_budget(10)
    a.context_manager.keep_recent_tokens = 1
    await a.run("another long user input " * 20)
    await a.wait_memory_idle()
    starts = [r for r in recorder.records if type(r) is CallStart]
    second = starts[len(first):]
    assert {r.purpose for r in second} >= {"agent", "summary", "memory_distill"}
    assert len({r.root_id for r in second}) == 1
    assert second[0].root_id != first[0].root_id
    await a.aclose()


@pytest.mark.anyio
async def test_agent_close_while_consumer_is_paused_does_not_hang(tmp_path):
    a = agent(tmp_path, BlockingLLM())
    stream = a.prompt_stream("one")
    await anext(stream)
    assert a.active_run_id
    await asyncio.wait_for(a.aclose(), 0.5)
    assert a.active_run_id is None
    await stream.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["abort", "deadline", "close"])
async def test_interrupted_observed_tool_intent_survives_session_reload(tmp_path, stop):
    """A delivered tool intent needs one result even before StreamDone arrives."""
    from my_agent_core.events import MessageUpdate
    from my_agent_llm import ToolCall
    from my_agent_llm.events import StreamStartEvent, ToolCallDoneEvent

    closed = asyncio.Event()
    call = ToolCall(id="observed", name="never_started", args={})
    partial = Response(content="", model="fake", tool_calls=[call]).to_message()

    class PartialTool:
        async def astream_events(self, **kwargs):
            try:
                yield StreamStartEvent(partial=partial)
                yield ToolCallDoneEvent(index=0, tool_call=call, partial=partial)
                await asyncio.Event().wait()
            finally:
                closed.set()

    a = agent(tmp_path, PartialTool(), run_limits=RunLimits(max_seconds=0.08) if stop == "deadline" else None)
    stream = a.prompt_stream("one")
    async for event in stream:
        if isinstance(event, MessageUpdate):
            if stop == "abort":
                a.abort()
            elif stop == "close":
                await stream.aclose()
                break
    assert closed.is_set()
    assert a.active_run_id is None
    restored = Session.load(tmp_path / "s.jsonl")
    history = restored.get_full_history_messages()
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[1].metadata["tool_calls"][0]["id"] == "observed"
    assert history[2].metadata["tool_call_id"] == "observed"
    assert history[2].metadata["is_error"] is True
    await a.aclose()
