from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

from my_agent_core.events import AgentEnd, MessageEnd, ToolExecutionStart
from my_agent_llm import Message
from my_agent_llm.auth.schema import ApiKeyCredential
from my_agent_llm.models import normalize_finish_reason
from my_agent_llm.observation import CallObservation, CallStart
from my_coding_agent.longhorizon import cli
from my_coding_agent.longhorizon.engine import LongHorizonController
from my_coding_agent.longhorizon.roles import LLMRoleBackend
from my_coding_agent.longhorizon.schema import (
    AuditReport,
    ManagerDecision,
    RoleEpisode,
)
from my_coding_agent.longhorizon.store import LongHorizonStore, StoreIntegrityError


class FakeLLM:
    def __init__(self, config) -> None:
        self.config = config
        self.observers = []

    def add_observer(self, observer):
        self.observers.append(observer)
        return SimpleNamespace(close=lambda: None, closed=False)


class FakeAuthManager:
    def __init__(self, *, key: str, base_url: str = "https://frozen.example/v1") -> None:
        self.key = key
        self.base_url = base_url
        self.calls = []

    def get_credential(self, provider, profile=None):
        self.calls.append((provider, profile))
        return ApiKeyCredential(key=self.key, base_url=self.base_url)


class FakeController:
    last = None

    def __init__(self, *, store, backend) -> None:
        self.store = store
        self.backend = backend
        self.calls = []
        self.__class__.last = self

    async def run_until_terminal(self):
        self.calls.append(("run",))
        return {"status": "ready"}

    async def resume(self):
        self.calls.append(("resume",))
        return {"status": "ready"}

    async def follow_up(self, *, message):
        self.calls.append(("follow_up", message))
        return {"status": "queued"}

    async def restore(self, *, round_index):
        self.calls.append(("restore", round_index))
        return {"status": "restored"}


def controller_factory(*, store, backend):
    return FakeController(store=store, backend=backend)


class ScriptedDecisionLLM:
    def __init__(self, config, decisions):
        self.config = config
        self.decisions = list(decisions)
        self.observers = []
        self.environ_snapshots = []

    def add_observer(self, observer):
        self.observers.append(observer)
        owner = self

        class Subscription:
            def close(self):
                if observer in owner.observers:
                    owner.observers.remove(observer)

        return Subscription()

    def _emit(self, record):
        for observer in tuple(self.observers):
            observer(record)

    async def achat(self, messages, *, tools=None, **kwargs):
        observation = CallObservation(
            self._emit,
            provider="fake",
            model="fake",
            method="achat",
        )
        self.environ_snapshots.append(dict(os.environ))
        try:
            if tools:
                content = "agent completed"
            else:
                if not self.decisions:
                    raise AssertionError("unexpected manager call")
                content = json.dumps(
                    self.decisions.pop(0).to_dict(),
                    sort_keys=True,
                )
            return SimpleNamespace(
                content=content,
                tool_calls=None,
                usage={"prompt_tokens": 4, "completion_tokens": 2},
                finish_reason="stop",
                outcome=normalize_finish_reason("stop", False),
            )
        finally:
            observation.finish()

    async def achat_stream(self, messages, *, tools=None, **kwargs):
        observation = CallObservation(
            self._emit,
            provider="fake",
            model="fake",
            method="achat_stream",
        )
        self.environ_snapshots.append(dict(os.environ))
        try:
            yield SimpleNamespace(content="agent stream chunk")
        finally:
            observation.finish()


