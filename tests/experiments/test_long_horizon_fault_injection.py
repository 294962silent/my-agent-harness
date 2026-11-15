"""Regression tests for the Phase A long-horizon fault injections."""

from __future__ import annotations

import asyncio
import json

from experiments.long_horizon_code import fault_injection


def _run(name: str, repeat: int = 2):
    return asyncio.run(fault_injection.run_experiment(repeat=repeat, scenario_names=[name]))["scenarios"][0]


def test_artifact_readback_beats_unpersisted_clipping():
    scenario = _run("context_budget_artifact_readback", repeat=3)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 3
    assert scenario["evidence"]["deterministic"] is True
    assert scenario["evidence"]["first_observation"]["canonical_session_body_preserved"] is True
    assert scenario["metrics"]["first_observation"]["artifact_hits"] >= 1


def test_memory_revision_and_scope_reject_stale_candidates():
    scenario = _run("memory_revision_scope", repeat=3)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 3
    assert scenario["evidence"]["first_observation"]["stale_candidate_revision_matches_current"] is False
    assert scenario["evidence"]["first_observation"]["cross_scope_candidate_used"] is False


def test_research_campaign_journal_resumes_without_replay():
    scenario = _run("checkpoint_resume", repeat=2)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 2
    assert scenario["evidence"]["first_observation"]["baseline_worklog"] == ["analysis", "analysis", "patch"]
    assert scenario["evidence"]["first_observation"]["enhanced_worklog"] == ["patch"]


def test_file_mutation_queue_prevents_lost_update():
    scenario = _run("concurrent_file_mutation", repeat=3)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 3
    assert scenario["metrics"]["first_observation"]["baseline_lost_updates"] == 1
    assert scenario["metrics"]["first_observation"]["enhanced_lost_updates"] == 0


def test_policy_registry_rejects_train_only_promotion_and_rolls_back():
    scenario = _run("controlled_policy_promotion", repeat=2)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 2
    observation = scenario["metrics"]["first_observation"]
    assert observation["train_gain"] > 0
    assert observation["holdout_gain_vs_incumbent"] < 0
    assert observation["incumbent_retained"] is True
    assert observation["rollback_success"] is True


def test_host_scoring_uses_pinned_docker_and_rejects_untrusted_outputs():
    scenario = _run("host_scoring_sandbox", repeat=2)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 2
    observation = scenario["metrics"]["first_observation"]
    assert observation["container_runs"] == 2
    assert observation["valid_candidate_accepted"] is True
    assert observation["escaped_output_rejected"] is True
    assert observation["extra_workspace_rejected"] is True
    assert observation["self_report_ignored"] is True
    assert observation["image_identity_verified"] is True
    assert scenario["metrics"]["container_runs_total"] == 4
    evidence = scenario["evidence"]["first_observation"]
    assert evidence["verified_image_id"] == fault_injection.HOST_SCORING_IMAGE
    assert evidence["reported_score"] != evidence["host_recomputed_score"]
    assert evidence["escaped_feedback"] == "candidate execution failed"
    assert evidence["escaped_output_files"] == ["predictions.csv"]


def test_longitudinal_repo_repair_requires_all_stages_and_host_tests():
    scenario = _run("longitudinal_repo_repair", repeat=2)
    assert scenario["status"] == "completed"
    assert scenario["baseline_pass"] == 0
    assert scenario["enhanced_pass"] == 2
    assert scenario["evidence"]["deterministic"] is True
    stages = scenario["evidence"]["first_observation"]["stages"]
    assert [stage["stage"] for stage in stages] == [
        "diagnostic_and_config",
        "regional_constraint_revision",
        "journal_resume_after_reconstruction",
        "conflicting_file_mutations",
    ]
    assert all(stage["baseline_pass"] is False for stage in stages)
    assert all(stage["enhanced_pass"] is True for stage in stages)
    host = scenario["evidence"]["first_observation"]["host_validation"]
    assert host["baseline"]["evidence"]["compile_returncode"] == 0
    assert host["baseline"]["evidence"]["test_returncode"] != 0
    assert host["enhanced"]["evidence"]["compile_returncode"] == 0
    assert host["enhanced"]["evidence"]["test_returncode"] == 0


def test_cli_report_schema_and_default_repeat():
    report = asyncio.run(fault_injection.run_experiment(repeat=1))
    assert report["repeat"] == 1
    assert report["scenario_count"] == 7
    assert report["scope"]["paid_model_calls"] is False
    assert [item["scenario"] for item in report["scenarios"]] == list(fault_injection.SCENARIO_NAMES)
    required = {
        "scenario",
        "baseline_pass",
        "enhanced_pass",
        "failure_reason",
        "evidence",
        "metrics",
    }
    assert all(required <= set(item) for item in report["scenarios"])
    json.dumps(report, allow_nan=False)
