from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import replace
from pathlib import Path

import pytest

from my_coding_agent.longhorizon.engine import ControllerError, LongHorizonController
from my_coding_agent.longhorizon.schema import AuditReport, ManagerDecision, RoleEpisode
from my_coding_agent.longhorizon.state import reduce_events
from my_coding_agent.longhorizon.store import LongHorizonStore, StoreIntegrityError


def _async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeBackend:
    def __init__(
        self,
        store: LongHorizonStore,
        *,
        managers: list[ManagerDecision],
        audits: list[AuditReport],
        mutate_on_audit: bool = False,
        write_executor: bool = True,
    ) -> None:
        self.store = store
        self.managers = list(managers)
        self.audits = list(audits)
        self.mutate_on_audit = mutate_on_audit
        self.write_executor = write_executor
        self.manager_states: list[dict] = []
        self.manager_event_tails: list[str] = []
        self.executor_calls = 0
        self.auditor_calls = 0
        self.auditor_workspaces: list[Path] = []
        self._session_counter = 0

    def _episode(
        self,
        role: str,
        output: str,
        round_index: int,
        *,
        cleanup_ok: bool = True,
    ) -> RoleEpisode:
        self._session_counter += 1
        suffix = (
            ""
            if role == "manager"
            else f"_{self._session_counter:012x}"
        )
        session = (
            self.store.root
            / "sessions"
            / f"round_{round_index:04d}_{role}{suffix}.jsonl"
        )
        session.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": output,
                    "metadata": {},
                },
            }
        ]
        if role == "auditor":
            rows.append(
                {
                    "type": "message",
                    "message": {
                        "role": "tool",
                        "content": "verified",
                        "metadata": {
                            "tool_name": "read",
                            "is_error": False,
                        },
                    },
                }
            )
        session.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return RoleEpisode(
            output=output,
            terminal="end_turn",
            iterations=1,
            tool_names=(),
            usage=({"prompt_tokens": 2, "completion_tokens": 1},),
            session_path=str(session),
            cleanup_ok=cleanup_ok,
        )

    async def manager(self, *, task: str, state: dict, round_index: int) -> RoleEpisode:
        events = self.store.events()
        self.manager_event_tails.append(events[-1]["kind"])
        self.manager_states.append(json.loads(json.dumps(dict(state))))
        if not self.managers:
            raise AssertionError("unexpected manager call")
        decision = self.managers.pop(0)
        return self._episode(
            "manager",
            json.dumps(decision.to_dict(), sort_keys=True),
            round_index,
        )

    async def executor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        state: dict,
        round_index: int,
    ) -> RoleEpisode:
        self.executor_calls += 1
        if self.write_executor:
            self.store.workspace.joinpath("answer.txt").write_text(
                f"round {round_index}\n",
                encoding="utf-8",
            )
        return self._episode(
            "executor",
            f"executor round {round_index}",
            round_index,
        )

    async def auditor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        executor_output: str,
        state: dict,
        round_index: int,
        workspace: Path,
    ) -> RoleEpisode:
        self.auditor_calls += 1
        self.auditor_workspaces.append(workspace)
        if self.mutate_on_audit:
            self.store.workspace.joinpath("auditor-mutated.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
        if not self.audits:
            raise AssertionError("unexpected auditor call")
        report = self.audits.pop(0)
        return self._episode(
            "auditor",
            json.dumps(report.to_dict(), sort_keys=True),
            round_index,
        )


def _store(tmp_path: Path, *, max_rounds: int = 5) -> LongHorizonStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    workspace.joinpath("seed.txt").write_text("seed\n", encoding="utf-8")
    return LongHorizonStore.prepare(
        tmp_path / "run",
        run_id="run",
        task="Fix the CLI.",
        workspace=workspace,
        model={"provider": "fake", "model": "fake", "max_retries": 0},
        limits={
            "max_rounds": max_rounds,
            "snapshot_max_files": 100,
            "snapshot_max_bytes": 1024 * 1024,
        },
    )


def _execute(reason: str = "edit") -> ManagerDecision:
    return ManagerDecision(
        next="execute",
        reason=reason,
        subtask="edit answer.txt",
        acceptance_criteria=("answer.txt exists",),
    )


@_async_test
async def test_round_started_is_durable_before_manager_call(tmp_path: Path) -> None:
    store = _store(tmp_path, max_rounds=1)
    backend = FakeBackend(
        store,
        managers=[
            ManagerDecision(next="blocked", reason="nothing safe to do"),
        ],
        audits=[],
    )
    controller = LongHorizonController(store, backend)
    state = await controller.run_until_terminal()
    assert backend.manager_event_tails == ["round_started"]
    assert state.status == "blocked"


@_async_test
async def test_clean_audit_is_required_before_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[_execute(), ManagerDecision(next="done", reason="verified")],
        audits=[
            AuditReport(
                status="complete",
                summary="ok",
                evidence=("answer.txt",),
                verified_facts=("answer exists",),
                remaining=(),
            )
        ],
    )
    controller = LongHorizonController(store, backend)
    state = await controller.run()
    assert state.status == "complete"
    assert state.completed_rounds == (1, 2)
    assert state.verified_facts == ("answer exists",)
    assert (store.root / "checkpoints" / "0001.json").is_file()
    assert (store.root / "checkpoints" / "0002.json").is_file()
    assert (store.root / "checkpoints" / "final.json").is_file()
    assert len(backend.auditor_workspaces) == 1
    audit_workspace = backend.auditor_workspaces[0]
    assert audit_workspace == store.root / "audit-workspaces" / "round_0001"
    assert audit_workspace != store.workspace
    assert (audit_workspace / "answer.txt").read_text(encoding="utf-8") == "round 1\n"
    answer = store.workspace / "answer.txt"
    expected = answer.read_text(encoding="utf-8")
    answer.write_text("changed\n", encoding="utf-8")
    controller.restore(2)
    assert answer.read_text(encoding="utf-8") == expected


