"""Offline frozen-plan and no-rerun contracts; no Docker or paid API."""
import hashlib
import json

import pytest

from experiments.benchmark_live import long_horizon as pilot
from experiments.benchmark_live.budget import BudgetLedger


def fixture(tmp_path):
    root = tmp_path / "tasks"
    entries, oracles = [], {}
    for name in pilot.TASKS:
        task = root / name
        task.mkdir(parents=True)
        for relative, content in {
            "instruction.md": "Do official work",
            "task.toml": '[agent]\ntimeout_sec=900\n[environment]\ncpus=1\nmemory_mb=2048\ngpus=0\n',
        }.items():
            data = content.encode()
            (task / relative).write_bytes(data)
            entries.append({"path": f"tasks/{name}/{relative}", "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "git_blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
                            "mode": "100644"})
        oracle = tmp_path / f"{name}.json"
        oracle.write_text(json.dumps({"task_name": name, "agent_info": {"name": "oracle"},
                                     "verifier_result": {"rewards": {"reward": 1}},
                                     "task_checksum": "official-fixture", "finished_at": "now"}))
        oracles[name] = str(oracle)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"revision": pilot.REVISION, "source": pilot.SOURCE, "entries": entries}))
    mapping = tmp_path / "oracles.json"
    mapping.write_text(json.dumps(oracles))
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    output = tmp_path / "pilot"
    plan = pilot.freeze(output, tasks_root=root, manifest=manifest, oracle_map=mapping,
                        ledger=ledger, campaign_id="test-code", spend_ceiling_usd=15)
    return output, plan


def test_freeze_balanced_and_preview_does_not_register(tmp_path):
    output, plan = fixture(tmp_path)
    assert len(plan["slots"]) == 16
    assert sum(s["arm"] == "on" for s in plan["slots"][::2]) == 4
    assert all(s["limits"]["max_seconds"] == 900 for s in plan["slots"])
    before = open(plan["ledger"]).read()
    pilot.run(output, expected_sha=pilot.sha_file(output / "plan.json"))
    assert open(plan["ledger"]).read() == before
    assert not (output / "journal.json").exists()


def test_official_mutation_blocks_even_preview(tmp_path):
    output, plan = fixture(tmp_path)
    (pilot.Path(plan["tasks_root"]) / pilot.TASKS[0] / "instruction.md").write_text("changed")
    with pytest.raises(ValueError):
        pilot.run(output, expected_sha=pilot.sha_file(output / "plan.json"))


def test_started_slot_never_reexecuted_and_two_slot_limit(tmp_path):
    output, plan = fixture(tmp_path)
    calls = []
    def execute(config, directory, timeout):
        calls.append(config)
        result = directory / "jobs" / "trial" / "result.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"task_name": config["tasks"][0]["path"].split("/")[-1],
                                     "verifier_result": {"rewards": {"reward": 0}}}))
    expected = pilot.sha_file(output / "plan.json")
    pilot.run(output, expected_sha=expected, execute=True, executor=execute)
    assert len(calls) == 2
    journal = json.loads((output / "journal.json").read_text())
    journal[plan["slots"][2]["run_id"]] = {"status": "started"}
    (output / "journal.json").write_text(json.dumps(journal))
    pilot.run(output, expected_sha=expected, execute=True, max_new_trials=1, executor=execute)
    assert len(calls) == 3
    journal = json.loads((output / "journal.json").read_text())
    assert journal[plan["slots"][2]["run_id"]]["status"] == "interrupted_unknown"


def test_history_truncation_and_same_campaign_other_output_cannot_repay(tmp_path):
    output, plan = fixture(tmp_path)
    ledger = BudgetLedger(plan["ledger"], campaign_id=plan["campaign_id"], spend_ceiling_usd=15)
    slot = plan["slots"][0]
    ledger.register_run(slot["run_id"], max_requests=64, time_cap=900)
    request = ledger.reserve(slot["run_id"], input_bound=1, max_output=1, purpose="agent")
    ledger.settle(request, usage={"prompt_tokens": 1, "completion_tokens": 1}, status="ok")
    calls = []
    pilot.run(output, expected_sha=pilot.sha_file(output / "plan.json"), execute=True,
              max_new_trials=1, executor=lambda *args: calls.append(args))
    assert len(calls) == 1
    journal = json.loads((output / "journal.json").read_text())
    assert journal[slot["run_id"]]["status"] == "consumed_elsewhere"


