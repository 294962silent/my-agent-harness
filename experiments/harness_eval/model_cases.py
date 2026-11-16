"""LLM facade observation and lifecycle contracts using injected offline providers."""

from __future__ import annotations

import asyncio

from my_agent_llm import Config, LLM, Message, Response, StreamChunk
from my_agent_llm.observation import CallEnd, CallStart, InMemoryObserver, observation_context


def client(provider, recorder=None):
    llm = LLM(Config(provider="openai", api_key="offline-fixture", model="offline-fixture"), observer=recorder)
    # Same instance-level fake-provider seam used by facade unit tests. No registry
    # mutation, network transport, environment credentials or external SDK request.
    llm._provider = provider
    return llm


async def observation(arm, path):
    recorder, calls = InMemoryObserver(), []
    body = "SYNTHETIC_PRIVATE_BODY_DO_NOT_EXPORT"

    class Leaf:
        async def achat(self, messages, **kwargs):
            calls.append("auxiliary")
            return Response(model="offline-fixture", content=body)

    auxiliary = client(Leaf(), recorder if arm == "on" else None)

    class Parent:
        async def achat(self, messages, **kwargs):
            calls.append("primary")
            with observation_context(purpose="memory_extract"):
                await auxiliary.achat([Message(role="user", content=body)])
            return Response(model="offline-fixture", content=body)

    llm = client(Parent(), recorder if arm == "on" else None)
    with observation_context(root_id="offline-root", purpose="agent"):
        response = await llm.achat([Message(role="user", content=body)])
    starts = [r for r in recorder.records if type(r) is CallStart]
    ends = [r for r in recorder.records if isinstance(r, CallEnd)]
    parent = next((r for r in starts if r.purpose == "agent"), None)
    child = next((r for r in starts if r.purpose == "memory_extract"), None)
    linked = bool(parent and child and child.parent_id == parent.call_id and child.root_id == parent.root_id)
    serialized = repr(recorder.records)
    return (
        dict(
            actual_calls=len(calls),
            observed_calls=len(starts),
            coverage=len(starts) / len(calls),
            matched_ends=sum(e.call_id in {s.call_id for s in starts} for e in ends),
            privacy_violations=int(body in serialized),
            linked_auxiliary=int(linked),
        ),
        {
            "same_model_response": response.content == body,
            "expected_coverage": len(starts) == (len(calls) if arm == "on" else 0),
            "all_calls_closed": len(starts) == len(ends),
            "privacy": body not in serialized,
            "auxiliary_parent_when_enabled": linked if arm == "on" else True,
        },
        dict(
            calls=calls,
            observed_methods=[r.method for r in ends],
            statuses=[r.status for r in ends],
            purposes=[r.purpose for r in starts],
            usage_available=any(r.usage is not None for r in ends),
        ),
    )


async def stream_close(arm, path):
    state = {"closed": 0}
    recorder = InMemoryObserver()

    class Provider:
        async def achat_stream(self, messages, **kwargs):
            try:
                yield StreamChunk(content="first")
                yield StreamChunk(content="second")
            finally:
                state["closed"] += 1

    llm = client(Provider(), recorder)
    stream = llm.achat_stream([Message(role="user", content="fixture")])
    first = await anext(stream)
    await stream.aclose()
    ends = [r for r in recorder.records if isinstance(r, CallEnd)]
    return (
        dict(provider_finalized=state["closed"], terminal_records=len(ends)),
        {
            "first_chunk_received": first.content == "first",
            "provider_finalized": state["closed"] == 1,
            "one_terminal_record": len(ends) == 1,
            "status_closed": len(ends) == 1 and ends[0].status == "closed",
        },
        dict(statuses=[r.status for r in ends]),
    )


async def stream_cancel(arm, path):
    entered, closed = asyncio.Event(), asyncio.Event()
    recorder = InMemoryObserver()

    class Provider:
        async def achat_stream(self, messages, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield StreamChunk(content="unreachable")
            finally:
                closed.set()

    llm = client(Provider(), recorder)

    async def consume():
        async for _ in llm.achat_stream([Message(role="user", content="fixture")]):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    propagated = False
    try:
        await task
    except asyncio.CancelledError:
        propagated = True
    ends = [r for r in recorder.records if isinstance(r, CallEnd)]
    return (
        dict(provider_finalized=int(closed.is_set()), cancellation_propagated=int(propagated)),
        {
            "cancel_propagates": propagated,
            "provider_finalized": closed.is_set(),
            "status_cancelled": len(ends) == 1 and ends[0].status == "cancelled",
        },
        dict(statuses=[r.status for r in ends]),
    )


async def owner(arm, path):
    from my_agent_core.agent import Agent
    from my_agent_core.session import Session

    entered = asyncio.Event()

    class BlockingModel:
        async def achat_stream(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield Response(model="offline-fixture", content="unreachable")

    agent = Agent(llm=BlockingModel(), tools=[], session=Session(path=path / "s.jsonl"), memory_dir=False)
    task = asyncio.create_task(agent.run("first"))
    await asyncio.wait_for(entered.wait(), 2)
    rejected = False
    try:
        try:
            await asyncio.wait_for(agent.run("second"), 1)
        except RuntimeError as exc:
            rejected = "already running" in str(exc)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await agent.aclose()
    return (
        dict(concurrent_prompt_rejected=int(rejected), owner_released=int(agent.active_run_id is None)),
        {
            "second_prompt_rejected": rejected,
            "owner_released": agent.active_run_id is None,
        },
        dict(owner_active_after_cleanup=agent.active_run_id is not None),
    )


def model_cases():
    from .scenarios import Case

    return [
        Case(
            "observation",
            "auxiliary_coverage",
            ("on", "off"),
            "ablation",
            observation,
            ["src/my_agent_llm/client.py:LLM.achat", "src/my_agent_llm/observation.py:observation_context"],
            "observer callback enabled versus None, same nested facade calls; provider usage unavailable",
        ),
        Case(
            "model",
            "stream_close",
            ("contract",),
            "contract",
            stream_close,
            ["src/my_agent_llm/client.py:LLM.achat_stream"],
            "explicit stream.aclose after first chunk; no fake feature-off claim",
        ),
        Case(
            "model",
            "stream_cancel",
            ("contract",),
            "contract",
            stream_cancel,
            ["src/my_agent_llm/client.py:LLM.achat_stream"],
            "cancel consuming task while provider awaits; check finalization and propagation",
        ),
        Case(
            "runtime",
            "single_owner",
            ("contract",),
            "contract",
            owner,
            ["src/my_agent_core/agent.py:Agent.prompt_stream", "src/my_agent_core/run_lifecycle.py"],
            "two concurrent prompts on one Agent; ownership safety has no supported off switch",
        ),
    ]
