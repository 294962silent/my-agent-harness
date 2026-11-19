"""Offline contracts for the strict three-arm public pilot driver."""

import hashlib
import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

_WSL_RUNTIME = (
    os.name == "posix"
    and importlib.util.find_spec("harbor") is not None
    and Path("/mnt/c/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/bench-proxy.py").is_file()
)
pytestmark = pytest.mark.skipif(
    not _WSL_RUNTIME, reason="requires the prepared WSL Docker/Harbor runtime"
)

if _WSL_RUNTIME:
    from experiments.long_horizon_code import public_pilot as pilot
else:
    pilot = None


def _fixture(tmp_path):
    tasks_root = tmp_path / "tasks"
    entries = []
    oracle_map = {}
    for index, name in enumerate(pilot.TASKS):
        task = tasks_root / name
        task.mkdir(parents=True)
        files = {
            "instruction.md": "Do the official task",
            "task.toml": (
                f'[task]\nname = "terminal-bench/{name}"\n'
                "[verifier]\ntimeout_sec=300\n"
                "[agent]\ntimeout_sec=900\n"
                "[environment]\ncpus=1\nmemory_mb=2048\nstorage_mb=10240\n"
                f'gpus=0\ndocker_image="fixture/{index}:latest"\n'
            ),
        }
        for relative, text in files.items():
            data = text.encode()
            (task / relative).write_bytes(data)
            entries.append({
                "path": f"tasks/{name}/{relative}",
                "bytes": len(data),
                "git_blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": "100644",
            })
        oracle = tmp_path / f"{name}.oracle.json"
        oracle.write_text(json.dumps({
            "task_name": f"terminal-bench/{name}",
            "agent_info": {"name": "oracle"},
            "verifier_result": {"rewards": {"reward": 1}},
            "task_checksum": f"fixture-checksum-{name}",
            "finished_at": "2026-11-15T00:00:00Z",
        }))
        oracle_map[name] = str(oracle)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"revision": pilot.REVISION, "source": pilot.SOURCE, "entries": entries}))
    oracle_path = tmp_path / "oracle-map.json"
    oracle_path.write_text(json.dumps(oracle_map))
    output = tmp_path / "campaign"
    ledger = tmp_path / "budget.json"
    images = {}
    for index, _name in enumerate(pilot.TASKS):
        images[f"fixture/{index}:latest"] = {
            "tag": f"fixture/{index}:latest",
            "tags": [f"fixture/{index}:latest"],
            "id": f"sha256:{index + 1:064x}",
            "size_bytes": 100 + index,
        }
    plan = pilot.freeze(
        output,
        tasks_root=tasks_root,
        manifest=manifest,
        oracle_map=oracle_path,
        ledger=ledger,
        runtime_proxy="http://172.19.80.1:17897",
        image_inspector=lambda image: images[image],
    )
    return output, plan, images, oracle_map


def test_freeze_has_fixed_order_modes_ids_and_isolated_ledger(tmp_path, monkeypatch):
    for name in pilot.LANGFUSE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    output, plan, _images, _oracles = _fixture(tmp_path)
    assert [[slot["task"], slot["arm"]] for slot in plan["slots"]] == [list(row) for row in pilot.ORDER]
    assert [slot["run_id"] for slot in plan["slots"]] == [
        pilot.EXPECTED_RUN_IDS[(slot["task"], slot["arm"])] for slot in plan["slots"]
    ]
    assert {(slot["arm"], slot["artifact_projection"], slot["compaction_mode"]) for slot in plan["slots"]} == {
        ("baseline", "off", "baseline"),
        ("enhanced", "on", "layered"),
        ("loo_artifact", "off", "layered"),
    }
    assert all(slot["limits"]["max_requests"] == 64 for slot in plan["slots"])
    assert all(slot["limits"]["max_seconds"] == 780 for slot in plan["slots"])
    ledger = json.loads((tmp_path / "budget.json").read_text())
    assert ledger["limit_micro_usd"] == 30_000_000
    assert ledger["campaigns"][pilot.CAMPAIGN_ID]["spend_ceiling_micro_usd"] == 30_000_000
    assert len(ledger["runs"]) == 9 and not ledger["requests"]
    assert plan["langfuse"]["enabled"] is True and plan["langfuse"]["required"] is True
    assert plan["langfuse"]["base_url"] == pilot.LANGFUSE_BASE_URL
    assert plan["langfuse"]["stage"] == "final"
    assert plan["langfuse"]["env_names"] == list(pilot.LANGFUSE_ENV_NAMES)
    assert plan["langfuse"]["env_presence"] == {name: False for name in pilot.LANGFUSE_ENV_NAMES}
    assert all(slot["observation_stage"] == "final" for slot in plan["slots"])
    assert (output / "plan.sha256").read_text().strip() == pilot.sha_file(output / "plan.json")


