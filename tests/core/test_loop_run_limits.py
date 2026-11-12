"""Budget stops extra main turns without changing retry accounting."""
import pytest
from my_agent_core.agent import Agent
from my_agent_core.session import Session
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.tools import tool
from my_agent_llm import Response


class Endless:
    def __init__(self):
        self.calls = 0
    async def achat_stream(self, **kwargs):
        self.calls += 1
        yield Response(content="", model="fake", tool_calls=[{"id":str(self.calls), "type":"function", "function":{"name":"again", "arguments":"{}"}}])


@pytest.mark.anyio
async def test_max_turns_prevents_extra_model_and_tools(tmp_path):
    executed = []
    @tool(name="again", description="repeat")
    def again():
        executed.append(1)
        return "ok"
    llm = Endless()
    a = Agent(llm=llm, tools=[again], session=Session(path=tmp_path / "s.jsonl"), memory_dir=False,
              run_limits=RunLimits(max_turns=2))
    events = [e async for e in a.prompt_stream("go")]
    assert llm.calls == 2
    assert len(executed) == 2
    assert events[-1].stop_reason == "budget_exhausted"
    assert events[-1].iterations == 2
    assert events[-1].limit_details["kind"] == "max_turns"
    await a.aclose()


@pytest.mark.anyio
async def test_retry_is_not_an_extra_main_turn(tmp_path):
    from my_agent_core.retry import AutoRetryPolicy
    from my_agent_core.events import TurnStart
    class Retry:
        def __init__(self):
            self.calls = 0
        async def achat_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Error code 503: Service Unavailable")
            yield Response(content="recovered", model="fake")
    llm = Retry()
    a = Agent(llm=llm, tools=[], session=Session(path=tmp_path / "s.jsonl"), memory_dir=False,
              run_limits=RunLimits(max_turns=1),
              retry_policy=AutoRetryPolicy(max_retries=1, base_delay_ms=1, max_delay_ms=1, jitter=0))
    events = [event async for event in a.prompt_stream("go")]
    assert llm.calls == 2
    assert sum(isinstance(event, TurnStart) for event in events) == 1
    assert events[-1].stop_reason == "end_turn"
    await a.aclose()