def test_nonempty_history_is_immutable_but_appends_are_allowed(tmp_path):
    output, plan = fixture(tmp_path)
    ledger = BudgetLedger(plan["ledger"])
    request = ledger.reserve("old", input_bound=1, max_output=1, purpose="agent")
    ledger.settle(request, usage={"prompt_tokens": 1, "completion_tokens": 1}, status="ok")
    second = tmp_path / "second"
    pilot.freeze(second, tasks_root=plan["tasks_root"], manifest=plan["manifest"],
                 oracle_map=plan["oracle_map"], ledger=pilot.Path(plan["ledger"]),
                 campaign_id="test-code", spend_ceiling_usd=15)
    expected = pilot.sha_file(second / "plan.json")
    pilot.preflight(second, expected)
    data = ledger.snapshot()
    data["requests"] = []
    ledger._write(data)
    with pytest.raises(ValueError, match="historical prefix"):
        pilot.preflight(second, expected)


def test_missing_oracle_is_frozen_blocked_without_replacement(tmp_path):
    output, plan = fixture(tmp_path)
    mapping = json.loads(pilot.Path(plan["oracle_map"]).read_text())
    mapping.pop(pilot.TASKS[0])
    pilot.Path(plan["oracle_map"]).write_text(json.dumps(mapping))
    second = tmp_path / "blocked"
    frozen = pilot.freeze(second, tasks_root=plan["tasks_root"], manifest=plan["manifest"],
                          oracle_map=plan["oracle_map"], ledger=pilot.Path(plan["ledger"]),
                          campaign_id="test-code", spend_ceiling_usd=15)
    assert all(s["blocked"] == "oracle_missing" for s in frozen["slots"][:2])
    config = pilot.job_config(frozen, frozen["slots"][2], second)
    assert config["retry"] == {"max_retries": 0}
    assert config["n_attempts"] == config["n_concurrent_trials"] == 1
    assert config["agents"][0]["kwargs"]["max_requests"] == 64
    frozen["runtime_proxy"] = "http://172.19.80.1:17913"
    config = pilot.job_config(frozen, frozen["slots"][2], second)
    assert config["verifier"]["env"]["HTTPS_PROXY"] == frozen["runtime_proxy"]
    assert config["verifier"]["env"]["HTTP_PROXY"] == ""
    assert config["verifier"]["env"]["http_proxy"] == ""
    assert config["verifier"]["env"] == config["environment"]["env"]


def test_frozen_job_matches_installed_official_harbor_schema(tmp_path):
    pytest.importorskip("harbor")
    from harbor.models.job.config import JobConfig
    output, plan = fixture(tmp_path)
    plan["runtime_proxy"] = "http://172.19.80.1:17913"
    config = JobConfig.model_validate(pilot.job_config(plan, plan["slots"][0], output))
    assert config.retry.max_retries == 0
    assert config.agents[0].import_path.endswith(":BudgetedHarborAgent")
    assert config.agents[0].max_timeout_sec == 900
    assert config.verifier.env["HTTPS_PROXY"] == plan["runtime_proxy"]
    assert config.verifier.env["HTTP_PROXY"] == config.environment.env["http_proxy"] == ""


def test_official_timeout_is_preserved_and_capped(tmp_path):
    path = tmp_path / "task"
    path.mkdir()
    (path / "task.toml").write_text("[agent]\ntimeout_sec=1200\n")
    assert pilot.runtime_constraints(path)["time_cap"] == 1200
    (path / "task.toml").write_text("[agent]\ntimeout_sec=3600\n")
    assert pilot.runtime_constraints(path)["time_cap"] == 1800


@pytest.mark.parametrize("schemas,valid", [(["on", "off"], False), ([None, None], False), (["same", "same"], True)])
def test_summary_requires_complete_matching_schemas_for_pair_delta(tmp_path, schemas, valid):
    output, plan = fixture(tmp_path)
    journal = {}
    for index, (slot, schema) in enumerate(zip(plan["slots"][:2], schemas, strict=True)):
        directory = output / f"{index:02d}-{slot['task']}-{slot['arm']}"
        directory.mkdir()
        (directory / "live_run.json").write_text(json.dumps({"schema_sha256": schema}))
        journal[slot["run_id"]] = {"status": "finished", "category": "scored", "official_reward": 1 - index}
    (output / "journal.json").write_text(json.dumps(journal))
    result = pilot.summary(output)
    assert result["complete_scored_pairs"] == 1
    assert result["complete_pairs"] == int(valid)
    assert result["pairs"][0]["valid_pair"] is valid
    assert (result["pairs"][0]["on_minus_off"] is not None) is valid
