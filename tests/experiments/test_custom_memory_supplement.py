"""Offline contracts for the custom-memory v3 sidecar."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

_WSL_RUNTIME = (
    os.name == "posix"
    and importlib.util.find_spec("harbor") is not None
    and Path("/mnt/c/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/bench-proxy.py").is_file()
)
pytestmark = pytest.mark.skipif(not _WSL_RUNTIME, reason="requires WSL Docker/Harbor")

if _WSL_RUNTIME:
    from experiments.long_horizon_code import custom_memory_supplement as sidecar
    from experiments.long_horizon_code import public_pilot as pilot
else:
    sidecar = pilot = None


def _plan():
    slots = []
    for index in range(9):
        slots.append({"task": "other", "arm": f"other-{index}", "run_id": f"other-{index}"})
    for index, arm in zip((6, 7, 8), ("loo_artifact", "baseline", "enhanced"), strict=True):
        slots[index] = {
            "task": sidecar.TASK,
            "arm": arm,
            "run_id": f"custom-{arm}",
            "limits": {"max_seconds": 1680},
            "oracle_gate": {"task_name": "terminal-bench/custom-memory-heap-crash"},
        }
    return {"slots": slots}


def test_custom_slots_are_exact_frozen_order():
    slots = sidecar._custom_slots(_plan())
    assert [slot["arm"] for slot in slots] == ["loo_artifact", "baseline", "enhanced"]
    with pytest.raises(pilot.PilotStop):
        sidecar._custom_slots({"slots": list(reversed(_plan()["slots"]))})


def test_stale_sidecar_receipt_is_rejected(tmp_path):
    output = tmp_path / "campaign"
    (output / "readiness").mkdir(parents=True)
    pilot.write_sealed(output / "readiness/custom-preflight.json", {
        "status": "ready", "plan_sha256": "fixture", "sidecar_sha256": "stale"
    })
    with pytest.raises(pilot.PilotStop, match="stale"):
        sidecar._preflight_receipt(output, "fixture")


def test_run_rechecks_credentials_before_using_ready_receipt(tmp_path, monkeypatch):
    output = tmp_path / "campaign"
    (output / "readiness").mkdir(parents=True)
    plan = _plan()
    plan["runtime_proxy"] = "http://127.0.0.1:7897"
    ledger_path = tmp_path / "ledger.json"
    plan["ledger"] = str(ledger_path)
    book = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1)
    for slot in sidecar._custom_slots(plan):
        book.register_run(slot["run_id"], max_requests=64, time_cap=1680)
    pilot.write_sealed(output / "readiness/preflight.json", {
        "status": "ready", "plan_sha256": "fixture"
    })
    monkeypatch.setattr(pilot, "validate_plan", lambda *_args, **_kwargs: (output, plan, book))
    monkeypatch.setattr(pilot, "_validate_oracle_receipt", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(pilot, "_validate_transport_receipt", lambda *_args, **_kwargs: {"status": "pass"})
    monkeypatch.setattr(pilot, "langfuse_status", lambda _plan: {
        "ready": True, "env_presence": {}, "base_url_matches": True,
        "host_matches": True, "base_url": "https://cloud.langfuse.com",
    })
    monkeypatch.setattr(pilot, "proxy_reachable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        pilot, "install_live_credential",
        lambda: (_ for _ in ()).throw(pilot.PilotStop("credential unavailable")),
    )
    calls = []
    with pytest.raises(pilot.PilotStop, match="credential unavailable"):
        sidecar.run(
            output,
            expected_sha="fixture",
            execute=True,
            max_new_trials=1,
            executor=lambda *args: calls.append(args),
        )
    assert calls == []
    assert not (output / "journal-custom.json").exists()
    snapshot = book.snapshot()
    assert not any(run.get("trial_started") for run in snapshot["runs"].values())


def test_run_never_reexecutes_finished_or_consumed_run(tmp_path, monkeypatch):
    output = tmp_path / "campaign"
    output.mkdir()
    plan = _plan()
    ledger_path = tmp_path / "ledger.json"
    plan["ledger"] = str(ledger_path)
    book = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1)
    for slot in sidecar._custom_slots(plan):
        book.register_run(slot["run_id"], max_requests=64, time_cap=1680)
    first = sidecar._custom_slots(plan)[0]
    with book.transaction() as data:
        data["runs"][first["run_id"]]["trial_started"] = True
    first_dir = output / f"custom-06-{sidecar.TASK}-{first['arm']}"
    first_dir.mkdir()
    (output / "journal-custom.json").write_text(json.dumps({
        first["run_id"]: {"status": "finished", "category": "scored"}
    }))
    main_journal = {"untouched": {"status": "finished"}}
    (output / "journal.json").write_text(json.dumps(main_journal))
    calls = []
    monkeypatch.setattr(sidecar, "preflight", lambda *_args, **_kwargs: {
        "oracle_results": {sidecar.TASK: "/tmp/oracle.json"}
    })
    monkeypatch.setattr(pilot, "validate_plan", lambda *_args, **_kwargs: (output, plan, book))
    monkeypatch.setattr(pilot, "trial_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pilot, "_trial_config_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "_adjudicate", lambda *_args, **_kwargs: {
        "status": "finished", "category": "scored", "official_reward": 0,
        "usage_complete": True, "schema_sha256": "same",
        "observation_mode": "langfuse", "observation_required": True,
        "observation_ready": True,
    })
    monkeypatch.setattr(sidecar, "summary", lambda _output: {"ok": True})
    result = sidecar.run(
        output,
        expected_sha="fixture",
        execute=True,
        max_new_trials=1,
        executor=lambda config, directory, timeout: calls.append((config, directory, timeout)),
    )
    assert result == {"ok": True}
    assert len(calls) == 1
    assert calls[0][1].name == f"custom-07-{sidecar.TASK}-baseline"
    assert json.loads((output / "journal.json").read_text()) == main_journal
    journal = json.loads((output / "journal-custom.json").read_text())
    assert journal[first["run_id"]]["status"] == "finished"
    assert len(journal) == 2


def test_run_rechecks_full_gate_before_each_new_trial(tmp_path, monkeypatch):
    output = tmp_path / "campaign"
    output.mkdir()
    plan = _plan()
    ledger_path = tmp_path / "ledger.json"
    plan["ledger"] = str(ledger_path)
    book = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1)
    for slot in sidecar._custom_slots(plan):
        book.register_run(slot["run_id"], max_requests=64, time_cap=1680)
    events = []

    def gate(*_args, **kwargs):
        events.append("preflight-locked" if kwargs.get("acquire_lock", True) else "preflight-unlocked")
        return {"oracle_results": {sidecar.TASK: "/tmp/oracle.json"}}

    monkeypatch.setattr(sidecar, "preflight", gate)
    monkeypatch.setattr(pilot, "validate_plan", lambda *_args, **_kwargs: (output, plan, book))
    monkeypatch.setattr(pilot, "trial_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pilot, "_trial_config_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "_adjudicate", lambda *_args, **_kwargs: {
        "status": "finished", "category": "scored", "official_reward": 1,
        "usage_complete": True, "schema_sha256": "same",
        "observation_mode": "langfuse", "observation_required": True,
        "observation_ready": True,
    })
    monkeypatch.setattr(sidecar, "summary", lambda _output: {"ok": True})

    def execute(*args):
        events.append("execute")

    sidecar.run(
        output,
        expected_sha="fixture",
        execute=True,
        max_new_trials=2,
        executor=execute,
    )
    assert events == [
        "preflight-locked",
        "preflight-unlocked",
        "execute",
        "preflight-unlocked",
        "execute",
    ]


def test_observation_failure_stops_before_next_trial(tmp_path, monkeypatch):
    output = tmp_path / "campaign"
    output.mkdir()
    plan = _plan()
    ledger_path = tmp_path / "ledger.json"
    plan["ledger"] = str(ledger_path)
    book = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1)
    for slot in sidecar._custom_slots(plan):
        book.register_run(slot["run_id"], max_requests=64, time_cap=1680)
    events = []
    monkeypatch.setattr(sidecar, "preflight", lambda *_args, **_kwargs: {
        "oracle_results": {sidecar.TASK: "/tmp/oracle.json"}
    })
    monkeypatch.setattr(pilot, "validate_plan", lambda *_args, **_kwargs: (output, plan, book))
    monkeypatch.setattr(pilot, "trial_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pilot, "_trial_config_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "_adjudicate", lambda *_args, **_kwargs: {
        "status": "finished", "category": "scored", "official_reward": 1,
        "usage_complete": True, "schema_sha256": "same",
        "observation_mode": "langfuse", "observation_required": True,
        "observation_ready": False,
    })
    monkeypatch.setattr(sidecar, "summary", lambda _output: {"ok": True})

    def execute(*args):
        events.append("execute")

    sidecar.run(
        output,
        expected_sha="fixture",
        execute=True,
        max_new_trials=3,
        executor=execute,
    )
    assert events == ["execute"]
    journal = json.loads((output / "journal-custom.json").read_text())
    assert list(journal.values())[0]["observation_ready"] is False


def test_context_budget_stop_is_terminal_but_does_not_block_next_arm(tmp_path, monkeypatch):
    output = tmp_path / "campaign"
    output.mkdir()
    plan = _plan()
    ledger_path = tmp_path / "ledger.json"
    plan["ledger"] = str(ledger_path)
    book = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1)
    for slot in sidecar._custom_slots(plan):
        book.register_run(slot["run_id"], max_requests=64, time_cap=1680)
    first = sidecar._custom_slots(plan)[0]
    terminal = {
        "status": "finished",
        "category": "execution_or_verifier_error",
        "run_status": "ContextBudgetExceeded",
        "usage_complete": True,
        "suspected_verifier_infra": False,
        "observation_mode": "langfuse",
        "observation_required": True,
        "observation_ready": True,
        "schema_sha256": "same",
    }
    (output / "journal-custom.json").write_text(json.dumps({first["run_id"]: terminal}))
    calls = []
    monkeypatch.setattr(sidecar, "preflight", lambda *_args, **_kwargs: {
        "oracle_results": {sidecar.TASK: "/tmp/oracle.json"}
    })
    monkeypatch.setattr(pilot, "validate_plan", lambda *_args, **_kwargs: (output, plan, book))
    monkeypatch.setattr(pilot, "trial_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pilot, "_trial_config_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "_adjudicate", lambda *_args, **_kwargs: dict(terminal))
    monkeypatch.setattr(sidecar, "summary", lambda _output: {"ok": True})

    sidecar.run(
        output,
        expected_sha="fixture",
        execute=True,
        max_new_trials=1,
        executor=lambda config, directory, timeout: calls.append(directory),
    )
    assert [path.name for path in calls] == [
        f"custom-07-{sidecar.TASK}-baseline"
    ]


def test_unknown_unscored_stop_blocks_custom_state():
    state = {
        "status": "finished",
        "category": "execution_or_verifier_error",
        "run_status": "UnknownFailure",
        "usage_complete": True,
        "suspected_verifier_infra": False,
        "observation_mode": "langfuse",
        "observation_required": True,
        "observation_ready": True,
    }
    assert sidecar._state_should_block(state)
    state["run_status"] = "ContextBudgetExceeded"
    assert not sidecar._state_should_block(state)
