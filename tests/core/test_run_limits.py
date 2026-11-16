"""Execution limit configuration and accounting contracts."""
import pytest
from my_agent_core.run_lifecycle import RunLimits, RunBudget, RunLimitExceeded


@pytest.mark.parametrize("kwargs", [{"max_turns": 0}, {"max_turns": -1}, {"max_turns": True},
                                    {"max_turns": 1.5}, {"max_seconds": 0}, {"max_seconds": -1},
                                    {"max_seconds": float("nan")}, {"max_seconds": float("inf")},
                                    {"max_seconds": True}])
def test_limits_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        RunLimits(**kwargs)


def test_budget_counts_started_turns_and_uses_monotonic_deadline():
    now = [10.0]
    budget = RunBudget(RunLimits(max_turns=1, max_seconds=2), clock=lambda: now[0])
    assert budget.begin_turn() == 1
    with pytest.raises(RunLimitExceeded, match="max_turns"):
        budget.begin_turn()
    now[0] = 12.0
    with pytest.raises(RunLimitExceeded, match="max_seconds"):
        budget.check()