@_async_test
async def test_incomplete_audit_continues_to_next_manager_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[_execute("first"), _execute("second"), ManagerDecision(next="done", reason="complete")],
        audits=[
            AuditReport(status="incomplete", summary="not yet", evidence=("answer.txt",)),
            AuditReport(status="complete", summary="ok", evidence=("answer.txt",), remaining=()),
        ],
    )
    state = await LongHorizonController(store, backend).run()
    assert state.status == "complete"
    assert state.completed_rounds == (1, 2, 3)
    assert backend.executor_calls == 2
    assert backend.auditor_calls == 2


@_async_test
async def test_waiting_follow_up_is_persisted_and_consumed_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[
            ManagerDecision(next="ask", reason="need input", question="Which value?"),
            _execute("apply answer"),
            ManagerDecision(next="done", reason="complete"),
        ],
        audits=[
            AuditReport(status="complete", summary="ok", evidence=("answer.txt",), remaining=()),
        ],
    )
    controller = LongHorizonController(store, backend)
    waiting = await controller.run()
    assert waiting.status == "waiting_input"
    controller.follow_up(message="use 42")
    inbox = store.read_inbox()
    assert len(inbox) == 1
    assert inbox[0]["text"] == "use 42"
    completed = await controller.resume()
    assert completed.status == "complete"
    assert backend.manager_states[1]["pending_follow_ups"] == inbox
    consumed = [event for event in store.events() if event["kind"] == "follow_up_consumed"]
    assert len(consumed) == 1
    assert store.read_inbox() == []


def test_follow_up_rejected_outside_waiting_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(store, managers=[], audits=[])
    with pytest.raises(ControllerError, match="waiting_input"):
        LongHorizonController(store, backend).follow_up(message="too early")


