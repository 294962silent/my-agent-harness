"""Durability and recovery contracts; no real provider or GPU calls."""
import asyncio
import json

import pytest

from experiments.context_rsi.live_runner import Campaign, JournalError, JournaledParentBudget, preflight
from my_agent_llm import Config, Message, Response
from my_coding_agent.rsi_runtime import RsiBudgetExceeded, SharedLLM


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    from experiments.context_rsi.live_runner import _model_secret_names
    for name in _model_secret_names():
        monkeypatch.delenv(name)


@pytest.fixture
def campaign(tmp_path):
    legacy = tmp_path / "old-ledger.json"
    legacy.write_text('{"requests": [{"status": "unknown", "held": 0.045131}]}')
    result = Campaign.freeze(tmp_path / "new-campaign", campaign_id="rsi-live-20261114", legacy_ledger=legacy)
    assert legacy.read_text() == '{"requests": [{"status": "unknown", "held": 0.045131}]}'
    return result


def test_frozen_authorization_and_no_overwrite(campaign):
    manifest = campaign.manifest
    assert manifest["authorization"]["claimed_tokens"] == 2_000_000_000
    assert manifest["authorization"]["provider_verified"] is False
    assert manifest["historical_campaign"]["unknown_reserved_usd"] == "0.045131"
    assert manifest["limits"] == {"max_requests": 64, "max_tokens": 1000000, "max_seconds": 1800, "max_evaluations": 6}
    assert manifest["protocol_id"] == "rsi-humaneval-calibration-v2-turn12"
    assert manifest["attempt_max_turns"] == 12
    assert manifest["context_compaction"] == {"model_context_window": 32000, "output_reserve": 4096,
        "safety_margin": 512, "summary_max_tokens": 1024}
    with pytest.raises(FileExistsError):
        Campaign.freeze(campaign.root, campaign_id="rsi-live-20261114", legacy_ledger="unused")


def test_persistent_reservation_precedes_dispatch_and_usage_is_authoritative(campaign):
    campaign.start_trial("trial-1", metadata={"task_id": "task-1"})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    class Raw:
        model = "synthetic-protocol-only"
        config = Config(provider="openai", model=model, max_tokens=256, max_retries=0)
        async def achat(self, messages, **kwargs):
            rows = campaign.events()
            assert rows[-1]["event"] == "request_reserved"
            assert rows[-1]["record"]["reserved_tokens"] > 0
            return Response(model=self.model, content="fixture", usage={"prompt_tokens": 962,
                "completion_tokens": 195, "total_tokens": 1413, "cache_read_tokens": 256})
    asyncio.run(SharedLLM(Raw(), budget).achat([Message(role="user", content="x" * 2000)]))
    row = campaign.events()[-1]
    assert row["event"] == "request_settled" and row["record"]["input_tokens"] == 1218
    assert row["raw_usage"]["cache_read_tokens"] == 256
    assert campaign.summary()["used_tokens"] == 1413
    assert campaign.summary()["currency"] is None


def test_unknown_holds_and_blocks_other_trial_ids(campaign):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    record = budget.reserve([], None, 64, "agent")
    with pytest.raises(RsiBudgetExceeded):
        budget.settle(record, None, error="CancelledError")
    summary = Campaign(campaign.root).summary()
    assert summary["blocked"] and summary["held_tokens"] == record["reserved_tokens"]
    assert summary["unknown_requests"] == 1
    with pytest.raises(JournalError):
        campaign.start_trial("new-id-must-not-bypass", metadata={})


def test_crash_after_reservation_is_never_retried(campaign):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    budget.reserve([], None, 64, "agent")
    recovered = Campaign(campaign.root)
    assert recovered.resume()["action"] == "blocked_manual_review"
    assert recovered.summary()["pending_requests"] == 1
    for trial in ("trial-1", "different-id"):
        with pytest.raises(JournalError):
            recovered.start_trial(trial, metadata={})