class ScriptedRoleAgent:
    def __init__(self, workspace, **kwargs):
        self.workspace = workspace
        self.kwargs = kwargs
        self.closed = False

    async def prompt_stream(self, prompt):
        tool_names = {tool.name for tool in self.kwargs["tools"]}
        async for _chunk in self.kwargs["llm"].achat_stream(
            [Message(role="user", content=prompt)],
            tools=[{"name": "probe"}],
        ):
            pass
        if "write" in tool_names:
            (self.workspace / "answer.txt").write_text("verified\n", encoding="utf-8")
            text = "created answer.txt"
        else:
            await self.kwargs["session"].add_message_async(
                "tool",
                "1 passed",
                tool_call_id="audit-tool-1",
                tool_name="bash",
                is_error=False,
                terminate=False,
            )
            yield ToolExecutionStart(
                tool_call_id="audit-tool-1",
                tool_name="bash",
                args={"command": "python -m pytest -q"},
            )
            text = json.dumps(
                AuditReport(
                    status="complete",
                    summary="verified",
                    evidence=("answer.txt",),
                    verified_facts=("answer.txt contains verified",),
                    remaining=(),
                ).to_dict(),
                sort_keys=True,
            )
        message = Message(
            role="assistant",
            content=text,
            metadata={"usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        )
        yield MessageEnd(message=message)
        yield AgentEnd(
            messages=[message],
            final_text=text,
            iterations=1,
            stop_reason="end_turn",
        )

    async def aclose(self):
        self.closed = True


class ManagerOnlyBackend:
    def __init__(self, store, decision):
        self.store = store
        self.decision = decision

    def _episode(self, round_index):
        path = (
            self.store.root
            / "sessions"
            / f"round_{round_index:04d}_manager.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"role": "manager", "round": round_index}) + "\n",
            encoding="utf-8",
        )
        return RoleEpisode(
            output=json.dumps(self.decision.to_dict(), sort_keys=True),
            terminal="end_turn",
            iterations=1,
            tool_names=(),
            usage=(),
            session_path=str(path),
            cleanup_ok=True,
        )

    async def manager(self, *, task, state, round_index):
        return self._episode(round_index)

    async def executor(self, **kwargs):
        raise AssertionError("executor must not run")

    async def auditor(self, **kwargs):
        raise AssertionError("auditor must not run")


def _limits(**overrides):
    values = {
        "max_rounds": 4,
        "max_turns": 10,
        "max_seconds": 30,
        "context_budget": 4096,
        "snapshot_max_files": 20,
        "snapshot_max_bytes": 4096,
    }
    values.update(overrides)
    return values


def _prepared_run(tmp_path, *, base_url="https://frozen.example/v1"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    runs = tmp_path / "runs"
    model = {
        "provider": "openai",
        "model": "deepseek-v4.1-flash",
        "base_url": base_url,
        "temperature": 0.2,
        "timeout": 30,
        "max_tokens": 128,
        "max_retries": 0,
    }
    store = LongHorizonStore.prepare(
        runs / "run-1",
        run_id="run-1",
        task="existing task\n",
        workspace=workspace,
        model=model,
        limits=_limits(),
    )
    return store, runs


def _install_scripted_role_backend(monkeypatch, workspace):
    def backend_factory(**kwargs):
        return LLMRoleBackend(
            **kwargs,
            agent_factory=lambda **agent_kwargs: ScriptedRoleAgent(
                workspace,
                **agent_kwargs,
            ),
        )

    monkeypatch.setattr(cli, "LLMRoleBackend", backend_factory)


def test_run_hashes_task_file_and_never_persists_credentials(tmp_path, capsys):
    task_file = tmp_path / "task.md"
    task_file.write_text("Implement the CLI.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    secret = "sk-never-persist-this"
    auth = FakeAuthManager(key=secret)
    llms = []

    def llm_factory(config):
        llm = FakeLLM(config)
        llms.append(llm)
        return llm

    code = cli.main(
        [
            "run",
            "--task",
            f"@{task_file}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(runs),
            "--run-id",
            "task-hash",
            "--provider",
            "openai",
            "--model",
            "deepseek-v4.1-flash",
            "--base-url",
            "https://frozen.example/v1",
            "--auth-profile",
            "work",
            "--json",
        ],
        controller_factory=controller_factory,
        llm_factory=llm_factory,
        auth_manager_factory=lambda: auth,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    manifest = json.loads((runs / "task-hash" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_sha256"] == hashlib.sha256(task_file.read_bytes()).hexdigest()
    assert manifest["model"]["max_retries"] == 0
    assert secret not in json.dumps(manifest)
    assert secret not in (runs / "task-hash" / "journal.jsonl").read_text(encoding="utf-8")
    assert secret not in json.dumps(output)
    assert auth.calls == [("openai", "work")]
    assert len(llms) == 1 and llms[0].config.api_key == secret
    assert (runs / "task-hash" / "calls.jsonl").is_file()
    assert FakeController.last.backend.llm is llms[0]


def test_status_is_read_only_and_never_loads_auth(tmp_path, capsys, monkeypatch):
    store, runs = _prepared_run(tmp_path)
    journal = (store.root / "journal.jsonl").read_bytes()
    before = {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    integrity_calls = []
    original_inspect = LongHorizonStore.inspect_integrity

    def counted_inspect(self):
        integrity_calls.append(self.root)
        return original_inspect(self)

    monkeypatch.setattr(LongHorizonStore, "inspect_integrity", counted_inspect)

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not construct credentials or an LLM")

    code = cli.main(
        ["status", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert output["state"]["next_round"] == 1
    assert output["state"] == output["integrity"]["result"]["state"]
    assert integrity_calls == [store.root]
    assert (store.root / "journal.jsonl").read_bytes() == journal
    after = {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert after == before


def test_status_integrity_failure_blocks_without_auth(
    tmp_path,
    capsys,
    monkeypatch,
):
    _store, runs = _prepared_run(tmp_path)

    def broken_integrity(self):
        raise StoreIntegrityError("workspace digest differs")

    monkeypatch.setattr(LongHorizonStore, "inspect_integrity", broken_integrity)

    def forbidden(*args, **kwargs):
        raise AssertionError("status integrity gate must not load auth")

    code = cli.main(
        ["status", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"


def test_resume_rejects_model_endpoint_mismatch_before_auth(tmp_path, capsys):
    _store, runs = _prepared_run(tmp_path, base_url="https://a.example/v1")

    def forbidden(*args, **kwargs):
        raise AssertionError("endpoint mismatch must fail before auth")

    code = cli.main(
        [
            "resume",
            "run-1",
            "--runs-root",
            str(runs),
            "--base-url",
            "https://b.example/v1",
            "--json",
        ],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code != 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert "endpoint" in output["error"].lower()


def test_resume_integrity_precedes_auth_and_credentials(
    tmp_path,
    capsys,
    monkeypatch,
):
    _store, runs = _prepared_run(tmp_path)

    def broken_integrity(self):
        raise StoreIntegrityError("session evidence is invalid")

    monkeypatch.setattr(LongHorizonStore, "inspect_integrity", broken_integrity)

    def forbidden(*args, **kwargs):
        raise AssertionError("integrity failure must precede auth")

    code = cli.main(
        ["resume", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"


def test_follow_up_integrity_precedes_controller(
    tmp_path,
    capsys,
    monkeypatch,
):
    _store, runs = _prepared_run(tmp_path)

    def broken_integrity(self):
        raise StoreIntegrityError("checkpoint evidence is invalid")

    monkeypatch.setattr(LongHorizonStore, "inspect_integrity", broken_integrity)

    def forbidden(*args, **kwargs):
        raise AssertionError("integrity failure must precede controller")

    code = cli.main(
        [
            "follow-up",
            "run-1",
            "--runs-root",
            str(runs),
            "--message",
            "continue",
            "--json",
        ],
        controller_factory=forbidden,
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"


def test_follow_up_and_restore_dispatch_without_auth(tmp_path, capsys):
    store, runs = _prepared_run(tmp_path)
    asyncio.run(
        LongHorizonController(
            store,
            ManagerOnlyBackend(
                store,
                ManagerDecision(
                    next="ask",
                    reason="input required",
                    question="Continue?",
                ),
            ),
        ).run()
    )
    followup = tmp_path / "follow.md"
    followup.write_text("Also update the README.\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("non-model commands must not load credentials")

    assert cli.main(
        [
            "follow-up",
            "run-1",
            "--runs-root",
            str(runs),
            "--message",
            f"@{followup}",
            "--json",
        ],
        controller_factory=controller_factory,
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
    ) == 0
    follow_output = json.loads(capsys.readouterr().out)
    assert follow_output["status"] == "ok"
    assert follow_output["state"]["status"] == "waiting_input"
    assert FakeController.last.calls == [
        ("follow_up", followup.read_bytes().decode("utf-8"))
    ]
    assert FakeController.last.backend is None

    assert cli.main(
        ["restore", "run-1", "--runs-root", str(runs), "--round", "2", "--json"],
        controller_factory=controller_factory,
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
    ) == 0
    restore_output = json.loads(capsys.readouterr().out)
    assert restore_output["status"] == "ok"
    assert FakeController.last.calls == [("restore", 2)]
    assert FakeController.last.backend is None


def test_repeatable_flags_use_last_value_and_resume_builds_controller(tmp_path, capsys):
    task = tmp_path / "task.txt"
    task.write_text("task", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    secret = "sk-repeat-test"
    auth = FakeAuthManager(key=secret)
    llm = FakeLLM(None)

    def llm_factory(config):
        llm.config = config
        return llm

    assert cli.main(
        [
            "run",
            "--task",
            f"@{task}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(runs),
            "--run-id",
            "repeat",
            "--model",
            "first-model",
            "--model",
            "final-model",
            "--max-rounds",
            "2",
            "--max-rounds",
            "3",
            "--base-url",
            "https://frozen.example/v1",
            "--json",
        ],
        controller_factory=controller_factory,
        llm_factory=llm_factory,
        auth_manager_factory=lambda: auth,
    ) == 0
    capsys.readouterr()
    manifest = json.loads((runs / "repeat" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model"]["model"] == "final-model"
    assert manifest["limits"]["max_rounds"] == 3

    assert cli.main(
        [
            "resume",
            "repeat",
            "--runs-root",
            str(runs),
            "--model",
            "final-model",
            "--base-url",
            "https://frozen.example/v1",
            "--json",
        ],
        controller_factory=controller_factory,
        llm_factory=llm_factory,
        auth_manager_factory=lambda: auth,
    ) == 0
    resume_output = json.loads(capsys.readouterr().out)
    assert resume_output["status"] == "ready"
    assert FakeController.last.store.manifest["run_id"] == "repeat"
    assert FakeController.last.calls == [("resume",)]


def test_cli_real_controller_scripted_roles_share_trace_and_scrub_secrets(
    tmp_path,
    capsys,
    monkeypatch,
):
    task = tmp_path / "task.txt"
    task.write_text("Create answer.txt and verify it.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    secret_env = "LONGHORIZON_TEST_KEY"
    secret = "sk-offline-role-secret"
    monkeypatch.setenv(secret_env, secret)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-offline-secret")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-langfuse-secret")
    decisions = [
        ManagerDecision(
            next="execute",
            reason="create the requested file",
            subtask="write answer.txt",
            acceptance_criteria=("answer.txt contains verified",),
        ),
        ManagerDecision(next="done", reason="audit verified the change"),
    ]
    llm = ScriptedDecisionLLM(None, decisions)
    records = []
    llm.add_observer(records.append)
    _install_scripted_role_backend(monkeypatch, workspace)

    def llm_factory(config):
        llm.config = config
        return llm

    code = cli.main(
        [
            "run",
            "--task",
            f"@{task}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(runs),
            "--run-id",
            "trace-run",
            "--credential-env",
            secret_env,
            "--base-url",
            "https://frozen.example/v1",
            "--max-rounds",
            "3",
            "--json",
        ],
        llm_factory=llm_factory,
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "complete"
    starts = [record for record in records if type(record) is CallStart]
    assert {record.root_id for record in starts} == {"trace-run"}
    assert [record.purpose for record in starts] == [
        "longhorizon_manager_r1",
        "longhorizon_executor_r1",
        "longhorizon_auditor_r1",
        "longhorizon_manager_r2",
    ]
    assert secret_env not in os.environ
    assert "LANGFUSE_PUBLIC_KEY" not in os.environ
    assert "LANGFUSE_SECRET_KEY" not in os.environ
    assert all(
        secret_env not in snapshot
        and "LANGFUSE_PUBLIC_KEY" not in snapshot
        and "LANGFUSE_SECRET_KEY" not in snapshot
        for snapshot in llm.environ_snapshots
    )
    run_root = runs / "trace-run"
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (run_root / "manifest.json", run_root / "journal.jsonl")
    )
    assert secret not in persisted
    assert secret not in json.dumps(output)
    assert llm.config.api_key == secret

    def forbidden(*args, **kwargs):
        raise AssertionError("completed resume must not load auth")

    resume_code = cli.main(
        [
            "resume",
            "trace-run",
            "--runs-root",
            str(runs),
            "--json",
        ],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert resume_code == 2
    resume_output = json.loads(capsys.readouterr().out)
    assert resume_output["status"] == "blocked"
    assert "complete" in resume_output["error"]


def test_cli_real_controller_blocked_state_returns_nonzero(tmp_path, capsys):
    task = tmp_path / "task.txt"
    task.write_text("Do something unsafe.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    llm = ScriptedDecisionLLM(
        None,
        [ManagerDecision(next="blocked", reason="no safe action remains")],
    )

    code = cli.main(
        [
            "run",
            "--task",
            f"@{task}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(runs),
            "--run-id",
            "blocked-run",
            "--auth-profile",
            "test",
            "--base-url",
            "https://frozen.example/v1",
            "--json",
        ],
        llm_factory=lambda config: (
            setattr(llm, "config", config) or llm
        ),
        auth_manager_factory=lambda: FakeAuthManager(key="offline-key"),
    )

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert output["state"]["status"] == "blocked"

    status_code = cli.main(
        ["status", "blocked-run", "--runs-root", str(runs), "--json"],
        auth_manager_factory=lambda: (_ for _ in ()).throw(
            AssertionError("status must not load auth")
        ),
    )
    assert status_code == 2
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["status"] == "blocked"
    assert status_output["state"]["status"] == "blocked"


def test_resume_waiting_without_follow_up_returns_before_auth(tmp_path, capsys):
    store, runs = _prepared_run(tmp_path)
    decision = ManagerDecision(
        next="ask",
        reason="human decision required",
        question="Which value?",
    )
    asyncio.run(
        LongHorizonController(
            store,
            ManagerOnlyBackend(store, decision),
        ).run()
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("waiting resume without follow-up must not load auth")

    code = cli.main(
        ["resume", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "waiting_input"
    assert output["result"]["resume"] == "not_needed"

    status_code = cli.main(
        ["status", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
    )
    assert status_code == 0
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["status"] == "waiting_input"


def test_resume_blocked_is_rejected_before_auth(tmp_path, capsys):
    store, runs = _prepared_run(tmp_path)
    decision = ManagerDecision(next="blocked", reason="cannot continue safely")
    asyncio.run(
        LongHorizonController(
            store,
            ManagerOnlyBackend(store, decision),
        ).run()
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("blocked resume must not load auth")

    code = cli.main(
        ["resume", "run-1", "--runs-root", str(runs), "--json"],
        auth_manager_factory=forbidden,
        llm_factory=forbidden,
        controller_factory=forbidden,
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert "blocked" in output["error"]


def test_resource_cleanup_continues_after_failures_and_checks_observer_counters():
    events = []

    class FailingController:
        async def aclose(self):
            events.append("controller")
            raise RuntimeError("must not escape")

    class Observer:
        def __init__(self, name, *, dropped=0):
            self.name = name
            self.dropped = dropped
            self.export_errors = 0
            self.close_errors = 0
            self.is_alive = False

        def close(self):
            events.append(self.name)
            return True

    class Client:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            events.append("client")

    shared = Client()
    cloud = Observer("cloud")
    local = Observer("local", dropped=1)
    llm = SimpleNamespace(
        _provider=SimpleNamespace(async_client=shared, client=shared)
    )

    report, ok = asyncio.run(
        cli._close_owned_resources(
            controller=FailingController(),
            cloud=cloud,
            local=local,
            llm=llm,
        )
    )

    assert ok is False
    assert events == ["controller", "cloud", "local", "client"]
    assert shared.calls == 1
    assert report["ok"] is False


def test_controller_factory_failure_still_closes_local_observer(
    tmp_path,
    capsys,
    monkeypatch,
):
    task = tmp_path / "task.txt"
    task.write_text("task", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    closed = []

    class Observer:
        dropped = 0
        export_errors = 0
        close_errors = 0
        is_alive = False

        def close(self):
            closed.append(True)
            return True

    monkeypatch.setattr(
        cli,
        "_attach_local_observation",
        lambda llm, run_root: Observer(),
    )

    def failing_controller_factory(**kwargs):
        raise RuntimeError("constructor failed")

    code = cli.main(
        [
            "run",
            "--task",
            f"@{task}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(tmp_path / "runs"),
            "--run-id",
            "constructor-failure",
            "--auth-profile",
            "test",
            "--base-url",
            "https://frozen.example/v1",
            "--json",
        ],
        controller_factory=failing_controller_factory,
        llm_factory=lambda config: FakeLLM(config),
        auth_manager_factory=lambda: FakeAuthManager(key="offline-key"),
    )

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert closed == [True]


def test_observer_export_failure_maps_to_safe_blocked_result(
    tmp_path,
    capsys,
    monkeypatch,
):
    task = tmp_path / "task.txt"
    task.write_text("task", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "sk-must-not-leak"

    class DroppedObserver:
        dropped = 1
        export_errors = 0
        close_errors = 0
        is_alive = False

        def close(self):
            return True

    monkeypatch.setattr(
        cli,
        "_attach_local_observation",
        lambda llm, run_root: DroppedObserver(),
    )

    code = cli.main(
        [
            "run",
            "--task",
            f"@{task}",
            "--workspace",
            str(workspace),
            "--runs-root",
            str(tmp_path / "runs"),
            "--run-id",
            "observer-failure",
            "--auth-profile",
            "test",
            "--base-url",
            "https://frozen.example/v1",
            "--json",
        ],
        controller_factory=controller_factory,
        llm_factory=lambda config: FakeLLM(config),
        auth_manager_factory=lambda: FakeAuthManager(key=secret),
    )

    assert code == 2
    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["status"] == "blocked"
    assert output["error_type"] == "CLIError"
    assert secret not in output_text
