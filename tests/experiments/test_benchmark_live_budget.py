"""Fail-closed live budget and task evidence contracts, no model calls."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments.benchmark_live.budget import BudgetLedger, BudgetStop, GuardedLLM
from experiments.benchmark_live.tasks import agent_instruction, oracle_gate
from my_agent_llm import Config, LLM
from my_agent_llm.models import Response


def test_ledger_lock_budget_reserve_unknown_and_no_zero(tmp_path):
    path = tmp_path / "ledger.json"
    BudgetLedger(path, limit_usd="0.01")

    def reserve(i):
        try:
            return BudgetLedger(path, limit_usd="0.01").reserve(
                str(i), input_bound=1000, max_output=4096, purpose="agent"
            )
        except BudgetStop:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(reserve, range(4)))
    accepted = [x for x in reservations if x]
    assert len(accepted) == 1
    ledger = BudgetLedger(path, limit_usd="0.01")
    ledger.settle(accepted[0], usage=None, status="error")
    row = ledger.snapshot()["requests"][0]
    assert row["usage"] is None and row["cost_micro_usd"] is None
    assert row["held_micro_usd"] > 0
    with pytest.raises(BudgetStop):
        ledger.reserve(row["run_id"], input_bound=1, max_output=1, purpose="summary")


def test_exact_usage_full_price_cache_and_limits(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json")
    for _ in range(16):
        reservation = ledger.reserve("run", input_bound=1000, max_output=4096, purpose="memory_distill")
        ledger.settle(
            reservation, usage={"prompt_tokens": 10, "cache_read_tokens": 90, "completion_tokens": 100}, status="ok"
        )
    row = ledger.snapshot()["requests"][0]
    assert row["cost_micro_usd"] == 150  # 100 input at .30/M + 100 output at 1.20/M
    with pytest.raises(BudgetStop):
        ledger.reserve("run", input_bound=10, max_output=4096, purpose="agent")
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "ledger.json", limit_usd="31")


@pytest.mark.anyio
async def test_guard_covers_auxiliary_calls_and_stops_after_missing_usage(tmp_path):
    from my_agent_llm.observation import observation_context

    ledger = BudgetLedger(tmp_path / "ledger.json")

    class Provider:
        def __init__(self):
            self.calls = 0

        async def achat(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["max_tokens"] == 4096
            return Response(content="body", model="fake")

    llm = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    provider = Provider()
    llm._provider = provider
    guarded = GuardedLLM(llm, ledger, "run")
    with observation_context(root_id="run", purpose="summary"):
        with pytest.raises(BudgetStop):
            await guarded.achat([])
    with pytest.raises(BudgetStop):
        await guarded.achat([])
    assert provider.calls == 1
    assert ledger.snapshot()["requests"][0]["purpose"] == "summary"


def test_agent_only_gets_instruction_not_gold(tmp_path):
    (tmp_path / "instruction.md").write_text("Repair visible behavior", encoding="utf-8")
    (tmp_path / "task.toml").write_text('version="1.0"', encoding="utf-8")
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution/solve.sh").write_text("SECRET GOLD", encoding="utf-8")
    assert agent_instruction(tmp_path) == "Repair visible behavior"
    bad = tmp_path / "result.json"
    bad.write_text(json.dumps({"task_name": tmp_path.name, "verifier_result": {"rewards": {"reward": 0}}}))
    with pytest.raises(ValueError):
        oracle_gate(tmp_path, bad)
    success = {
        "task_name": tmp_path.name,
        "verifier_result": {"rewards": {"reward": 1}},
        "exception_info": None,
        "agent_info": {"name": "custom-agent"},
        "task_checksum": "abc",
        "finished_at": "2026-11-12T00:00:00Z",
    }
    bad.write_text(json.dumps(success))
    with pytest.raises(ValueError):
        oracle_gate(tmp_path, bad)
    success["agent_info"]["name"] = "oracle"
    bad.write_text(json.dumps(success))
    gate = oracle_gate(tmp_path, bad)
    assert gate["status"] == "oracle_passed" and "SECRET GOLD" not in json.dumps(gate)


@pytest.mark.anyio
async def test_guard_all_five_entries_reserve_and_settle(tmp_path):
    from my_agent_llm.observation import observation_context

    usage = {"prompt_tokens": 11, "completion_tokens": 7}

    class Provider:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return Response(content="ok", model="fake", usage=usage)

        async def achat(self, messages, **kwargs):
            return self.chat(messages, **kwargs)

    llm = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    llm._provider = provider = Provider()
    ledger = BudgetLedger(tmp_path / "ledger.json")
    guard = GuardedLLM(llm, ledger, "run")
    guard.chat([])
    list(guard.stream([]))
    await guard.achat([])
    [chunk async for chunk in guard.achat_stream([])]
    [event async for event in guard.astream_events([])]
    with observation_context(purpose="memory_extract"):
        await guard.achat([])
    rows = ledger.snapshot()["requests"]
    assert provider.calls == len(rows) == 6
    assert all(r["status"] == "ok" and r["usage"] == usage for r in rows)
    assert rows[-1]["purpose"] == "memory_extract"
    assert all(r["input_token_bound"] >= 4096 for r in rows)


@pytest.mark.anyio
async def test_guard_provider_error_does_not_leak_credentials(tmp_path):
    class Provider:
        async def achat(self, *args, **kwargs):
            raise RuntimeError("SYNTHETIC_SECRET_KEY")

    llm = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    llm._provider = Provider()
    ledger = BudgetLedger(tmp_path / "ledger.json")
    with pytest.raises(BudgetStop) as raised:
        await GuardedLLM(llm, ledger, "run").achat([])
    assert "SYNTHETIC_SECRET_KEY" not in str(raised.value)
    assert "SYNTHETIC_SECRET_KEY" not in json.dumps(ledger.snapshot())


def test_parser_retains_environment_failure_and_unavailable_usage(tmp_path):
    from experiments.benchmark_live.tasks import parse_trial_result

    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "task_name": "official",
                "exception_info": {"exception_type": "BuildError"},
                "verifier_result": None,
                "agent_result": None,
            }
        )
    )
    row = parse_trial_result(path)
    assert row["category"] == "environment_error" and row["official_reward"] is None
    assert row["cost_usd"] is None and row["input_tokens"] is None
    path.write_text(
        json.dumps(
            {
                "task_name": "official",
                "verifier_result": {"rewards": {"reward": 0}},
                "agent_result": {"metadata": {"status": "budget_exhausted"}},
            }
        )
    )
    row = parse_trial_result(path)
    assert row["category"] == "scored" and row["official_reward"] == 0
    assert row["run_status"] == "budget_exhausted" and row["output_tokens"] is None


@pytest.mark.anyio
async def test_guard_cancellation_retains_reservation_and_model_override_stops(tmp_path):
    import asyncio

    started = asyncio.Event()

    class Provider:
        async def achat(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    llm = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    llm._provider = Provider()
    ledger = BudgetLedger(tmp_path / "ledger.json")
    guard = GuardedLLM(llm, ledger, "run")
    with pytest.raises(BudgetStop):
        await guard.achat([], model="unbudgeted-model")
    assert not ledger.snapshot()["requests"]
    pending = asyncio.create_task(guard.achat([]))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    row = ledger.snapshot()["requests"][0]
    assert row["status"] == "cancelled" and row["cost_micro_usd"] is None and row["held_micro_usd"] > 0
    assert ledger.snapshot()["runs"]["run"]["blocked"]