def test_failed_durable_reserve_never_calls_model(campaign, monkeypatch):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    def unavailable(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(campaign, "record_reservation", unavailable)
    class Raw:
        model = "synthetic"
        config = Config(provider="openai", model=model, max_tokens=64)
        calls = 0
        async def achat(self, messages, **kwargs):
            self.calls += 1
    raw = Raw()
    with pytest.raises(JournalError):
        asyncio.run(SharedLLM(raw, budget).achat([]))
    assert raw.calls == 0 and budget.blocked
    assert Campaign(campaign.root).resume()["action"] == "blocked_manual_review"


def test_torn_or_changed_journal_and_manifest_fail_closed(campaign):
    path = campaign.root / "journal.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    with pytest.raises(JournalError):
        Campaign(campaign.root).summary()


def test_completed_trial_cannot_be_overwritten(campaign):
    campaign.start_trial("trial-1", metadata={})
    campaign.finish_trial("trial-1", status="completed", evidence={})
    with pytest.raises(JournalError):
        campaign.start_trial("trial-1", metadata={})
    campaign.start_trial("trial-2", metadata={})
    assert campaign.summary()["trials"]["trial-2"]["status"] == "started"


def test_preflight_does_not_construct_or_call_provider(campaign):
    result = preflight(campaign)
    assert result["network_calls"] == 0 and result["model"]["max_retries"] == 0
    assert result["ready_to_dispatch"] is False
    assert result["not_ready_reason"] == "no_frozen_plan"
    assert "api_key" not in json.dumps(result)


def test_preflight_ready_checks_real_temporary_task_material_without_dispatch(campaign, prepared_contract, tmp_path, monkeypatch):
    from experiments.context_rsi import live_runner
    manifest, tasks = prepared_contract
    checked = []
    for task in tasks:
        directory = tmp_path / task.task_id.replace("/", "-")
        directory.mkdir()
        path = directory / "solution.py"
        path.write_text("def temporary_" + task.task_id.replace("/", "_") + "():\n    pass\n")
        task.source_sha = live_runner._sha(path.read_bytes())
        def verify(path=path, expected=task.source_sha):
            checked.append(path)
            if live_runner._sha(path.read_bytes()) != expected:
                raise ValueError("synthetic task material drift")
        task.verify = verify
    live_runner.freeze_plan(campaign, manifest)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not construct credentials, run a task or optimize")
    monkeypatch.setattr(live_runner, "construct_live_llm", forbidden)
    monkeypatch.setattr(live_runner, "run_batch", forbidden)
    monkeypatch.setattr(live_runner, "optimize_collection", forbidden)
    result = preflight(campaign)
    assert result["ready_to_dispatch"] is True and result["phase"] == "needs_training"
    assert result["network_calls"] == 0 and len(checked) == 11
    checked[0].write_text("changed")
    changed = preflight(campaign)
    assert changed["ready_to_dispatch"] is False
    assert changed["not_ready_reason"] == "invalid_frozen_plan_or_task_material"
    assert campaign.summary()["requests"] == 0


@pytest.mark.parametrize("stage", ["optimization", "testing", "complete", "optimizer_interrupted"])
def test_preflight_reports_phase_without_running_optimizer(campaign, prepared_contract, stage):
    from experiments.context_rsi import live_runner
    manifest, _ = prepared_contract
    plan = live_runner.freeze_plan(campaign, manifest)
    slots = plan["slots"] if stage == "complete" else plan["slots"][:5]
    for slot in slots:
        campaign.start_trial(slot["trial_id"], metadata=slot)
        campaign.finish_trial(slot["trial_id"], status="completed", evidence={"kind": "synthetic_no_model"})
    if stage in ("testing", "complete", "optimizer_interrupted"):
        campaign._append("optimizer_started", kind="synthetic_phase_fixture")
    if stage in ("testing", "complete"):
        policy = {"parameters": {"max_width": 1, "max_refine": 1}, "kind": "synthetic_phase_fixture"}
        live_runner._exclusive_json(campaign.root / "research-policy.json", policy)
        campaign._append("optimizer_finished", policy_sha256=live_runner._sha(live_runner._bytes(policy)))
    result = preflight(campaign)
    assert result["phase"] == ("optimization" if stage == "optimizer_interrupted" else stage)
    assert result["ready_to_dispatch"] is (stage in ("optimization", "testing"))
    assert result["network_calls"] == 0 and campaign.summary()["requests"] == 0
    if stage == "optimizer_interrupted":
        assert result["not_ready_reason"] == "optimizer_started_without_terminal_evidence"
    if stage == "testing":
        (campaign.root / "research-policy.json").write_text('{"changed": true}')
        assert preflight(campaign)["not_ready_reason"] == "invalid_frozen_plan_or_task_material"


def test_preflight_blocks_started_unknown_trial_without_retry(campaign, prepared_contract):
    from experiments.context_rsi import live_runner
    manifest, _ = prepared_contract
    plan = live_runner.freeze_plan(campaign, manifest)
    slot = plan["slots"][0]
    campaign.start_trial(slot["trial_id"], metadata=slot)
    budget = JournaledParentBudget(campaign, slot["trial_id"], verify_task=lambda: None)
    record = budget.reserve([], None, 64, "agent")
    with pytest.raises(RsiBudgetExceeded):
        budget.settle(record, None, error="SyntheticCancelled")
    result = preflight(campaign)
    assert result["ready_to_dispatch"] is False and result["phase"] == "needs_training"
    assert result["not_ready_reason"] == "campaign_recovery_blocked"
    assert result["recovery"]["summary"]["unknown_requests"] == 1


def test_failed_settlement_persistence_stays_pending_after_recovery(campaign, monkeypatch):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    record = budget.reserve([], None, 64, "agent")
    def failed(*args, **kwargs):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(campaign, "record_settlement", failed)
    with pytest.raises(JournalError):
        budget.settle(record, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    assert budget.blocked
    recovered = Campaign(campaign.root)
    assert recovered.summary()["pending_requests"] == 1
    assert recovered.summary()["used_tokens"] == 0  # Unknown is held, never asserted as a zero-use request.
    assert recovered.summary()["held_tokens"] > 0
    with pytest.raises(JournalError):
        recovered.start_trial("other", metadata={})


def test_provider_cancellation_is_durable_unknown(campaign):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    class Cancelled:
        model = "synthetic-protocol-only"
        config = Config(provider="openai", model=model, max_tokens=64)
        async def achat(self, messages, **kwargs):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(SharedLLM(Cancelled(), budget).achat([]))
    assert Campaign(campaign.root).summary()["unknown_requests"] == 1


def test_known_output_violation_is_settled_but_blocks_campaign(campaign):
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    record = budget.reserve([], None, 2, "agent")
    with pytest.raises(RsiBudgetExceeded):
        budget.settle(record, {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4})
    summary = campaign.summary()
    assert summary["blocked"] and summary["used_tokens"] == 4 and summary["held_tokens"] == 0
    campaign.finish_trial("trial-1", status="failed", evidence={})
    with pytest.raises(JournalError):
        campaign.start_trial("other", metadata={})


def test_manifest_mutation_is_detected(campaign):
    manifest = json.loads(campaign.manifest_path.read_text())
    manifest["campaign_id"] = "changed"
    campaign.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(JournalError):
        campaign.summary()


def test_changed_execution_source_blocks_before_provider_dispatch(campaign, monkeypatch):
    from experiments.context_rsi import live_runner
    campaign.start_trial("trial-1", metadata={})
    budget = JournaledParentBudget(campaign, "trial-1", verify_task=lambda: None)
    monkeypatch.setattr(live_runner, "_execution_fingerprint", lambda: {"changed.py": "changed"})
    with pytest.raises(JournalError):
        budget.reserve([], None, 64, "agent")
    assert budget.blocked and not any(r["event"] == "request_reserved" for r in campaign.events())


@pytest.fixture
def prepared_contract(campaign, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from experiments.context_rsi import live_tasks
    tasks = []
    for split, numbers in (("train", (7, 11, 27)), ("val", (32, 45)), ("test", (1, 6, 9, 20, 33, 40))):
        for number in numbers:
            tasks.append(SimpleNamespace(task_id=f"HumanEval/{number}", split=split, source_sha="a" * 64,
                host_evaluator_sha="b" * 64, preflight_proof={"kind": "synthetic_test_fixture"}, verify=lambda: None))
    manifest = tmp_path / "synthetic-task-contract.json"
    manifest.write_text('{"fixture": "synthetic schema only, not official task validation"}')
    monkeypatch.setattr(live_tasks, "load_tasks", lambda path: tasks)
    return manifest, tasks


def test_batch_plan_is_frozen_interleaved_and_has_53_slots(campaign, prepared_contract):
    from experiments.context_rsi.live_runner import freeze_plan, verify_plan
    manifest, _ = prepared_contract
    plan = freeze_plan(campaign, manifest)
    assert len(plan["slots"]) == 53
    assert [s["split"] for s in plan["slots"][:5]] == ["train"] * 3 + ["val"] * 2
    for index in range(5, 53, 4):
        group = plan["slots"][index:index + 4]
        assert {s["arm"] for s in group} == {"A", "B", "C", "random"}
        assert len({(s["task_id"], s["seed"]) for s in group}) == 1
    assert verify_plan(campaign)[0] == plan
    with pytest.raises(FileExistsError):
        freeze_plan(campaign, manifest)


def test_batch_max_one_continues_only_next_unstarted_slot(campaign, prepared_contract, monkeypatch):
    from experiments.context_rsi import live_runner
    manifest, _ = prepared_contract
    plan = live_runner.freeze_plan(campaign, manifest)
    called = []
    async def synthetic_trial(**kwargs):
        trial = kwargs["trial_id"]
        called.append(trial)
        campaign.start_trial(trial, metadata=next(slot for slot in plan["slots"] if slot["trial_id"] == trial))
        campaign.finish_trial(trial, status="completed", evidence={})
    monkeypatch.setattr(live_runner, "run_prepared_trial", synthetic_trial)
    first = asyncio.run(live_runner.run_batch(campaign, max_new_trials=1))
    second = asyncio.run(live_runner.run_batch(Campaign(campaign.root), max_new_trials=1))
    assert first["new_trials"] == second["new_trials"] == 1
    assert called == [s["trial_id"] for s in plan["slots"][:2]]
    assert campaign.summary()["requests"] == 0


def test_mutated_task_manifest_cannot_continue_plan(campaign, prepared_contract):
    from experiments.context_rsi.live_runner import freeze_plan, verify_plan
    manifest, _ = prepared_contract
    freeze_plan(campaign, manifest)
    manifest.write_text('{"changed": true}')
    with pytest.raises(JournalError):
        verify_plan(campaign)


def test_failed_trial_cannot_restart_under_another_id(campaign):
    campaign.start_trial("first", metadata={"task_id": "same"})
    campaign.finish_trial("first", status="failed", evidence={})
    assert campaign.summary()["blocked"]
    with pytest.raises(JournalError):
        campaign.start_trial("different", metadata={"task_id": "same"})


def test_task_drift_blocks_every_payment_and_evaluation(campaign):
    campaign.start_trial("first", metadata={})
    changed = False
    def verify():
        if changed:
            raise ValueError("changed task private material")
    budget = JournaledParentBudget(campaign, "first", verify_task=verify)
    request = budget.reserve([], None, 10, "agent")
    budget.settle(request, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    changed = True
    with pytest.raises(JournalError):
        budget.reserve([], None, 10, "agent")
    assert campaign.summary()["requests"] == 1
    with pytest.raises(JournalError):
        budget.begin_evaluation()
    assert budget.evaluations == []
    with pytest.raises(JournalError):
        campaign.start_trial("alternate", metadata={})


def test_budget_requires_explicit_task_verifier(campaign):
    campaign.start_trial("first", metadata={})
    with pytest.raises((TypeError, JournalError)):
        JournaledParentBudget(campaign, "first")


def test_unregistered_slot_or_wrong_tuple_rejected(campaign, prepared_contract):
    from experiments.context_rsi.live_runner import freeze_plan
    manifest, _ = prepared_contract
    plan = freeze_plan(campaign, manifest)
    slot = plan["slots"][0]
    with pytest.raises(JournalError):
        campaign.start_trial("alternate-id", metadata=slot)
    wrong = dict(slot, arm="random")
    with pytest.raises(JournalError):
        campaign.start_trial(slot["trial_id"], metadata=wrong)


@pytest.mark.parametrize("name", ["LIVE_BENCH_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"])
def test_library_rejects_dirty_model_secret_environment_before_task_or_provider(campaign, monkeypatch, name):
    from experiments.context_rsi import live_runner
    monkeypatch.setenv(name, "fake-secret-never-log")
    with pytest.raises(JournalError) as error:
        asyncio.run(live_runner.run_prepared_trial(campaign=campaign, trial_id="first", task=None, arm="A"))
    assert "fake-secret" not in str(error.value)
    assert not campaign.summary()["trials"]


def test_cli_constructs_model_before_scrubbing_tool_environment(campaign, monkeypatch, capsys):
    from experiments.context_rsi import live_runner
    marker = object()
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "fake-cli-only")
    def construct():
        assert live_runner.os.environ["LIVE_BENCH_API_KEY"] == "fake-cli-only"
        return marker
    async def batch(campaign, **kwargs):
        assert kwargs["llm"] is marker
        assert "LIVE_BENCH_API_KEY" not in live_runner.os.environ
        return {"fixture": True}
    monkeypatch.setattr(live_runner, "construct_live_llm", construct)
    monkeypatch.setattr(live_runner, "run_batch", batch)
    live_runner.main(["run", "--root", str(campaign.root)])
    assert "fake-cli-only" not in capsys.readouterr().out


@pytest.mark.parametrize("material", ["solution.py", "host-evaluator.py", "private-task.json"])
def test_each_material_drift_blocks_first_score_without_provider_dispatch(campaign, tmp_path, material):
    from experiments.context_rsi.live_tasks import _check, _sha
    paths = [tmp_path / name for name in ("solution.py", "host-evaluator.py", "private-task.json")]
    for path in paths:
        path.write_text("original fixture material")
    hashes = {path: _sha(path) for path in paths}
    def verify():
        for path, digest in hashes.items():
            _check(path, digest)
    campaign.start_trial("first", metadata={})
    budget = JournaledParentBudget(campaign, "first", verify_task=verify)
    (tmp_path / material).write_text("changed fixture material")
    with pytest.raises(JournalError):
        budget.begin_evaluation()
    assert budget.evaluations == [] and campaign.summary()["requests"] == 0
    assert budget.blocked


def test_known_quota_guard_is_durable_host_completion_not_task_success(campaign):
    from experiments.context_rsi.live_runner import _episode_integrity
    campaign.start_trial("first", metadata={})
    budget = JournaledParentBudget(campaign, "first", verify_task=lambda: None)
    budget.max_tokens = 0
    with pytest.raises(RsiBudgetExceeded):
        budget.reserve([], None, 1, "agent")
    assert campaign.events()[-1]["event"] == "budget_stopped"
    assert not budget.blocked and budget.snapshot()["usage_complete"]
    report = {"status": "invalid", "cleanup_errors": []}
    assert _episode_integrity(report, budget)
    campaign.finish_trial("first", status="completed", evidence={"agent_task_status": "invalid", "success": False})
    campaign.start_trial("next", metadata={})


@pytest.mark.parametrize("status,terminal,expected", [
    ("completed", None, True), ("budget_exhausted", None, True),
    ("invalid_attempt", "budget_exhausted", True), ("invalid_attempt", "error", False),
    ("invalid", None, False), ("cancelled", None, False), ("cleanup_failed", None, False)])
def test_episode_completion_does_not_conflate_agent_failure_or_unknown(status, terminal, expected):
    from types import SimpleNamespace
    from experiments.context_rsi.live_runner import _episode_integrity
    budget = SimpleNamespace(blocked=False, quota_stop=None, snapshot=lambda: {"usage_complete": True})
    report = {"status": status, "cleanup_errors": [], "attempts": [{"agent_terminal": {"stop_reason": terminal}}]}
    assert _episode_integrity(report, budget) is expected
    budget.blocked = True
    assert not _episode_integrity(report, budget)


def test_prepared_adapter_only_projects_public_prompt_examples(campaign, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from experiments.context_rsi import live_runner
    from my_coding_agent.dream_rsi_contract import validate_task_evidence
    source = tmp_path / "source"
    source.mkdir()
    prompt = 'def solve(x):\n    """Example.\n    >>> solve(1)\n    2\n    """\n'
    (source / "solution.py").write_bytes(prompt.encode("utf-8"))
    task = SimpleNamespace(source_dir=source, entry_point="solve", verify=lambda: None,
        task_id="HumanEval/7", split="train", goal=prompt, visible_argv=("python", "visible"),
        final_argv=("python", "PRIVATE_FINAL_SENTINEL"), source_sha="a" * 64,
        host_evaluator_sha="b" * 64, preflight_proof={})
    async def capture(**kwargs):
        proof = kwargs["task_evidence"]
        validate_task_evidence(proof)
        assert proof["public_input"] == prompt
        assert "solve(1)" in proof["visible_spec"]
        assert "PRIVATE_FINAL_SENTINEL" not in json.dumps(proof)
        assert kwargs["verify_task"] == task.verify
        return "captured-no-provider"
    monkeypatch.setattr(live_runner, "run_trial", capture)
    assert asyncio.run(live_runner.run_prepared_trial(campaign=campaign, trial_id="first", task=task, arm="B")) == "captured-no-provider"


def test_final_known_quota_records_unscored_terminal_without_failing_host(campaign, tmp_path):
    import sys
    from experiments.context_rsi.live_runner import _postselection_evaluate
    campaign.start_trial("first", metadata={})
    budget = JournaledParentBudget(campaign, "first", verify_task=lambda: None)
    budget.max_evaluations = 0
    final = asyncio.run(_postselection_evaluate((sys.executable, "-c", "raise RuntimeError('must not run')"), tmp_path, budget))
    assert final == {"valid": False, "score": None, "status": "not_run_parent_quota_exhausted"}
    assert not budget.evaluations and not budget.blocked
    assert campaign.events()[-1]["event"] == "budget_stopped"


@pytest.mark.parametrize("turns", [4, 12])
def test_five_request_read_edit_test_endturn_consumes_frozen_v2_turn_limit(campaign, tmp_path, turns):
    import os
    import shlex
    import subprocess
    import sys
    from experiments.context_rsi.live_runner import _branch_factory
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime
    source = tmp_path / "five-turn-source"
    source.mkdir()
    (source / "solution.py").write_text("value = 0\n")
    argv = [sys.executable, "-c", "from pathlib import Path; assert 'value = 42' in Path('solution.py').read_text()"]
    bash_argv = [*argv[:2], argv[2] + "; Path('tested.flag').write_text('ok')"]
    command = subprocess.list2cmdline(bash_argv) if os.name == "nt" else shlex.join(bash_argv)
    steps = [("read", {"path": "solution.py"}), ("read", {"path": "solution.py"}),
        ("edit", {"path": "solution.py", "edits": [{"oldText": "value = 0", "newText": "value = 42"}]}),
        ("bash", {"command": command})]
    class FiveRequests:
        model = "synthetic-five-request-turn-contract"
        config = Config(provider="openai", model=model, max_tokens=64, max_retries=0)
        def __init__(self):
            self.calls = 0
        async def achat(self, messages, **kwargs):
            self.calls += 1
            usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            if self.calls == 5:
                return Response(model=self.model, content="Read, edited and tested; done.", usage=usage)
            name, args = steps[self.calls - 1]
            return Response(model=self.model, content="", usage=usage, tool_calls=[{
                "id": f"step-{self.calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])
    campaign.start_trial("turn-contract", metadata={"kind": "synthetic_no_provider"})
    budget = JournaledParentBudget(campaign, "turn-contract", verify_task=lambda: None)
    if turns == 12:
        factory = _branch_factory(campaign, source=source, output=tmp_path / "branch-output", mode="layered")
        assert factory.attempt_max_turns == campaign.manifest["attempt_max_turns"] == 12
        assert factory.compaction_policy.output_reserve == 4096
        assert factory.compaction_policy.safety_margin == 512
        assert factory.compaction_policy.summary_max_tokens == 1024
    else:
        # Explicit offline 4-turn control, never a paid campaign or an archived v1 edit.
        factory = LocalBranchFactory(source, tmp_path / "branch-output", attempt_max_turns=4)
    raw = FiveRequests()
    runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator(tuple(argv)), budget=budget,
        policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id="five-request-contract", split="train", max_attempts=1)
    result = asyncio.run(runtime.run("Read solution.py, edit value and test it, then finish.", llm=raw))
    terminal = result.report["attempts"][0]["agent_terminal"]
    assert raw.calls == (5 if turns == 12 else 4)
    assert budget.snapshot()["limits"] == campaign.manifest["limits"]
    assert budget.snapshot()["usage_complete"] and budget.snapshot()["held_tokens"] == 0
    assert terminal["stop_reason"] == ("end_turn" if turns == 12 else "budget_exhausted")
    assert (factory.handles[0].workspace / "tested.flag").read_text() == "ok"
    assert result.final_success is None
    if turns == 12:
        assert result.selected_visible_score == 1 and result.report["valid_attempts"] == 1
        assert (result.selected_path / "solution.py").read_text() == "value = 42\n"
    else:
        assert result.report["status"] == "invalid_attempt" and result.selected_path is None