def test_started_run_is_allowed_but_official_task_drift_is_not(tmp_path, monkeypatch):
    output, plan, images, _oracles = _fixture(tmp_path)
    monkeypatch.setattr(pilot, "image_info", lambda image: images[image])
    ledger = pilot.BudgetLedger(
        plan["ledger"], limit_usd="30", campaign_id=plan["campaign_id"], spend_ceiling_usd=30
    )
    with ledger.transaction() as data:
        data["runs"][plan["slots"][0]["run_id"]]["trial_started"] = True
    expected = pilot.sha_file(output / "plan.json")
    pilot.validate_plan(output, expected)
    task_file = pilot.Path(plan["tasks_root"]) / pilot.TASKS[0] / "instruction.md"
    task_file.write_text("changed")
    with pytest.raises(pilot.PilotStop, match="official"):
        pilot.validate_plan(output, expected)


def test_job_config_carries_arm_and_proxy_identity(tmp_path):
    output, plan, _images, _oracles = _fixture(tmp_path)
    slot = plan["slots"][1]
    config = pilot.trial_config(plan, slot, output / "trial", "/tmp/oracle.json")
    pilot._trial_config_validate(config)
    kwargs = config["agents"][0]["kwargs"]
    assert kwargs["artifact_projection"] == "on"
    assert kwargs["compaction_mode"] == "layered"
    assert kwargs["observation_mode"] == "langfuse"
    assert kwargs["observation_required"] is True
    assert kwargs["observation_base_url"] == pilot.LANGFUSE_BASE_URL
    assert kwargs["max_requests"] == 64
    assert config["environment"]["env"] == config["verifier"]["env"]
    assert config["environment"]["env"]["HTTPS_PROXY"] == plan["runtime_proxy"]
    assert config["environment"]["env"]["HTTP_PROXY"] == ""


