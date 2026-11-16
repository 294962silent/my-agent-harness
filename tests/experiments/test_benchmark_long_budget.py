"""Frozen long-run limits without rewriting historical shared-ledger policy."""
from copy import deepcopy

import pytest

from experiments.benchmark_live.budget import BudgetLedger, BudgetStop


def phase(path, **kwargs):
    return BudgetLedger(path, campaign_id="long-pilot", spend_ceiling_usd=1, **kwargs)


def settle(ledger, run_id):
    request = ledger.reserve(run_id, input_bound=10, max_output=10, purpose="agent")
    ledger.settle(request, usage={"prompt_tokens": 1, "completion_tokens": 1}, status="ok")
    return request


def test_new_limits_extend_only_new_registered_runs_and_preserve_history(tmp_path):
    path = tmp_path / "ledger.json"
    old = BudgetLedger(path)
    settle(old, "legacy")
    before = deepcopy(old.snapshot())
    ledger = phase(path)
    ledger.register_run("long", max_requests=64, time_cap=1800)
    for _ in range(64):
        settle(ledger, "long")
    with pytest.raises(BudgetStop):
        settle(ledger, "long")
    after = ledger.snapshot()
    assert (after["limit_micro_usd"], after["max_requests"], after["time_cap"]) == (30_000_000, 16, 600)
    assert after["requests"][0] == before["requests"][0]
    assert after["runs"]["legacy"] == before["runs"]["legacy"]
    assert ledger.effective_run_limits("long") == {"max_requests": 64, "time_cap": 1800}
    with pytest.raises(ValueError):
        ledger.register_run("legacy", max_requests=64, time_cap=1800)


def test_registration_is_frozen_and_request_ceiling_only_tightens(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = phase(path, request_ceiling=3)
    ledger.register_run("long", max_requests=64, time_cap=1800)
    ledger.register_run("long", max_requests=64, time_cap=1800)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ledger.register_run("long", max_requests=63, time_cap=1800)
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        BudgetLedger(path, campaign_id="long-pilot", spend_ceiling_usd=2)
    assert ledger.effective_run_limits("long")["max_requests"] == 3


def test_clock_starts_on_first_reservation_not_registration(tmp_path, monkeypatch):
    from experiments.benchmark_live import budget
    clock = [100.0]
    monkeypatch.setattr(budget.time, "time", lambda: clock[0])
    ledger = phase(tmp_path / "ledger.json")
    ledger.register_run("long", max_requests=64, time_cap=1800)
    clock[0] = 1000.0
    assert ledger.remaining_time("long") == 1800
    settle(ledger, "long")
    clock[0] = 1600.0
    assert ledger.remaining_time("long") == 1200


def test_unknown_usage_blocks_every_registered_campaign_run_after_reopen(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = phase(path)
    for run_id in ("one", "two"):
        ledger.register_run(run_id, max_requests=64, time_cap=1800)
    request = ledger.reserve("one", input_bound=10, max_output=10, purpose="summary")
    with pytest.raises(BudgetStop):
        ledger.reserve("two", input_bound=10, max_output=10, purpose="agent")
    assert not ledger.settle(request, usage=None, status="error")
    reopened = phase(path)
    assert reopened.campaign_status()["blocked"]
    with pytest.raises(BudgetStop):
        reopened.reserve("two", input_bound=10, max_output=10, purpose="judge")
    with pytest.raises(BudgetStop):
        reopened.reserve("not-registered", input_bound=10, max_output=10, purpose="agent")


def test_phase_ceiling_is_cumulative_and_unknown_stops_other_new_phases(tmp_path):
    path = tmp_path / "ledger.json"
    old = BudgetLedger(path)
    settle(old, "historical")
    first = BudgetLedger(path, campaign_id="memory", spend_ceiling_usd=0.00002)
    second = BudgetLedger(path, campaign_id="coding", spend_ceiling_usd=0.00004)
    first.register_run("memory-run", max_requests=2, time_cap=1800)
    second.register_run("coding-run", max_requests=64, time_cap=1800)
    # The historical charge is included; this reservation fits the absolute cap.
    request = first.reserve("memory-run", input_bound=10, max_output=10, purpose="judge")
    first.settle(request, usage=None, status="error")
    assert second.campaign_status()["any_campaign_blocked"]
    with pytest.raises(BudgetStop):
        second.reserve("coding-run", input_bound=1, max_output=1, purpose="agent")


def test_registered_ceiling_applies_even_without_campaign_constructor(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, campaign_id="phase", spend_ceiling_usd=0.000015)
    ledger.register_run("new", max_requests=64, time_cap=1800)
    settle(ledger, "new")
    # Reopening a generic legacy view cannot discard the run's persisted cap.
    with pytest.raises(BudgetStop):
        BudgetLedger(path).reserve("new", input_bound=10, max_output=10, purpose="agent")


def test_live_factory_is_explicit_fixed_and_disables_sdk_retries(monkeypatch):
    from experiments.benchmark_live import budget
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "offline-fixture")
    seen = []
    monkeypatch.setattr(budget, "LLM", lambda config: seen.append(config) or config)
    config = budget.create_live_llm(max_output_tokens=1024)
    assert config.model == "deepseek-v4.1-flash" and config.provider == "openai"
    assert config.max_retries == 0 and config.max_tokens == 1024
    monkeypatch.delenv("LIVE_BENCH_API_KEY")
    with pytest.raises(ValueError, match="LIVE_BENCH_API_KEY"):
        budget.create_live_llm()
    assert len(seen) == 1


@pytest.mark.parametrize("limits", [{"max_requests": True, "time_cap": 1800},
                                    {"max_requests": 65, "time_cap": 1800},
                                    {"max_requests": 64, "time_cap": float("nan")},
                                    {"max_requests": 64, "time_cap": 1801}])
def test_invalid_long_run_limits_are_rejected(tmp_path, limits):
    ledger = phase(tmp_path / "ledger.json")
    with pytest.raises(ValueError):
        ledger.register_run("long", **limits)
