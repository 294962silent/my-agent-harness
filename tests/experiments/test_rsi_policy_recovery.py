"""Offline recovery fixtures. No genuine provider calls or quality assertions."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiments.context_rsi import live_runner, live_tasks
from experiments.context_rsi import recover_research_policy as recovery
from my_coding_agent.dream_rsi_contract import canonical, checksum, make_task_evidence, policy_identity


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for name in live_runner._model_secret_names():
        monkeypatch.delenv(name)


def build_campaign(root, monkeypatch, missing=("HumanEval/32",), first_success=False):
    root.mkdir(parents=True)
    legacy = root / "historical-pointer.json"
    legacy.write_text('{"kind":"synthetic untouched historical pointer"}')
    tasks = []
    for split, numbers in (("train", (7, 11, 27)), ("val", (32, 45)), ("test", (1, 6, 9, 20, 33, 40))):
        for number in numbers:
            tasks.append(SimpleNamespace(task_id=f"HumanEval/{number}", split=split, source_sha=str(number).zfill(64),
                host_evaluator_sha="b" * 64, preflight_proof={"kind": "synthetic"}, verify=lambda: None))
    manifest = root / "synthetic-task-manifest.json"
    manifest.write_text('{"kind":"synthetic recovery schema fixture, not official task data"}')
    monkeypatch.setattr(live_tasks, "load_tasks", lambda path: tasks)
    campaign = live_runner.Campaign.freeze(root / "campaign", campaign_id="synthetic-recovery-unit", legacy_ledger=legacy)
    plan = live_runner.freeze_plan(campaign, manifest)
    reports = []
    for slot in plan["slots"][:5]:
        campaign.start_trial(slot["trial_id"], metadata=slot)
        budget = live_runner.JournaledParentBudget(campaign, slot["trial_id"], verify_task=lambda: None)
        unavailable = slot["task_id"] in missing
        for _ in range(12 if unavailable else 4):
            record = budget.reserve([], None, 64, "agent")
            budget.settle(record, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
        directory = campaign.root / "trials" / slot["trial_id"]
        directory.mkdir(parents=True)
        report = {"task_id": slot["task_id"], "split": slot["split"], "status": "invalid_attempt" if unavailable else "completed",
            "trace_sha256": None, "selected_candidate": None if unavailable else "b0_a1",
            "selected_visible_score": None if unavailable else 1, "budget": budget.snapshot(),
            "provenance": {"kind": "synthetic"}, "policy": {"parameters": {"max_width": 2, "max_refine": 1}},
            "attempts": [{"agent_terminal": {"stop_reason": "budget_exhausted" if unavailable else "end_turn"},
                          "visible": {"score": 1}, "valid": not unavailable}]}
        if not unavailable:
            evidence = make_task_evidence(public_input=f"def public_task_{slot['task_id'].replace('/', '_')}():\n    pass\n",
                                          visible_spec="Synthetic visible specification; no hidden test data")
            def cell(score):
                return {"evaluated": True, "combined_score": score, "n_valid": int(score), "n_total": 1}
            trace = {"version": 1, "n_problems": 1, "baseline": cell(0), "nodes": [
                {"id": f"b{b}_a{a}", "branch": b, "attempt": a, "parent_id": None if a == 0 else f"b{b}_a{a-1}",
                 "path": "synthetic", "status": "done", "raw": cell(1 if first_success else a)} for a in range(2) for b in range(2)],
                "task_evidence": evidence, "task_evidence_sha256": checksum(canonical(evidence))}
            (directory / "trace.json").write_bytes(canonical(trace))
            report["trace_sha256"] = live_runner._sha((directory / "trace.json").read_bytes())
            report["task_evidence_sha256"] = trace["task_evidence_sha256"]
        live_runner._exclusive_json(directory / "result.json", report)
        final = {"task_id": slot["task_id"], "split": slot["split"], "final": None if unavailable else {"valid": True, "score": 1},
                 "final_success": not unavailable, "runtime_result_sha256": live_runner._sha((directory / "result.json").read_bytes()),
                 "budget": budget.snapshot()}
        live_runner._exclusive_json(directory / "final-evaluation.json", final)
        campaign.finish_trial(slot["trial_id"], status="completed", evidence={
            "final_evaluation_sha256": live_runner._sha((directory / "final-evaluation.json").read_bytes())})
        reports.append((directory, report))
    campaign._append("optimizer_started", collection_trials=[s["trial_id"] for s in plan["slots"][:5]])
    partial = campaign.root / "dataset"
    partial.mkdir()
    for index, (directory, report) in enumerate(reports):
        if report["trace_sha256"] is None:
            break
        (partial / f"trace-{index}.json").write_bytes((directory / "trace.json").read_bytes())
    return campaign


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    return build_campaign(tmp_path / "fixture", monkeypatch)


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = []
    monkeypatch.setattr(recovery, "_sdk_fingerprint", lambda: {"kind": "synthetic unit SDK fingerprint"})
    def optimize(manifest, output, **kwargs):
        calls.append((manifest, output, kwargs))
        Path(output).mkdir()
        (Path(output) / "synthetic-only.json").write_text('{"not_actual_SDK":true}')
        data = recovery.optimizer.load_dataset(manifest)
        entries = []
        for index, entry in enumerate(data["entries"]):
            name = f"trace-{index}.json"
            (Path(output) / name).write_bytes(Path(entry["path"]).read_bytes())
            entries.append({"split": entry["split"], "frozen_file": name, "sha256": entry["sha256"]})
        choices = recovery.optimizer.effective_candidates(
            [e["trace"] for e in data["entries"] if e["split"] == "train"], kwargs["budgets"], kwargs["max_candidates"])
        (Path(output) / "plan.json").write_text(json.dumps({"candidate_selection": "effective-balanced-v2",
            "max_candidates_excludes_baseline": True, "max_candidates": kwargs["max_candidates"],
            "budgets": list(kwargs["budgets"]), "entries": entries, "candidates": choices}))
        selected = next(p for p in choices if p["parameters"] == {"max_width": 1, "max_refine": 1})
        scores = {"baseline": .5, **{p["role"]: .5 for p in choices}}
        scores[selected["role"]] = .9
        return {"status": "completed", "kind": "synthetic", "selected": selected, "train_scores": scores,
            "holdout_scores": {"baseline": 0, selected["role"]: 1}, "promoted": False,
            "validation_gain": 999, "incumbent_gain": 999}
    monkeypatch.setattr(recovery.optimizer, "optimize", optimize)
    return calls


def test_preflight_retains_full_denominator_and_does_not_search(campaign, fake_sdk):
    before = campaign.path.read_bytes()
    result = recovery.inspect_recovery(campaign.root)
    assert result["coverage_counts"] == {"train": [3, 3], "holdout": [1, 2], "test_started": 0}
    assert len(result["coverage"]) == 5 and result["currency"] is None
    failed = next(c for c in result["coverage"] if c["task_id"] == "HumanEval/32")
    assert failed["original_final"] is None and not failed["replayable"]
    assert failed["usage"]["requests"] == 12 and failed["usage"]["used_tokens"] == 60
    assert result["validation_gain"] is result["incumbent_gain"] is None
    assert not fake_sdk and campaign.path.read_bytes() == before


def test_train_choice_reconstructs_legacy_grid_and_ignores_holdout(tmp_path):
    choices = recovery.optimizer._legacy_candidate_grid(4)
    selected = next(p for p in choices if p["parameters"] == {"max_width": 1, "max_refine": 1})
    plan = {"max_candidates": 4, "candidates": choices}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    result = {"train_scores": {"baseline": .5, **{p["role"]: .9 if p == selected else .5 for p in choices}},
              "selected": selected, "holdout_scores": {"baseline": 999, selected["role"]: -999}}
    assert recovery._train_only_choice(result, 4, tmp_path) == selected
    plan["candidates"] = recovery.optimizer.candidate_grid(4)
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(recovery.RecoveryError, match="candidate inventory"):
        recovery._train_only_choice(result, 4, tmp_path)


def test_train_choice_rejects_new_plan_candidate_and_trace_tampering(campaign, fake_sdk, tmp_path):
    output = tmp_path / "recovery"
    recovery.recover(campaign.root, output)
    root = output / "optimizer"
    result = json.loads((output / "sdk-return.json").read_text())
    plan = json.loads((root / "plan.json").read_text())
    original = (root / "plan.json").read_bytes()
    assert recovery._train_only_choice(result, 4, root) == result["selected"]
    plan["candidates"][0]["parameters"]["max_width"] = 64
    (root / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(recovery.RecoveryError, match="candidate inventory"):
        recovery._train_only_choice(result, 4, root)
    (root / "plan.json").write_bytes(original)
    entry = next(e for e in plan["entries"] if e["split"] == "train")
    (root / entry["frozen_file"]).write_text('{}')
    with pytest.raises(recovery.RecoveryError, match="candidate reconstruction"):
        recovery._train_only_choice(result, 4, root)


def test_recovery_commits_only_unpublished_train_choice_preserving_raw(campaign, fake_sdk, tmp_path):
    before = recovery._inventory(campaign.root, skip=("journal.jsonl",))
    output = tmp_path / "recovery"
    result = recovery.recover(campaign.root, output)
    assert len(fake_sdk) == 1 and fake_sdk[0][2]["dry_run"] is True
    assert fake_sdk[0][2]["budgets"] == (1, 2, 4) and fake_sdk[0][2]["max_candidates"] == 4
    policy = result["policy"]
    assert policy["parameters"] == {"max_width": 1, "max_refine": 1}
    assert policy["kind"] == "synthetic" and policy["promoted"] is False
    assert policy["publication_status"] == recovery.PUBLICATION_STATUS
    assert policy["validation_gain"] is policy["incumbent_gain"] is None
    assert recovery._inventory(campaign.root, skip=("journal.jsonl", "research-policy.json")) == before
    assert len([r for r in campaign.events() if r["event"] == "optimizer_finished"]) == 1
    assert len(campaign.summary()["trials"]) == 5 and not campaign.summary()["unknown_requests"]
    projected = json.loads((output / "available-tree-projection/manifest.json").read_text())
    assert len(projected["entries"]) == 4
    assert "HumanEval/32" not in {e["task_id"] for e in projected["entries"]}
    with pytest.raises(recovery.RecoveryError):
        recovery.recover(campaign.root, tmp_path / "must-not-research-again")
    assert len(fake_sdk) == 1


def test_baseline_choice_uses_registered_reference_fallback_not_runner_up(campaign, fake_sdk, monkeypatch, tmp_path):
    original = recovery.optimizer.optimize
    def baseline(*args, **kwargs):
        result = original(*args, **kwargs)
        result["train_scores"] = {role: 1 for role in result["train_scores"]}
        result["selected"] = {"role": "baseline", "parameters": None, "policy_hash": policy_identity(None)}
        return result
    monkeypatch.setattr(recovery.optimizer, "optimize", baseline)
    policy = recovery.recover(campaign.root, tmp_path / "baseline-recovery")["policy"]
    assert policy["selected_role"] == "baseline" and policy["selected_parameters"] is None
    assert policy["parameters"] == {"max_width": 2, "max_refine": 1}
    assert policy["online_parameter_rule"] == "online_reference_fallback"
    assert policy["C_condition"] == "reference_fallback_not_trained_intervention"


@pytest.mark.parametrize("missing", [("HumanEval/11", "HumanEval/32"), ("HumanEval/32", "HumanEval/45")])
def test_incomplete_train_or_no_available_holdout_cannot_recover(tmp_path, monkeypatch, missing):
    campaign = build_campaign(tmp_path / "bad-coverage", monkeypatch, missing)
    before = campaign.path.read_bytes()
    with pytest.raises(recovery.RecoveryError):
        recovery.inspect_recovery(campaign.root)
    assert campaign.path.read_bytes() == before


def test_no_second_search_after_sdk_error(campaign, fake_sdk, monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        fake_sdk.append("attempted")
        raise RuntimeError("synthetic SDK failure")
    monkeypatch.setattr(recovery.optimizer, "optimize", fail)
    with pytest.raises(RuntimeError):
        recovery.recover(campaign.root, tmp_path / "failed-recovery")
    with pytest.raises(recovery.RecoveryError):
        recovery.recover(campaign.root, tmp_path / "alternate-output")
    assert fake_sdk == ["attempted"] and not (campaign.root / "research-policy.json").exists()


def test_interrupted_commit_can_only_finish_same_sealed_candidate(campaign, fake_sdk, monkeypatch, tmp_path):
    original = live_runner.Campaign._append
    def interrupt(self, event, **kwargs):
        if event == "optimizer_finished":
            raise OSError("synthetic interrupted final commit")
        return original(self, event, **kwargs)
    monkeypatch.setattr(live_runner.Campaign, "_append", interrupt)
    output = tmp_path / "prepared-recovery"
    with pytest.raises(OSError):
        recovery.recover(campaign.root, output)
    assert (campaign.root / "research-policy.json").exists() and len(fake_sdk) == 1
    monkeypatch.setattr(live_runner.Campaign, "_append", original)
    assert recovery.complete_commit(campaign.root, output)["status"] == "committed"
    assert len(fake_sdk) == 1
    with pytest.raises(recovery.RecoveryError):
        recovery.complete_commit(campaign.root, output)


def test_test_start_is_rejected(campaign, fake_sdk, tmp_path):
    plan = live_runner.verify_plan(campaign)[0]
    test_slot = plan["slots"][5]
    campaign.start_trial(test_slot["trial_id"], metadata=test_slot)
    with pytest.raises(recovery.RecoveryError):
        recovery.recover(campaign.root, tmp_path / "not-allowed")
    assert not fake_sdk


def test_changed_original_final_seal_is_rejected(campaign, fake_sdk, tmp_path):
    slot = live_runner.verify_plan(campaign)[0]["slots"][0]
    path = campaign.root / "trials" / slot["trial_id"] / "final-evaluation.json"
    path.write_text('{"changed":true}')
    with pytest.raises(recovery.RecoveryError):
        recovery.recover(campaign.root, tmp_path / "not-allowed")
    assert not fake_sdk


def test_atomic_registration_rejects_competing_recovery_before_search(campaign, fake_sdk, monkeypatch, tmp_path):
    original = live_runner.Campaign._append
    def compete(self, event, **kwargs):
        if event == "operator_recovery" and kwargs.get("phase") == "started":
            original(self, "operator_recovery", recovery_id=recovery.RECOVERY_ID, phase="started", output="competing-output")
        return original(self, event, **kwargs)
    monkeypatch.setattr(live_runner.Campaign, "_append", compete)
    with pytest.raises(recovery.RecoveryError, match="another recovery"):
        recovery.recover(campaign.root, tmp_path / "not-allowed")
    assert not fake_sdk


@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None, reason="optional pinned Dream SDK required")
@pytest.mark.parametrize("first_success", [False, True])
def test_actual_sdk_recovery_is_synthetic_unpublished_and_keeps_validation_null(tmp_path, monkeypatch, first_success):
    campaign = build_campaign(tmp_path / "actual-sdk-fixture", monkeypatch, first_success=first_success)
    result = recovery.recover(campaign.root, tmp_path / "actual-sdk-recovery")
    assert result["policy"]["kind"] == "synthetic" and result["policy"]["promoted"] is False
    assert result["policy"]["validation_gain"] is result["policy"]["incumbent_gain"] is None
    sdk = json.loads((tmp_path / "actual-sdk-recovery/sdk-return.json").read_text())
    assert sdk["status"] == "completed" and sdk["kind"] == "synthetic"
    assert result["policy"]["publication_status"] == recovery.PUBLICATION_STATUS
    if first_success:
        assert sdk["selected"]["role"] == "baseline" and sdk["selected"]["parameters"] is None
        assert result["policy"]["raw_sdk_selected"] == sdk["selected"]
        assert result["policy"]["parameters"] == {"max_width": 2, "max_refine": 1}
        assert result["policy"]["C_condition"] == "reference_fallback_not_trained_intervention"
    else:
        assert result["policy"]["parameters"] == {"max_width": 1, "max_refine": 1}