def test_zero_model_preparation_then_one_fake_trial(tmp_path, monkeypatch):
    output, plan, images, oracles = _fixture(tmp_path)
    monkeypatch.setattr(pilot, "image_info", lambda image: images[image])
    monkeypatch.setattr(pilot, "proxy_reachable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(pilot, "install_live_credential", lambda: "test_credential_source")
    monkeypatch.setattr(
        pilot,
        "langfuse_status",
        lambda plan: {
            "ready": True,
            "env_presence": {name: True for name in pilot.LANGFUSE_ENV_NAMES},
            "base_url_matches": True,
            "host_matches": True,
            "base_url": plan["langfuse"]["base_url"],
        },
    )

    def oracle_executor(config, directory, _timeout):
        name = pilot.Path(config["tasks"][0]["path"]).name
        trial = directory / "jobs" / "oracle" / "trial"
        (trial / "verifier").mkdir(parents=True)
        (directory / "jobs/oracle/result.json").write_text(json.dumps({"stats": {"mean": 1}}))
        (trial / "verifier/ctrf.json").write_text("{}")
        (trial / "result.json").write_text(json.dumps({
            "task_name": f"terminal-bench/{name}",
            "agent_info": {"name": "oracle"},
            "verifier_result": {"rewards": {"reward": 1}},
            "task_checksum": json.loads(pilot.Path(oracles[name]).read_text())["task_checksum"],
            "exception_info": None,
            "finished_at": "2026-11-15T00:00:00Z",
        }))
        return 0

    expected = pilot.sha_file(output / "plan.json")
    oracle_receipt = pilot.run_oracles(output, expected_sha=expected, executor=oracle_executor)
    assert oracle_receipt["status"] == "pass"
    transport = pilot.transport_probe(
        output,
        expected_sha=expected,
        probe=lambda image, proxy: {"status": "pass", "return_code": 0, "http_status": 200, "bytes_read": 1},
    )
    assert transport["status"] == "pass"
    assert pilot.preflight(output, expected_sha=expected, require_ready=True)["status"] == "ready"

    def trial_executor(config, directory, _timeout):
        name = pilot.Path(config["tasks"][0]["path"]).name
        trial = directory / "jobs" / "harbor" / "trial"
        trial.mkdir(parents=True)
        kwargs = config["agents"][0]["kwargs"]
        limits = {
            key: kwargs[key]
            for key in ("max_requests", "max_seconds", "context_budget", "output_tokens",
                        "shell_timeout", "compaction_mode", "observation_mode",
                        "observation_required")
        }
        (trial / "live_run.json").write_text(json.dumps({
            "artifact_projection": kwargs["artifact_projection"],
            "compaction_mode": kwargs["compaction_mode"],
            "observation_mode": kwargs["observation_mode"],
            "observation_required": kwargs["observation_required"],
            "observation": {
                "mode": kwargs["observation_mode"],
                "required": kwargs["observation_required"],
                "base_url": kwargs["observation_base_url"],
                "arm": next(
                    slot["arm"] for slot in plan["slots"] if slot["run_id"] == kwargs["run_id"]
                ),
                "stage": "final",
                "task_id": name,
                "ready": True,
            },
            "schema_sha256": "same-tools",
            "tool_schema_sha256": "same-tools",
            "experiment_identity_sha256": f"identity-{kwargs['run_id']}",
            "oracle_gate": {
                "harbor_task_checksum": next(
                    slot["oracle_gate"]["harbor_task_checksum"]
                    for slot in plan["slots"] if slot["run_id"] == kwargs["run_id"]
                )
            },
            "limits": limits,
        }))
        (trial / "result.json").write_text(json.dumps({
            "task_name": f"terminal-bench/{name}",
            "agent_info": {"name": "budgeted-my-agent-harness"},
            "agent_result": {"metadata": {"status": "end_turn"}},
            "verifier_result": {"rewards": {"reward": 0}},
            "exception_info": None,
            "finished_at": "2026-11-15T00:00:00Z",
        }))
        ledger = pilot.BudgetLedger(
            kwargs["ledger_path"], campaign_id=kwargs["campaign_id"], spend_ceiling_usd=30
        )
        request = ledger.reserve(kwargs["run_id"], input_bound=10, max_output=1, purpose="agent")
        ledger.settle(request, usage={"prompt_tokens": 10, "completion_tokens": 1}, status="ok")
        return 0

    result = pilot.run(output, expected_sha=expected, execute=True, max_new_trials=1, executor=trial_executor)
    assert result["complete_scored_triplets"] == 0
    journal = json.loads((output / "journal.json").read_text())
    assert len(journal) == 1
    first = journal[plan["slots"][0]["run_id"]]
    assert first["status"] == "finished" and first["official_reward"] == 0


def test_failed_oracle_is_sealed_and_keeps_zero_model_request(tmp_path, monkeypatch):
    output, plan, images, oracles = _fixture(tmp_path)
    monkeypatch.setattr(pilot, "image_info", lambda image: images[image])
    monkeypatch.setattr(pilot, "proxy_reachable", lambda *_args, **_kwargs: True)

    def failed_oracle(config, directory, _timeout):
        name = pilot.Path(config["tasks"][0]["path"]).name
        trial = directory / "jobs" / "oracle" / "trial"
        (trial / "verifier").mkdir(parents=True)
        (trial / "verifier/ctrf.json").write_text("{}")
        (trial / "result.json").write_text(json.dumps({
            "task_name": f"terminal-bench/{name}",
            "agent_info": {"name": "oracle"},
            "verifier_result": {"rewards": {"reward": 0}},
            "task_checksum": json.loads(pilot.Path(oracles[name]).read_text())["task_checksum"],
            "exception_info": None,
            "finished_at": "2026-11-15T00:00:00Z",
        }))
        return 0

    with pytest.raises(pilot.PilotStop, match="oracle gate failed"):
        pilot.run_oracles(
            output,
            expected_sha=pilot.sha_file(output / "plan.json"),
            executor=failed_oracle,
        )
    receipt = pilot.read_sealed(output / "readiness/oracle.json")
    assert receipt["status"] == "blocked"
    assert receipt["model_requests"] == 0
    assert receipt["failed_task"] == pilot.TASKS[0]
    assert receipt["error_type"] == "PilotStop"


def test_transport_probe_requires_both_github_and_release_asset_tls(monkeypatch):
    captured = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout='{"github_tls":0,"release_assets_tls":1}\n',
            stderr="",
        )

    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        run,
    )
    result = pilot.docker_transport_probe(
        "fixture/image:latest", "http://172.19.80.1:17897"
    )
    assert result["status"] == "blocked"
    assert result["github_tls"] == 0 and result["release_assets_tls"] == 1
    command = captured["command"]
    assert "NO_PROXY=github.com,release-assets.githubusercontent.com" in command
    probe_code = command[-3]
    assert "openssl s_client -connect github.com:443" in probe_code
    assert (
        "openssl s_client -connect release-assets.githubusercontent.com:443"
        in probe_code
    )


