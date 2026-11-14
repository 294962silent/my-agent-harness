"""Synthetic continuation evidence only; no paid model calls or real campaign writes."""
import asyncio
import errno
import json
from types import SimpleNamespace

import pytest

from experiments.context_rsi import live_runner, live_tasks
from experiments.context_rsi.continue_live import (
    ContinuationCampaign, ContinuationError, register, run_continuation, safe_diagnostic,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in live_runner._model_secret_names():
        monkeypatch.delenv(name)


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    tasks = [SimpleNamespace(task_id=f"HumanEval/{n}", split=split, source_sha=str(n).zfill(64),
        host_evaluator_sha="b" * 64, preflight_proof={"kind": "synthetic"}, verify=lambda: None)
        for split, numbers in (("train", (7, 11, 27)), ("val", (32, 45)), ("test", (1, 6, 9, 20, 33, 40))) for n in numbers]
    monkeypatch.setattr(live_tasks, "load_tasks", lambda path: tasks)
    manifest = tmp_path / "synthetic-tasks.json"
    manifest.write_text('{"kind":"synthetic continuation schema fixture"}')
    legacy = tmp_path / "historical.json"
    legacy.write_text('{"untouched":true}')
    campaign = live_runner.Campaign.freeze(tmp_path / "campaign", campaign_id="rsi-live-20261114-v2", legacy_ledger=legacy)
    plan = live_runner.freeze_plan(campaign, manifest)
    policy = {"parameters": {"max_width": 2, "max_refine": 1}, "kind": "synthetic", "promoted": False}
    for index, slot in enumerate(plan["slots"]):
        if index == 5:
            live_runner._exclusive_json(campaign.root / "research-policy.json", policy)
            campaign._append("optimizer_finished", policy=policy, policy_sha256=live_runner._sha(live_runner._bytes(policy)))
        campaign.start_trial(slot["trial_id"], metadata=slot)
        if slot["trial_id"] == "test-HumanEval-6-s23-C":
            budget = live_runner.JournaledParentBudget(campaign, slot["trial_id"], verify_task=lambda: None)
            record = budget.reserve([], None, 64, "agent")
            budget.settle(record, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
            directory = campaign.root / "trials" / slot["trial_id"]
            directory.mkdir(parents=True)
            report = {"status": "invalid", "error_type": "PermissionError", "cleanup_errors": [], "budget": budget.snapshot()}
            live_runner._exclusive_json(directory / "result.json", report)
            final = {"trial_id": slot["trial_id"], "task_id": slot["task_id"], "split": "test", "final": None,
                "final_success": False, "execution_integrity": False,
                "runtime_result_sha256": live_runner._sha((directory / "result.json").read_bytes()), "budget": budget.snapshot()}
            live_runner._exclusive_json(directory / "final-evaluation.json", final)
            campaign.finish_trial(slot["trial_id"], status="failed", evidence={
                "final_evaluation_sha256": live_runner._sha((directory / "final-evaluation.json").read_bytes())})
            break
        campaign.finish_trial(slot["trial_id"], status="completed", evidence={"kind": "synthetic_no_provider"})
    assert len(campaign.summary()["trials"]) == 16
    return campaign, plan


def test_register_is_no_payment_and_does_not_hide_original_failure(stopped, tmp_path):
    campaign, _ = stopped
    old = campaign.path.read_bytes()
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    assert len(receipt["pending_trial_ids"]) == 37
    assert campaign.path.read_bytes().startswith(old)
    wrapped = ContinuationCampaign(campaign.root, incident)
    assert wrapped.summary()["blocked"] is True
    assert wrapped.operational_status()["ready"] is True
    assert wrapped.summary()["trials"]["test-HumanEval-6-s23-C"]["status"] == "failed"
    assert wrapped.summary()["requests"] == 1


def test_old_slot_unknown_slot_and_wrong_tuple_never_start(stopped, tmp_path):
    campaign, plan = stopped
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    wrapped = ContinuationCampaign(campaign.root, incident)
    before = campaign.path.read_bytes()
    for trial, metadata in (("test-HumanEval-6-s23-C", plan["slots"][15]), ("alternate-id", {})):
        with pytest.raises(ContinuationError):
            wrapped.start_trial(trial, metadata=metadata)
    next_slot = next(s for s in plan["slots"] if s["trial_id"] == receipt["pending_trial_ids"][0])
    with pytest.raises(ContinuationError):
        wrapped.start_trial(next_slot["trial_id"], metadata={**next_slot, "arm": "wrong"})
    assert campaign.path.read_bytes() == before


@pytest.mark.parametrize("drift", ["incident", "failure", "wrapper", "source"])
def test_every_guard_rejects_material_drift(stopped, tmp_path, monkeypatch, drift):
    from experiments.context_rsi import continue_live
    campaign, _ = stopped
    incident = tmp_path / "incident"
    register(campaign.root, incident)
    wrapped = ContinuationCampaign(campaign.root, incident)
    if drift == "incident":
        (incident / "incident.json").write_text('{"changed":true}')
    elif drift == "failure":
        (campaign.root / "trials/test-HumanEval-6-s23-C/result.json").write_text('{"changed":true}')
    elif drift == "wrapper":
        monkeypatch.setattr(continue_live, "_script_hash", lambda: "changed")
    else:
        monkeypatch.setattr(live_runner, "_execution_fingerprint", lambda: {"changed": "source"})
    with pytest.raises((ContinuationError, live_runner.JournalError)):
        wrapped.assert_execution_pin()


@pytest.mark.parametrize("status", ["pending", "unknown", "failed"])
def test_new_failure_or_unsettled_request_stops_continuation(stopped, tmp_path, status):
    campaign, plan = stopped
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    wrapped = ContinuationCampaign(campaign.root, incident)
    slot = next(s for s in plan["slots"] if s["trial_id"] == receipt["pending_trial_ids"][0])
    wrapped.start_trial(slot["trial_id"], metadata=slot)
    if status == "failed":
        wrapped.finish_trial(slot["trial_id"], status="failed", evidence={"error_type": "SyntheticFailure"})
    else:
        budget = live_runner.JournaledParentBudget(wrapped, slot["trial_id"], verify_task=lambda: None)
        record = budget.reserve([], None, 64, "agent")
        if status == "unknown":
            with pytest.raises(Exception):
                budget.settle(record, None, error="SyntheticUnknown")
    assert wrapped.operational_status()["ready"] is False
    with pytest.raises(ContinuationError):
        wrapped.start_trial(receipt["pending_trial_ids"][1], metadata={})


def test_unreviewed_extra_failure_prevents_registration(stopped, tmp_path):
    campaign, plan = stopped
    slot = plan["slots"][16]
    campaign._append("trial_started", trial_id=slot["trial_id"], metadata=slot)
    campaign._append("trial_finished", trial_id=slot["trial_id"], status="failed", evidence={})
    with pytest.raises(ContinuationError):
        register(campaign.root, tmp_path / "not-an-additional-exception")


def test_one_slot_runs_only_next_unstarted_and_keeps_old_failed(stopped, tmp_path, monkeypatch):
    campaign, plan = stopped
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    calls = []
    async def no_provider(**kwargs):
        c, trial = kwargs["campaign"], kwargs["trial_id"]
        calls.append(trial)
        slot = next(s for s in plan["slots"] if s["trial_id"] == trial)
        c.start_trial(trial, metadata=slot)
        c.finish_trial(trial, status="completed", evidence={"kind": "synthetic_no_provider"})
        return {"result": SimpleNamespace(report={"status": "completed", "error_type": None})}
    monkeypatch.setattr(live_runner, "run_prepared_trial", no_provider)
    result = asyncio.run(run_continuation(campaign.root, incident, tmp_path / "run-1", max_new_trials=1, llm=object()))
    assert calls == receipt["pending_trial_ids"][:1] and result["new_trials"] == 1
    assert result["original_summary"]["blocked"] is True and result["operational_gate"]["ready"] is True
    asyncio.run(run_continuation(campaign.root, incident, tmp_path / "run-2", max_new_trials=1, llm=object()))
    assert calls == receipt["pending_trial_ids"][:2]
    assert campaign.summary()["trials"]["test-HumanEval-6-s23-C"]["status"] == "failed"


def test_diagnostic_never_dumps_message_or_outside_path(tmp_path):
    error = PermissionError(errno.EACCES, "SECRET_MESSAGE_DO_NOT_PRINT", str(tmp_path.parent / "private-secret"))
    result = safe_diagnostic(error, tmp_path)
    assert result["error_type"] == "PermissionError" and result["errno"] == errno.EACCES
    assert result["filename_relative"] is None
    assert "SECRET" not in json.dumps(result) and "private-secret" not in json.dumps(result)
    result = safe_diagnostic(None, tmp_path, runtime_error_type="PermissionError")
    assert result["errno"] is result["winerror"] is result["filename_relative"] is None


@pytest.mark.parametrize("boundary", ["request", "evaluation"])
def test_incident_bytes_checked_before_each_payment_and_grade(stopped, tmp_path, boundary):
    campaign, plan = stopped
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    wrapped = ContinuationCampaign(campaign.root, incident)
    slot = next(s for s in plan["slots"] if s["trial_id"] == receipt["pending_trial_ids"][0])
    wrapped.start_trial(slot["trial_id"], metadata=slot)
    budget = live_runner.JournaledParentBudget(wrapped, slot["trial_id"], verify_task=lambda: None)
    before = wrapped.summary()["requests"]
    (incident / "incident.json").write_text('{"changed":true}')
    with pytest.raises(live_runner.JournalError):
        budget.reserve([], None, 64, "agent") if boundary == "request" else budget.begin_evaluation()
    assert wrapped.summary()["requests"] == before and not budget.evaluations


def test_start_cas_detects_an_additional_failure_inside_registration_window(stopped, tmp_path, monkeypatch):
    campaign, plan = stopped
    incident = tmp_path / "incident"
    receipt = register(campaign.root, incident)
    wrapped = ContinuationCampaign(campaign.root, incident)
    first, second = [next(s for s in plan["slots"] if s["trial_id"] == name) for name in receipt["pending_trial_ids"][:2]]
    original = wrapped._append
    injected = False
    def race(event, **kwargs):
        nonlocal injected
        if event == "trial_started" and not injected:
            injected = True
            original("trial_started", trial_id=second["trial_id"], metadata=second)
            original("trial_finished", trial_id=second["trial_id"], status="failed", evidence={"kind": "synthetic-race"})
        return original(event, **kwargs)
    monkeypatch.setattr(wrapped, "_append", race)
    with pytest.raises(ContinuationError):
        wrapped.start_trial(first["trial_id"], metadata=first)
    assert first["trial_id"] not in wrapped.summary()["trials"]


def test_cli_rejects_without_printing_original_exception_message(tmp_path, monkeypatch, capsys):
    from experiments.context_rsi import continue_live
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, "SECRET_MESSAGE", "SECRET_ABSOLUTE_FILENAME")
    monkeypatch.setattr(continue_live, "register", fail)
    result = continue_live.main(["register", "--campaign", str(tmp_path / "campaign"), "--incident", str(tmp_path / "incident")])
    assert result == 1 and "SECRET" not in capsys.readouterr().out


def test_first_continuation_is_limited_to_one_new_slot(stopped, tmp_path, monkeypatch):
    campaign, _ = stopped
    incident = tmp_path / "incident"
    register(campaign.root, incident)
    called = []
    async def forbidden(**kwargs):
        called.append(kwargs["trial_id"])
        raise AssertionError("first smoke limit must reject before dispatch")
    monkeypatch.setattr(live_runner, "run_prepared_trial", forbidden)
    with pytest.raises(ContinuationError):
        asyncio.run(run_continuation(campaign.root, incident, tmp_path / "must-not-run-two", max_new_trials=2, llm=object()))
    assert not called
