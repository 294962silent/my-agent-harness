"""Single pinned SWE solver contract: fake provider/container, no external calls."""

import json

import pytest

from experiments.benchmark_live import swe_agent as swe
from my_agent_llm import LLM, Response


def evidence(tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "instance_id": swe.INSTANCE,
                "problem_statement": "Repair redirect behavior",
                "repo": "psf/requests",
                "base_commit": swe.BASE,
                "image": swe.IMAGE,
            }
        )
    )
    report = tmp_path / "official-report.json"
    report.write_text(
        json.dumps(
            {"resolved_ids": [swe.INSTANCE], "completed_instances": 1, "resolved_instances": 1, "error_instances": 0}
        )
    )
    gold = tmp_path / "evidence.json"
    gold.write_text(
        json.dumps(
            {
                "instance_id": swe.INSTANCE,
                "base_commit": swe.BASE,
                "image": swe.IMAGE,
                "image_id": swe.IMAGE_ID,
                "metadata_sha256": swe.sha(task),
                "resolved": True,
                "official_report_path": str(report),
                "official_report_sha256": swe.sha(report),
            }
        )
    )
    return task, gold


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup_fault", [False, True])
async def test_pinned_swe_solver_creates_prediction_and_retains_budget(tmp_path, monkeypatch, cleanup_fault):
    task, gold = evidence(tmp_path)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_HOST_KEY")
    if cleanup_fault:
        original_close = swe.Agent.aclose

        async def broken_close(self):
            await original_close(self)
            raise RuntimeError("SYNTHETIC_CLEANUP_SECRET")

        monkeypatch.setattr(swe.Agent, "aclose", broken_close)
    received = []

    class Provider:
        async def achat(self, messages, **kwargs):
            received.extend(m.text_content or "" for m in messages)
            return Response(content="done", model="fake", usage={"prompt_tokens": 30, "completion_tokens": 5})

    def factory(config):
        llm = LLM(config)
        llm._provider = Provider()
        return llm

    class Environment:
        started = closed = False

        async def start(self):
            self.started = True

        async def exec(self, command):
            raise AssertionError("fake model has no tool calls")

        async def patch(self):
            return "diff --git a/a b/a\n"

        async def close(self):
            self.closed = True

    env = Environment()
    root = await swe.solve(
        metadata=task,
        gold_evidence=gold,
        ledger_path=tmp_path / "ledger.json",
        arm="on",
        output=tmp_path / "run",
        environment=env,
        llm_factory=factory,
    )
    result = json.loads((root / "run.json").read_text())
    prediction = json.loads((root / "model_patch.jsonl").read_text())
    assert env.started and env.closed and result["status"] == "end_turn"
    assert result["requests"] == 1 and result["usage_available"] and result["official_reward"] is None
    assert prediction["instance_id"] == swe.INSTANCE and prediction["model_patch"].startswith("diff")
    assert result["cleanup_errors"] == (["agent:RuntimeError"] if cleanup_fault else [])
    assert "SYNTHETIC_CLEANUP_SECRET" not in (root / "run.json").read_text()
    assert not any(key in "\n".join(received) for key in ("SYNTHETIC_HOST_KEY", "resolved_ids", "official_report_path"))


def test_gate_rejects_gold_fields_and_changed_report(tmp_path):
    task, gold = evidence(tmp_path)
    row = json.loads(task.read_text())
    row["test_patch"] = "GOLD_TEST"
    task.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="safe projected"):
        swe.gated_task(task, gold)
    task, gold = evidence(tmp_path)
    report = tmp_path / "official-report.json"
    report.write_text("{}")
    with pytest.raises(ValueError, match="checksum differs"):
        swe.gated_task(task, gold)
    task, gold = evidence(tmp_path)
    row = json.loads(gold.read_text())
    row["resolved"] = False
    gold.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="gold evidence"):
        swe.gated_task(task, gold)


@pytest.mark.anyio
async def test_docker_commands_never_forward_host_env_or_pull(monkeypatch):
    docker = swe.DockerTask()
    calls = []

    async def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "image":
            return 0, swe.IMAGE_ID + "\n", ""
        if args[0] == "exec":
            return 0, swe.BASE + "\n", ""
        return 0, "", ""

    monkeypatch.setattr(docker, "call", call)
    await docker.start()
    await docker.close()
    create = next(c for c in calls if c[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--pull") + 1] == "never"
    assert not any(option in create for option in ("-e", "--env", "-v", "--mount"))
    commands = [c[-1] for c in calls if c[0] == "exec"]
    assert commands == [
        "git rev-parse HEAD",
        "git reset --hard " + swe.BASE,
        "git rev-parse HEAD && git status --porcelain",
    ]


@pytest.mark.anyio
async def test_offset_image_head_is_reset_then_verified(monkeypatch):
    docker = swe.DockerTask()
    head = "5efaed257c694e4452b4c8361aae1cd9cdefd6d1"

    async def call(*args, **kwargs):
        nonlocal head
        if args[0] == "image":
            return 0, swe.IMAGE_ID + "\n", ""
        if args[0] == "exec":
            if args[-1].startswith("git reset --hard "):
                head = swe.BASE
                return 0, "reset", ""
            return 0, head + "\n", ""
        return 0, "", ""

    monkeypatch.setattr(docker, "call", call)
    await docker.start()
    assert docker.initial_head != swe.BASE and head == swe.BASE and docker.stage == "ready"
    await docker.close()


@pytest.mark.anyio
async def test_pre_agent_reset_failure_persists_stage_without_model(tmp_path, monkeypatch):
    task, gold = evidence(tmp_path)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_KEY")

    class Environment:
        stage = "base_reset"
        closed = False

        async def start(self):
            raise swe.SetupError("pinned_base_reset_failed")

        async def close(self):
            self.closed = True

    env = Environment()

    def forbidden(config):
        raise AssertionError("LLM must not initialize")

    with pytest.raises(RuntimeError, match="sanitized"):
        await swe.solve(
            metadata=task,
            gold_evidence=gold,
            ledger_path=tmp_path / "ledger.json",
            arm="on",
            output=tmp_path / "run",
            environment=env,
            llm_factory=forbidden,
        )
    result = json.loads((tmp_path / "run/run.json").read_text())
    assert result["status"] == "pre_agent_error" and result["requests"] == 0
    assert result["stage"] == "base_reset" and result["error_code"] == "pinned_base_reset_failed"
    assert env.closed