def test_proxy_env_bypasses_github_release_hosts_only():
    proxy = "http://172.19.80.1:17897"
    env = pilot.proxy_env(proxy)
    assert env["HTTPS_PROXY"] == proxy
    assert env["HTTP_PROXY"] == ""
    assert env["NO_PROXY"] == "github.com,release-assets.githubusercontent.com"
    assert env["no_proxy"] == "github.com,release-assets.githubusercontent.com"


def test_langfuse_preflight_requires_all_env_names_and_matching_urls(monkeypatch):
    for name in pilot.LANGFUSE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    plan = {
        "langfuse": {
            "base_url": pilot.LANGFUSE_BASE_URL,
            "env_names": list(pilot.LANGFUSE_ENV_NAMES),
        }
    }
    assert pilot.langfuse_status(plan)["ready"] is False
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "fixture-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "fixture-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", pilot.LANGFUSE_BASE_URL)
    monkeypatch.setenv("LANGFUSE_HOST", "cloud.langfuse.com")
    status = pilot.langfuse_status(plan)
    assert status["ready"] is True
    assert status["env_presence"] == {name: True for name in pilot.LANGFUSE_ENV_NAMES}


def test_sign_test_is_descriptive_for_three_pairs():
    result = pilot._sign_test([1, 1, 1])
    assert result == {"positive": 3, "negative": 0, "n": 3, "two_sided_p": 0.25}


def test_summary_reports_all_three_valid_pairs(tmp_path):
    output, plan, _images, _oracles = _fixture(tmp_path)
    rewards = {"baseline": 0, "enhanced": 1, "loo_artifact": 0}
    ledger = pilot.BudgetLedger(
        plan["ledger"], campaign_id=plan["campaign_id"], spend_ceiling_usd=30
    )
    journal = {}
    for index, slot in enumerate(plan["slots"]):
        directory = output / f"{index:02d}-{slot['task']}-{slot['arm']}"
        trial = directory / "jobs" / "harbor" / "trial"
        trial.mkdir(parents=True)
        limits = {
            key: slot["limits"][key]
            for key in ("max_requests", "max_seconds", "context_budget", "output_tokens",
                        "shell_timeout", "compaction_mode", "observation_mode",
                        "observation_required")
        }
        (trial / "live_run.json").write_text(json.dumps({
            "artifact_projection": slot["artifact_projection"],
            "compaction_mode": slot["compaction_mode"],
            "observation_mode": "langfuse",
            "observation_required": True,
            "observation": {
                "mode": "langfuse", "required": True, "base_url": pilot.LANGFUSE_BASE_URL,
                "arm": slot["arm"], "stage": "final", "task_id": slot["task"], "ready": True,
            },
            "schema_sha256": "same-tools",
            "tool_schema_sha256": "same-tools",
            "experiment_identity_sha256": f"identity-{slot['run_id']}",
            "oracle_gate": {"harbor_task_checksum": slot["oracle_gate"]["harbor_task_checksum"]},
            "limits": limits,
        }))
        journal[slot["run_id"]] = {
            "status": "finished", "category": "scored", "official_reward": rewards[slot["arm"]],
            "usage_complete": True, "schema_sha256": "same-tools",
            "compaction_mode": slot["compaction_mode"],
            "artifact_projection": slot["artifact_projection"],
            "observation_mode": "langfuse",
            "observation_required": True,
            "observation_ready": True,
        }
        request = ledger.reserve(slot["run_id"], input_bound=10, max_output=1, purpose="agent")
        ledger.settle(request, usage={"prompt_tokens": 10, "completion_tokens": 1}, status="ok")
    (output / "journal.json").write_text(json.dumps(journal))
    result = pilot.summary(output)
    assert result["complete_scored_triplets"] == 3
    assert result["valid_triplets"] == 3
    assert all(row["delta_enhanced_minus_baseline"] == 1 for row in result["triplets"])
    assert all(row["delta_enhanced_minus_loo"] == 1 for row in result["triplets"])
    assert result["statistics"]["enhanced_minus_baseline_sign_test"]["two_sided_p"] == 0.25


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["baseline", "layered"])
async def test_harbor_adapter_constructs_real_compaction_policy(tmp_path, monkeypatch, mode):
    from experiments.benchmark_live import harbor_agent
    from harbor.models.agent.context import AgentContext
    from harbor.models.task.task import Task

    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "environment/Dockerfile").write_text("FROM scratch\n")
    (task / "tests/test.sh").write_text("exit 0\n")
    (task / "instruction.md").write_text("Do work")
    (task / "task.toml").write_text(
        '[task]\nname = "fixture/task"\n[agent]\ntimeout_sec=900\n'
        "[environment]\ncpus=1\nmemory_mb=2048\ngpus=0\n"
    )
    oracle = tmp_path / "oracle.json"
    oracle.write_text(json.dumps({
        "task_name": "fixture/task",
        "agent_info": {"name": "oracle"},
        "verifier_result": {"rewards": {"reward": 1}},
        "task_checksum": Task(task).checksum,
        "finished_at": "2026-11-15T00:00:00Z",
    }))
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.registry = SimpleNamespace(get_schemas=lambda: [])

        def subscribe(self, _callback):
            return None

        async def run(self, _instruction):
            return None

        async def aclose(self):
            return None

    class RawLLM:
        model = "deepseek-v4.1-flash"
        config = SimpleNamespace(max_retries=0, provider="openai")

    monkeypatch.setattr(harbor_agent, "Agent", FakeAgent)
    monkeypatch.setattr(harbor_agent, "create_live_llm", lambda **_kwargs: RawLLM())

    async def capture(_agent, _instruction, _timeout):
        return "done", {"stop_reason": "end_turn"}

    monkeypatch.setattr(harbor_agent, "_run_agent_and_capture", capture)
    adapter = harbor_agent.BudgetedHarborAgent(
        task_dir=task,
        logs_dir=tmp_path / "logs",
        oracle_result=oracle,
        ledger_path=tmp_path / "ledger.json",
        artifact_projection="on",
        compaction_mode=mode,
    )
    context = AgentContext()
    await adapter.run("Do work", SimpleNamespace(), context)
    assert captured["compaction_policy"].mode == mode
    assert captured["compaction_policy"].micro_min_chars == 256
    assert captured["compaction_policy"].recent_tool_groups == 1
    assert captured["artifact_store"] is not None
    assert context.metadata["compaction_mode"] == mode
    assert context.metadata["limits"]["compaction_mode"] == mode
    assert context.metadata["limits"]["compaction_profile"]["micro_min_chars"] == 256
    assert context.metadata["schema_sha256"] == context.metadata["tool_schema_sha256"]
    assert context.metadata["observation_mode"] == "off"
    assert context.metadata["observation_required"] is False
    assert context.metadata["observation"]["ready"] is True


