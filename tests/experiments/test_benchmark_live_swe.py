"""Single pinned SWE solver contract: fake provider/container, no external calls."""

import json

import pytest

from experiments.benchmark_live import swe_agent as swe
from my_agent_llm import LLM, Response


@pytest.mark.anyio
@pytest.mark.parametrize("projection,condition,guidance",[("off","off","none"),("on","off","static"),("on","on","none")])
async def test_solver_rejects_conflicting_experiment_dimensions_before_io(projection,condition,guidance):
    with pytest.raises(ValueError,match="dimensions"):
        await swe.solve(metadata="unused",gold_evidence="unused",ledger_path="unused",output="unused",
                        arm=projection,condition_arm=condition,budget_guidance=guidance,experiment_factor="budget_visibility")


@pytest.mark.anyio
@pytest.mark.parametrize("guidance", ["none", "static"])
@pytest.mark.parametrize("observe_error", [False, True])
@pytest.mark.parametrize("sidecar_error", [False, True])
@pytest.mark.parametrize("shell_error", [False, True])
async def test_static_budget_hint_and_private_worktree_observation(tmp_path, monkeypatch, guidance, observe_error, sidecar_error, shell_error):
    task, gold = evidence(tmp_path)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_HOST_KEY")
    original_ledger = swe.BudgetLedger
    monkeypatch.setattr(swe, "BudgetLedger", lambda path, **kw: original_ledger(path, request_ceiling=7, **kw))
    if sidecar_error:
        original_open=swe.Path.open
        def broken_open(path,*args,**kwargs):
            if path.name=="worktree-observations.jsonl":
                raise OSError("PRIVATE_WORKTREE")
            return original_open(path,*args,**kwargs)
        monkeypatch.setattr(swe.Path,"open",broken_open)
    systems = []
    class Provider:
        async def achat(self, messages, **kwargs):
            systems.append(messages[0].text_content)
            if len(systems) == 1:
                return Response(content="", model="fake", usage={"prompt_tokens": 20, "completion_tokens": 5},
                                tool_calls=[{"id":"shell1","name":"shell","args":{"command":"inspect"}}])
            visible = "\n".join(m.text_content for m in messages)
            assert not any(value in visible for value in ("PRIVATE_WORKTREE", "tracked_changed_files", "shell_response_index"))
            return Response(content="done", model="fake", usage={"prompt_tokens": 20, "completion_tokens": 5})
    def factory(config):
        llm = LLM(config)
        llm._provider = Provider()
        return llm
    class Environment:
        async def start(self): pass
        async def exec(self, command):
            if shell_error:
                raise RuntimeError("shell failed after an observable mutation")
            return {"exit_code":0,"stdout":"visible","stderr":""}
        async def observe_worktree(self):
            if observe_error:
                raise RuntimeError("PRIVATE_WORKTREE")
            return {"status":"available","tracked_changed_files":1,"untracked_files":0}
        async def patch(self): return "SYNTHETIC_DIFF"
        async def close(self): pass
    condition="on" if guidance=="static" else "off"
    root = await swe.solve(metadata=task,gold_evidence=gold,ledger_path=tmp_path/"ledger.json",arm="on",
                           output=tmp_path/"run",environment=Environment(),llm_factory=factory,budget_guidance=guidance,
                           condition_arm=condition,experiment_factor="budget_visibility")
    assert len(systems)==2 and systems[0]==systems[1]
    sentence="This run allows at most 7 API requests, including auxiliary calls."
    assert (sentence in systems[0]) == (guidance == "static")
    result=json.loads((root/"run.json").read_text())
    assert result["budget_guidance"]==guidance and result["artifact_projection"]=="on"
    assert result["api_request_limit"]==7 and result["condition_arm"]==condition and result["experiment_factor"]=="budget_visibility"
    prediction=json.loads((root/"model_patch.jsonl").read_text())
    assert prediction["model_name_or_path"]=="my-agent-harness-"+condition
    assert original_ledger(tmp_path/"ledger.json").snapshot()["max_requests"]==16
    if not sidecar_error:
        observations=[json.loads(line) for line in (root/"worktree-observations.jsonl").read_text().splitlines()]
        assert len(observations)==1
        assert observations[0]["status"]==("unavailable" if observe_error else "available")
    assert result["worktree_observation_errors"]==int(sidecar_error)
    assert result["first_visible_worktree_difference_after_shell"] == (None if observe_error or sidecar_error else 1)


@pytest.mark.anyio
async def test_worktree_observation_uses_only_readonly_git_commands(monkeypatch):
    docker=swe.DockerTask()
    commands=[]
    async def call(*args, **kwargs):
        commands.append(args[-1])
        return (0,"2\t1\tsrc/a.py\n-\t-\tasset.bin\n","") if "diff" in args[-1] else (0,"new.py\0","")
    monkeypatch.setattr(docker,"call",call)
    result=await docker.observe_worktree()
    assert result["tracked_changed_files"]==2 and result["untracked_files"]==1
    assert result["tracked_added_lines"]==2 and result["tracked_deleted_lines"]==1
    assert all("--no-optional-locks" in c for c in commands)
    assert not any(token in "\n".join(commands) for token in ("add", "reset", "checkout", "restore"))


@pytest.mark.anyio
async def test_worktree_observation_preserves_real_git_index(tmp_path, monkeypatch):
    import hashlib
    import shlex
    import subprocess
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    tracked=tmp_path/"tracked.txt"
    tracked.write_text("old\n")
    git("add", "tracked.txt")
    git("-c", "user.name=offline", "-c", "user.email=offline@example.invalid", "commit", "-qm", "fixture")
    docker=swe.DockerTask()
    docker.pin["base_commit"]=git("rev-parse", "HEAD").strip()
    tracked.write_text("new\nextra\n")
    (tmp_path/"untracked.txt").write_text("new file\n")
    fingerprint=hashlib.sha256((tmp_path/".git/index").read_bytes()).hexdigest()
    async def call(*args, **kwargs):
        result=subprocess.run(shlex.split(args[-1]),cwd=tmp_path,capture_output=True,text=True)
        return result.returncode,result.stdout,result.stderr
    monkeypatch.setattr(docker,"call",call)
    observed=await docker.observe_worktree()
    assert observed["status"]=="available" and observed["tracked_changed_files"]==1 and observed["untracked_files"]==1
    assert hashlib.sha256((tmp_path/".git/index").read_bytes()).hexdigest()==fingerprint


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
