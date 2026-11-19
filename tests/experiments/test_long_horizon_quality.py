"""Offline contracts for long-horizon quality pre-registration and analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.long_horizon_quality.analysis import (
    TrialOutcome,
    analyze,
    cluster_bootstrap_ci,
    exact_sign_test,
)
from experiments.long_horizon_quality import harbor_pilot
from experiments.long_horizon_quality.harbor_pilot import MAX_ROUNDS
from experiments.long_horizon_quality.protocol import (
    CONFIRMATORY_TASK_COUNT,
    DEFAULT_ARMS,
    TERMINAL_BENCH_REVISION,
    ProtocolError,
    build_slots,
    freeze_campaign,
    load_plan,
)


def test_mea_pilot_reserves_a_manager_terminal_round() -> None:
    assert MAX_ROUNDS >= 3


def test_quality_pilot_requires_unique_campaign_identity(monkeypatch) -> None:
    monkeypatch.delenv("LHQ_CAMPAIGN_ID", raising=False)
    with pytest.raises(harbor_pilot.PilotError, match="unique campaign id"):
        harbor_pilot._resolve_campaign_id(None)
    monkeypatch.setenv("LHQ_CAMPAIGN_ID", "pilot-explicit")
    assert harbor_pilot._resolve_campaign_id(None) == "pilot-explicit"
    assert harbor_pilot._resolve_campaign_id("pilot-cli") == "pilot-cli"


def test_finished_resume_state_is_enriched_before_journal_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    slot = {
        "task": "fix-git",
        "arm": "mea",
        "trial": 1,
        "run_id": "run-1",
    }
    monkeypatch.setattr(
        harbor_pilot,
        "_live_metadata",
        lambda directory, value: {"contract": directory.name, "run_id": value["run_id"]},
    )
    monkeypatch.setattr(
        harbor_pilot,
        "_usage",
        lambda ledger, run_id: {"requests": 12, "run_id": run_id},
    )

    state = harbor_pilot._enrich_finished_state(
        {"status": "finished"},
        directory=tmp_path,
        slot=slot,
        ledger=object(),
    )

    assert state == {
        "status": "finished",
        "live_metadata": {"contract": tmp_path.name, "run_id": "run-1"},
        "usage": {"requests": 12, "run_id": "run-1"},
        "task": "fix-git",
        "arm": "mea",
        "trial": 1,
    }


def test_outcome_rows_preserve_finished_error_category(monkeypatch) -> None:
    slot = {
        "task_id": "fix-git",
        "arm": "mea",
        "trial": 0,
        "run_id": "run-1",
    }
    plan = {"slots": [slot]}
    journal = {
        "run-1": {
            "status": "finished",
            "category": "execution_or_verifier_error",
        }
    }
    monkeypatch.setattr(harbor_pilot, "_ledger_for_slot", lambda _slot: object())
    monkeypatch.setattr(
        harbor_pilot,
        "_usage",
        lambda _ledger, _run_id: {"requests": 1},
    )

    row = harbor_pilot._outcome_rows(plan, journal)[0]

    assert row["status"] == "execution_or_verifier_error"


def _manifest(path: Path, count: int) -> Path:
    data = {
        "schema_version": 1,
        "benchmark": "terminal-bench-2.1",
        "revision": TERMINAL_BENCH_REVISION,
        "tasks": [
            {"id": f"task-{index:02d}", "cohort": "quality"}
            for index in range(count)
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _outcome(
    task: str,
    arm: str,
    trial: int,
    reward: int,
    *,
    usage_complete: bool = True,
) -> TrialOutcome:
    return TrialOutcome(
        task_id=task,
        arm=arm,
        trial=trial,
        status="scored",
        official_reward=reward,
        requests=10 if arm == "react" else 14,
        total_tokens=1000 if arm == "react" else 1500,
        cache_read_tokens=500,
        prompt_tokens=400,
        cache_write_tokens=100,
        cost_micro_usd=100 if arm == "react" else 150,
        usage_complete=usage_complete,
    )


def test_build_slots_is_deterministic_and_complete() -> None:
    first = build_slots(
        ["fix-git", "large-scale-text-editing"],
        campaign_id="campaign",
        trials=3,
        seed=7,
    )
    second = build_slots(
        ["fix-git", "large-scale-text-editing"],
        campaign_id="campaign",
        trials=3,
        seed=7,
    )
    assert first == second
    assert len(first) == 2 * len(DEFAULT_ARMS) * 3
    assert len({slot.run_id for slot in first}) == len(first)
    assert {
        (slot.task_id, slot.arm, slot.trial)
        for slot in first
    } == {
        (task, arm, trial)
        for task in ("fix-git", "large-scale-text-editing")
        for arm in DEFAULT_ARMS
        for trial in range(3)
    }
    for task in ("fix-git", "large-scale-text-editing"):
        rows = [slot for slot in first if slot.task_id == task]
        assert [slot.arm for slot in rows] == [
            rows[0].arm,
            rows[0].arm,
            rows[0].arm,
            rows[3].arm,
            rows[3].arm,
            rows[3].arm,
        ]
        assert [slot.trial for slot in rows] == [0, 1, 2, 0, 1, 2]


def test_freeze_pilot_and_confirmatory_task_gate(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "pilot.json", 3)
    plan = freeze_campaign(
        task_manifest=manifest,
        output=tmp_path / "pilot-run",
        campaign_id="pilot",
        stage="pilot",
        trials=3,
    )
    assert plan["claim_ceiling"] == "descriptive_only"
    assert plan["cache_policy"]["stable_system_prompt"] is True
    assert plan["cache_policy"]["stable_tool_schema"] is True
    assert plan["cache_policy"]["system_prompt_sha256_required"] is True
    assert load_plan(tmp_path / "pilot-run" / "plan.json") == plan
    assert (tmp_path / "pilot-run" / "plan.sha256").is_file()

    incomplete = _manifest(tmp_path / "incomplete.json", 12)
    with pytest.raises(ProtocolError, match="requires all 89 tasks"):
        freeze_campaign(
            task_manifest=incomplete,
            output=tmp_path / "blocked",
            campaign_id="blocked",
            stage="confirmatory",
            trials=3,
        )

    complete = _manifest(tmp_path / "complete.json", CONFIRMATORY_TASK_COUNT)
    final = freeze_campaign(
        task_manifest=complete,
        output=tmp_path / "final-run",
        campaign_id="final",
        stage="confirmatory",
        trials=3,
    )
    assert final["task_count"] == CONFIRMATORY_TASK_COUNT
    assert final["claim_ceiling"] == "quality_and_benchmark"


def test_exact_sign_and_cluster_bootstrap_are_deterministic() -> None:
    values = [1.0] * 8
    sign = exact_sign_test(values)
    assert sign["p_value_two_sided"] == pytest.approx(2 / 256)
    low, high = cluster_bootstrap_ci(
        {f"task-{index}": 1.0 for index in range(8)},
        iterations=500,
        seed=3,
    )
    assert low == high == 1.0


def test_positive_confirmatory_result_passes_quality_gate(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "complete.json", CONFIRMATORY_TASK_COUNT)
    plan = freeze_campaign(
        task_manifest=manifest,
        output=tmp_path / "run",
        campaign_id="quality",
        stage="confirmatory",
        trials=3,
    )
    outcomes = []
    for task_index in range(CONFIRMATORY_TASK_COUNT):
        task = f"task-{task_index:02d}"
        for trial in range(3):
            outcomes.append(_outcome(task, "react", trial, 0))
            outcomes.append(_outcome(task, "mea", trial, 1))
    report = analyze(
        outcomes,
        plan=plan,
        bootstrap_iterations=200,
        seed=5,
    )
    assert report["analysis_valid"] is True
    assert report["mean_delta"] == 1.0
    assert report["cluster_bootstrap_95pct"] == [1.0, 1.0]
    assert report["sign_test"]["p_value_two_sided"] < 0.05
    assert report["resources"]["cache"]["react"]["cache_hit_rate"] == 0.5
    assert report["resources"]["cache"]["mea"]["cache_hit_rate"] == 0.5
    assert "benchmark_quality_improvement" in report["claims"]


def test_missing_usage_blocks_claim_but_keeps_descriptive_summary() -> None:
    outcomes = []
    for task_index in range(8):
        task = f"task-{task_index}"
        for trial in range(3):
            outcomes.append(_outcome(task, "react", trial, 0))
            outcomes.append(
                _outcome(
                    task,
                    "mea",
                    trial,
                    1,
                    usage_complete=task_index != 0,
                )
            )
    report = analyze(outcomes, bootstrap_iterations=200)
    assert report["analysis_valid"] is False
    assert report["claims"] == []
    assert report["mean_delta"] > 0
    assert report["resources"]["cache"]["mea"]["cache_hit_rate"] is None


def test_duplicate_plan_or_outcome_slot_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "pilot.json", 1)
    plan = freeze_campaign(
        task_manifest=manifest,
        output=tmp_path / "run",
        campaign_id="duplicate",
        stage="pilot",
        trials=3,
    )
    duplicate = _outcome("task-00", "react", 0, 0)
    with pytest.raises(ProtocolError, match="duplicate outcome"):
        analyze([duplicate, duplicate], plan=plan)

    with pytest.raises(ProtocolError, match="exactly react and mea arms"):
        build_slots(["task-00"], campaign_id="arms", arms=("react",), trials=3)


def test_plan_tampering_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "pilot.json", 1)
    plan_path = tmp_path / "run" / "plan.json"
    freeze_campaign(
        task_manifest=manifest,
        output=plan_path.parent,
        campaign_id="tamper",
        stage="pilot",
        trials=3,
    )
    value = json.loads(plan_path.read_text(encoding="utf-8"))
    value["claim_ceiling"] = "quality_and_benchmark"
    plan_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProtocolError, match="hash drifted"):
        load_plan(plan_path)