def test_harbor_adapter_rejects_unknown_compaction_mode(tmp_path):
    from experiments.benchmark_live import harbor_agent

    logs = tmp_path / "logs"
    logs.mkdir()
    with pytest.raises(ValueError, match="compaction_mode"):
        harbor_agent.BudgetedHarborAgent(
            task_dir=tmp_path / "missing-task",
            logs_dir=logs,
            oracle_result=tmp_path / "missing-oracle.json",
            ledger_path=tmp_path / "ledger.json",
            compaction_mode="unknown",
        )


@pytest.mark.anyio
async def test_required_langfuse_agent_shell_call_survives_task_boundaries(tmp_path, monkeypatch):
    from contextvars import ContextVar
    from experiments.benchmark_live import harbor_agent
    from harbor.models.agent.context import AgentContext
    from harbor.models.task.task import Task
    from my_agent_llm import Response

    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "environment/Dockerfile").write_text("FROM scratch\n")
    (task / "tests/test.sh").write_text("exit 0\n")
    (task / "instruction.md").write_text("Do work")
    (task / "task.toml").write_text(
        '[task]\nname = "fixture/task"\n[agent]\ntimeout_sec=900\n'
        "[environment]\ncpus=1\nmemory_mb=2048\ngpus=0\n"
    )
    oracle = tmp_path / "oracle.json"
    oracle.write_text(json.dumps({
        "task_name": "fixture/task",
        "agent_info": {"name": "oracle"},
        "verifier_result": {"rewards": {"reward": 1}},
        "task_checksum": Task(task).checksum,
        "finished_at": "2026-11-15T00:00:00Z",
    }))
    owner_calls = {"observe": [], "flush": 0, "close": 0, "factory": None}
    scope_value = ContextVar("fixture_observation_scope", default=None)

    class FakeOwner:
        @classmethod
        def from_environment(cls, raw, **kwargs):
            owner_calls["factory"] = (raw, kwargs)
            return cls()

        @contextmanager
        def observe(self, root_id, **kwargs):
            owner_calls["observe"].append((root_id, kwargs))
            token = scope_value.set((root_id, kwargs))
            try:
                yield
            finally:
                scope_value.reset(token)

        async def aflush_and_check(self):
            owner_calls["flush"] += 1
            return True

        async def aclose(self):
            owner_calls["close"] += 1
            return {
                "runtime_close_complete": True,
                "export_errors": 0,
                "close_errors": 0,
                "dropped": 0,
                "runtime_alive_after_close": False,
                "llm_clients_closed": True,
            }

    class RawLLM:
        model = "deepseek-v4.1-flash"
        config = SimpleNamespace(max_retries=0, provider="openai")

        async def achat(self, messages, **_kwargs):
            owner_calls.setdefault("provider", []).append([message.role for message in messages])
            usage = {"prompt_tokens": 10, "completion_tokens": 2}
            if len(owner_calls["provider"]) == 1:
                return Response(
                    model=self.model,
                    content="",
                    usage=usage,
                    tool_calls=[{
                        "id": "shell-1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
                    }],
                )
            return Response(model=self.model, content="done", usage=usage)

    class Environment:
        def __init__(self):
            self.commands = []

        async def exec(self, command, timeout_sec):
            self.commands.append((command, timeout_sec))
            return SimpleNamespace(return_code=0, stdout="/workspace", stderr="")

    ledger_path = tmp_path / "ledger.json"
    pilot.BudgetLedger(
        ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1
    )
    monkeypatch.setattr(harbor_agent, "ExperimentLLMOwner", FakeOwner)
    monkeypatch.setattr(harbor_agent, "create_live_llm", lambda **_kwargs: RawLLM())
    adapter = harbor_agent.BudgetedHarborAgent(
        task_dir=task,
        logs_dir=tmp_path / "logs",
        oracle_result=oracle,
        ledger_path=ledger_path,
        artifact_projection="on",
        compaction_mode="layered",
        observation_mode="langfuse",
        observation_required=True,
        campaign_id="lhpp-test",
        run_id="run-1",
        spend_ceiling_usd=1,
    )
    context = AgentContext()
    environment = Environment()
    await adapter.run("Do work", environment, context)
    ledger = pilot.BudgetLedger(ledger_path, campaign_id="lhpp-test", spend_ceiling_usd=1).snapshot()
    assert len(ledger["requests"]) == 2
    assert all(request["status"] == "ok" for request in ledger["requests"])
    assert environment.commands and environment.commands[0][0] == "pwd"
    assert owner_calls["observe"] == [
        ("run-1", {"arm": "enhanced", "stage": "final", "task_id": "task"}),
        ("run-1", {"arm": "enhanced", "stage": "final", "task_id": "task"}),
    ]
    assert owner_calls["flush"] == owner_calls["close"] == 1
    assert owner_calls["factory"][1]["campaign_id"] == "lhpp-test"
    assert owner_calls["factory"][1]["enabled"] is True
    assert owner_calls["factory"][1]["required"] is True
    assert owner_calls["factory"][1]["public_key_env"] == "LANGFUSE_PUBLIC_KEY"
    assert owner_calls["factory"][1]["secret_key_env"] == "LANGFUSE_SECRET_KEY"
    assert context.metadata["observation_mode"] == "langfuse"
    assert context.metadata["observation_required"] is True
    assert context.metadata["observation"]["ready"] is True
    assert context.metadata["observation"]["stage"] == "final"
    assert context.metadata["status"] == "end_turn"


def test_exception_metadata_is_bounded_and_redacts_credentials(monkeypatch):
    from experiments.benchmark_live.harbor_agent import _safe_exception

    monkeypatch.setenv("LIVE_BENCH_API_KEY", "fixture-live-secret")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "fixture-langfuse-secret")
    result = _safe_exception(
        ValueError(
            "Bearer abc.def fixture-live-secret fixture-langfuse-secret "
            + "x" * 2000
        )
    )
    assert result["type"] == "ValueError"
    assert "fixture-live-secret" not in result["message"]
    assert "fixture-langfuse-secret" not in result["message"]
    assert "[REDACTED]" in result["message"]
    assert len(result["message"]) <= 1000
    assert len(result["traceback"]) <= 8000
