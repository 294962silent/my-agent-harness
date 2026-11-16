"""Tests for the deadline/progress guard and its offline ablation."""

from __future__ import annotations

import json

from experiments.long_horizon_code.progress_guard_ablation import (
    FakeClock,
    ProgressGuard,
    main,
    run_ablation,
)


def test_warning_threshold_does_not_fire_early_or_at_exact_pre_threshold():
    clock = FakeClock()
    guard = ProgressGuard(total_seconds=10, warning_ratio=0.7, max_consecutive_no_progress=10, clock=clock)
    clock.advance(6.9)
    decision = guard.poll()
    assert decision["decision"] == "continue"
    assert decision["inject"] is False
    clock.advance(0.1)
    decision = guard.poll()
    assert decision["decision"] == "inject"
    assert decision["reason"] == "deadline_warning_without_delivery"


def test_steering_is_injected_at_most_once_for_the_same_running_guard():
    clock = FakeClock()
    guard = ProgressGuard(total_seconds=10, warning_ratio=0.7, max_consecutive_no_progress=10, clock=clock)
    clock.advance(7)
    first = guard.poll()
    second = guard.poll()
    assert first["inject"] is True
    assert first["evidence"]["injection_count"] == 1
    assert second["inject"] is False
    assert second["decision"] == "suppressed"
    assert second["evidence"]["injection_count"] == 1


def test_repeated_no_progress_calls_trigger_before_deadline():
    clock = FakeClock()
    guard = ProgressGuard(total_seconds=100, warning_ratio=0.9, max_consecutive_no_progress=2, clock=clock)
    first = guard.register_tool("local_test", {"target": "x"}, True, {"progress": False})
    second = guard.register_tool("local_test", {"target": "x"}, True, {"progress": False})
    third = guard.register_tool("local_test", {"target": "x"}, True, {"progress": False})
    assert first["inject"] is False
    assert second["inject"] is False
    assert third["inject"] is True
    assert third["reason"] == "consecutive_no_progress"


def test_early_final_delivery_suppresses_deadline_steering():
    clock = FakeClock()
    guard = ProgressGuard(total_seconds=10, warning_ratio=0.7, max_consecutive_no_progress=2, clock=clock)
    clock.advance(1)
    delivered = guard.register_tool(
        "write_file",
        {"path": "result.json"},
        True,
        {"progress": True, "final_delivery": True},
    )
    clock.advance(8)
    later = guard.poll()
    assert delivered["decision"] == "delivered"
    assert later["decision"] == "delivered"
    assert later["inject"] is False
    assert later["evidence"]["final_delivery_event"] == 0


def test_exhausted_time_budget_stops_instead_of_injecting():
    clock = FakeClock()
    guard = ProgressGuard(total_seconds=5, warning_ratio=0.7, max_consecutive_no_progress=2, clock=clock)
    clock.advance(5)
    decision = guard.poll()
    assert decision["decision"] == "stop"
    assert decision["stop"] is True
    assert decision["inject"] is False
    assert decision["reason"] == "time_budget_exhausted"


def test_ablation_writes_before_validation_and_passes_host_checks():
    report = run_ablation(repeat=2)
    assert report["repeat"] == 2
    assert report["baseline"] == {
        "passed": 0,
        "total": 2,
        "failure_classes": {"timeout_before_final_delivery": 2},
    }
    assert report["enhanced"]["passed"] == 2
    assert report["enhanced"]["failure_classes"] == {"none": 2}
    assert report["deterministic"] is True
    first = report["first_observation"]
    assert first["evidence"]["baseline"]["timeout"] is True
    assert first["metrics"]["enhanced"]["injection_count"] == 1
    assert first["metrics"]["enhanced"]["compile_returncode"] == 0
    assert first["metrics"]["enhanced"]["test_returncode"] == 0


def test_cli_emits_valid_json(capsys):
    exit_code = main(["--repeat", "1", "--compact"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["experiment"] == "deadline_progress_guard_ablation"
    assert payload["enhanced"]["passed"] == 1