@_async_test
async def test_controller_aclose_closes_owned_backend(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class ClosableBackend(FakeBackend):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    backend = ClosableBackend(store, managers=[], audits=[])
    controller = LongHorizonController(store, backend)
    await controller.aclose()
    assert backend.closed is True


@_async_test
async def test_dangling_started_round_blocks_resume_without_backend_call(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    backend = FakeBackend(store, managers=[], audits=[])
    with pytest.raises(ControllerError, match="resume blocked"):
        await LongHorizonController(store, backend).resume()
    assert backend.manager_event_tails == []


@_async_test
async def test_auditor_mutation_writes_round_unknown_and_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[_execute()],
        audits=[AuditReport(status="complete", summary="ok", evidence=("answer.txt",))],
        mutate_on_audit=True,
    )
    with pytest.raises(ControllerError, match="auditor mutated"):
        await LongHorizonController(store, backend).run()
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert state.active_round == 1
    assert [event["kind"] for event in store.events()].count("round_unknown") == 1
    assert store.events()[-1]["kind"] == "run_blocked"
    assert not (store.root / "checkpoints" / "0001.json").exists()


@_async_test
async def test_resume_rejects_workspace_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="ask", reason="need input", question="Which?")],
        audits=[],
    )
    controller = LongHorizonController(store, backend)
    assert (await controller.run()).status == "waiting_input"
    store.workspace.joinpath("seed.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ControllerError, match="resume blocked"):
        await controller.resume()


@_async_test
async def test_max_rounds_stops_without_another_manager_call(tmp_path: Path) -> None:
    store = _store(tmp_path, max_rounds=1)
    backend = FakeBackend(
        store,
        managers=[_execute()],
        audits=[AuditReport(status="incomplete", summary="more work", evidence=("answer.txt",))],
    )
    with pytest.raises(ControllerError, match="max_rounds"):
        await LongHorizonController(store, backend, max_rounds=1).run()
    assert backend.manager_states.__len__() == 1
    assert store.events()[-1]["kind"] == "run_blocked"


@_async_test
async def test_cleanup_failure_writes_round_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class BadCleanupBackend(FakeBackend):
        async def manager(self, *, task: str, state: dict, round_index: int) -> RoleEpisode:
            episode = await super().manager(task=task, state=state, round_index=round_index)
            return replace(episode, cleanup_ok=False)

    backend = BadCleanupBackend(
        store,
        managers=[ManagerDecision(next="blocked", reason="no")],
        audits=[],
    )
    with pytest.raises(ControllerError, match="cleanup"):
        await LongHorizonController(store, backend).run()
    assert [event["kind"] for event in store.events()].count("round_unknown") == 1
    assert store.events()[-1]["kind"] == "run_blocked"


@_async_test
async def test_checkpoint_failure_after_round_completed_writes_run_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="blocked", reason="stop")],
        audits=[],
    )

    def fail_checkpoint(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_checkpoint", fail_checkpoint)
    with pytest.raises(ControllerError, match="disk full"):
        await LongHorizonController(store, backend).run()
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert store.events()[-1]["kind"] == "run_blocked"


@_async_test
async def test_waiting_follow_up_cannot_exceed_max_rounds(tmp_path: Path) -> None:
    store = _store(tmp_path, max_rounds=1)
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="ask", reason="input", question="Which?")],
        audits=[],
    )
    controller = LongHorizonController(store, backend, max_rounds=1)
    assert (await controller.run()).status == "waiting_input"
    controller.follow_up(message="continue")
    with pytest.raises(ControllerError, match="max_rounds"):
        await controller.resume()
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert store.events()[-1]["kind"] == "run_blocked"


@_async_test
async def test_final_checkpoint_failure_after_run_completed_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[_execute(), ManagerDecision(next="done", reason="verified")],
        audits=[
            AuditReport(
                status="complete",
                summary="ok",
                evidence=("answer.txt",),
                remaining=(),
            )
        ],
    )

    def fail_final(*args, **kwargs):
        raise OSError("final disk full")

    monkeypatch.setattr(store, "write_final_checkpoint", fail_final)
    with pytest.raises(ControllerError, match="final disk full"):
        await LongHorizonController(store, backend).run()
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert [event["kind"] for event in store.events()].count("run_completed") == 1
    assert store.events()[-1]["kind"] == "run_blocked"
    assert not (store.root / "checkpoints" / "final.json").exists()


@_async_test
async def test_resume_validates_historical_sessions_before_another_paid_round(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="ask", reason="input", question="Which?")],
        audits=[],
    )
    controller = LongHorizonController(store, backend)
    assert (await controller.run()).status == "waiting_input"
    manager_event = next(
        event for event in store.events() if event["kind"] == "manager_completed"
    )
    manager_session = Path(manager_event["payload"]["episode"]["session_path"])
    manager_session.unlink()
    controller.follow_up(message="continue")
    with pytest.raises(ControllerError, match="session"):
        await controller.resume()
    assert len(backend.manager_event_tails) == 1


@_async_test
async def test_status_uses_the_same_integrity_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="ask", reason="input", question="Which?")],
        audits=[],
    )
    controller = LongHorizonController(store, backend)
    assert (await controller.run()).status == "waiting_input"
    manager_event = next(
        event for event in store.events() if event["kind"] == "manager_completed"
    )
    Path(manager_event["payload"]["episode"]["session_path"]).unlink()
    with pytest.raises(StoreIntegrityError, match="session"):
        controller.status()
