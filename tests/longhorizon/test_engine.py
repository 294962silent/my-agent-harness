from __future__ import annotations

import asyncio
import functools
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from my_coding_agent.longhorizon.engine import ControllerError, LongHorizonController
from my_coding_agent.longhorizon.schema import (
    AuditReport,
    ManagerDecision,
    RoleEpisode,
    canonical_json,
    digest_value,
)
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
        self.executor_states: list[dict] = []
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
        self.executor_states.append(json.loads(json.dumps(dict(state))))
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


def _store(
    tmp_path: Path,
    *,
    max_rounds: int = 5,
    immutable_paths: tuple[str, ...] = (),
    protected_directory: bool = False,
) -> LongHorizonStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    workspace.joinpath("seed.txt").write_text("seed\n", encoding="utf-8")
    if protected_directory:
        protected = workspace / "protected"
        protected.mkdir()
        protected.joinpath("keep.txt").write_text("keep\n", encoding="utf-8")
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
        immutable_paths=immutable_paths,
    )


def _execute(reason: str = "edit") -> ManagerDecision:
    return ManagerDecision(
        next="execute",
        reason=reason,
        subtask="edit answer.txt",
        acceptance_criteria=("answer.txt exists",),
    )


def _make_legacy_prepared_run(
    store: LongHorizonStore,
) -> tuple[LongHorizonStore, bytes, bytes]:
    manifest = store.manifest
    manifest.pop("immutable_paths")
    manifest_payload = canonical_json(manifest) + b"\n"
    store.root.joinpath("manifest.json").write_bytes(manifest_payload)
    event = {
        "schema_version": 1,
        "sequence": 0,
        "previous": None,
        "manifest_sha256": digest_value(manifest),
        "kind": "prepared",
        "payload": {},
        "time_unix": 1,
    }
    event["sha256"] = digest_value(event)
    journal_payload = canonical_json(event) + b"\n"
    store.root.joinpath("journal.jsonl").write_bytes(journal_payload)
    return LongHorizonStore.load(store.root), manifest_payload, journal_payload


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


@pytest.mark.parametrize("entrypoint", ["run", "run_until_terminal"])
@_async_test
async def test_pause_after_checkpoint_resumes_at_next_round(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    store = _store(tmp_path, max_rounds=4, immutable_paths=("seed.txt",))
    first_backend = FakeBackend(
        store,
        managers=[_execute("first")],
        audits=[
            AuditReport(
                status="incomplete",
                summary="ready for next round",
                evidence=("answer.txt",),
                remaining=("finish",),
            )
        ],
    )
    controller = LongHorizonController(store, first_backend)
    paused = await getattr(controller, entrypoint)(pause_after_round=1)

    assert paused.status == "ready"
    assert paused.next_round == 2
    assert paused.completed_rounds == (1,)
    assert first_backend.executor_calls == 1
    assert first_backend.auditor_calls == 1
    assert first_backend.manager_states[0]["immutable_paths"] == ["seed.txt"]
    assert first_backend.executor_states[0]["immutable_paths"] == ["seed.txt"]
    assert (store.root / "checkpoints" / "0001.json").is_file()
    status = json.loads((store.root / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "ready"

    second_backend = FakeBackend(
        store,
        managers=[_execute("second")],
        audits=[
            AuditReport(
                status="complete",
                summary="second round verified",
                evidence=("answer.txt",),
            )
        ],
    )
    paused_again = await LongHorizonController(store, second_backend).resume(
        pause_after_round=2
    )
    assert paused_again.status == "ready"
    assert paused_again.next_round == 3
    assert paused_again.completed_rounds == (1, 2)
    assert second_backend.manager_states[0]["next_round"] == 2
    assert second_backend.executor_calls == 1
    assert (store.root / "checkpoints" / "0002.json").is_file()

    final_backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="done", reason="verified")],
        audits=[],
    )
    completed = await LongHorizonController(store, final_backend).resume()
    assert completed.status == "complete"
    assert completed.completed_rounds == (1, 2, 3)
    assert final_backend.executor_calls == 0
    assert (store.root / "checkpoints" / "0003.json").is_file()
    assert (store.root / "checkpoints" / "final.json").is_file()


@_async_test
async def test_legacy_manifest_status_and_resume_preserve_identity(
    tmp_path: Path,
) -> None:
    current = _store(tmp_path)
    store, manifest_payload, prepared_payload = _make_legacy_prepared_run(current)
    assert "immutable_paths" not in store.manifest
    backend = FakeBackend(
        store,
        managers=[ManagerDecision(next="blocked", reason="legacy completed")],
        audits=[],
    )
    controller = LongHorizonController(store, backend)

    assert controller.status().status == "ready"
    resumed = await controller.resume()

    assert resumed.status == "blocked"
    assert controller.status().status == "blocked"
    assert backend.manager_states[0]["immutable_paths"] == []
    assert store.root.joinpath("manifest.json").read_bytes() == manifest_payload
    journal = store.root.joinpath("journal.jsonl").read_bytes()
    assert journal.startswith(prepared_payload)
    for event in store.events()[1:]:
        assert event["manifest_sha256"] == digest_value(store.manifest)


@_async_test
async def test_executor_immutable_mutation_blocks_before_audit(tmp_path: Path) -> None:
    store = _store(tmp_path, immutable_paths=("seed.txt",))

    class MutatingExecutor(FakeBackend):
        async def executor(
            self,
            *,
            task: str,
            decision: ManagerDecision,
            state: dict,
            round_index: int,
        ) -> RoleEpisode:
            episode = await super().executor(
                task=task,
                decision=decision,
                state=state,
                round_index=round_index,
            )
            self.store.workspace.joinpath("seed.txt").write_text(
                "changed\n",
                encoding="utf-8",
            )
            return episode

    backend = MutatingExecutor(
        store,
        managers=[_execute()],
        audits=[
            AuditReport(
                status="complete",
                summary="should not run",
                evidence=("seed.txt",),
            )
        ],
    )
    with pytest.raises(ControllerError, match="immutable path changed"):
        await LongHorizonController(store, backend).run()

    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert backend.executor_calls == 1
    assert backend.auditor_calls == 0
    assert store.events()[-1]["kind"] == "run_blocked"
    assert not (store.root / "checkpoints" / "0001.json").exists()


@_async_test
async def test_delayed_executor_write_is_detected_in_post_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, max_rounds=1, immutable_paths=("seed.txt",))
    gate = threading.Event()
    mutation_finished = threading.Event()
    background_errors: list[BaseException] = []

    class DelayedExecutor(FakeBackend):
        background: threading.Thread | None = None

        async def executor(
            self,
            *,
            task: str,
            decision: ManagerDecision,
            state: dict,
            round_index: int,
        ) -> RoleEpisode:
            episode = await super().executor(
                task=task,
                decision=decision,
                state=state,
                round_index=round_index,
            )

            def mutate() -> None:
                try:
                    if not gate.wait(timeout=5):
                        raise TimeoutError("post-snapshot gate was not opened")
                    self.store.workspace.joinpath("seed.txt").write_text(
                        "late change\n",
                        encoding="utf-8",
                    )
                except BaseException as exc:
                    background_errors.append(exc)
                finally:
                    mutation_finished.set()

            self.background = threading.Thread(target=mutate)
            self.background.start()
            return episode

    original_capture = store.capture_workspace
    capture_armed = False

    def capture_with_delayed_mutation():
        nonlocal capture_armed
        if not capture_armed:
            capture_armed = True
            gate.set()
            if not mutation_finished.wait(timeout=5):
                raise TimeoutError("delayed mutation did not finish")
            snapshot = original_capture()
            store.workspace.joinpath("seed.txt").write_text(
                "seed\n",
                encoding="utf-8",
            )
            return snapshot
        return original_capture()

    monkeypatch.setattr(store, "capture_workspace", capture_with_delayed_mutation)
    backend = DelayedExecutor(
        store,
        managers=[_execute()],
        audits=[
            AuditReport(
                status="complete",
                summary="must not audit",
                evidence=("seed.txt",),
            )
        ],
    )
    try:
        with pytest.raises(ControllerError, match="immutable path changed"):
            await LongHorizonController(store, backend).run()
    finally:
        if backend.background is not None:
            backend.background.join(timeout=5)

    assert backend.background is not None
    assert not backend.background.is_alive()
    assert background_errors == []
    assert backend.auditor_calls == 0
    assert reduce_events(store.manifest, store.events()).status == "blocked"


@_async_test
async def test_deleted_protected_file_allows_new_process_blocked_status(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, immutable_paths=("seed.txt",))

    class DeletingExecutor(FakeBackend):
        async def executor(
            self,
            *,
            task: str,
            decision: ManagerDecision,
            state: dict,
            round_index: int,
        ) -> RoleEpisode:
            episode = await super().executor(
                task=task,
                decision=decision,
                state=state,
                round_index=round_index,
            )
            self.store.workspace.joinpath("seed.txt").unlink()
            return episode

    backend = DeletingExecutor(
        store,
        managers=[_execute()],
        audits=[
            AuditReport(
                status="complete",
                summary="must not audit",
                evidence=("seed.txt",),
            )
        ],
    )
    with pytest.raises(ControllerError, match="immutable path changed"):
        await LongHorizonController(store, backend).run()

    reloaded = LongHorizonStore.load(store.root)
    reloaded_backend = FakeBackend(reloaded, managers=[], audits=[])
    status = LongHorizonController(reloaded, reloaded_backend).status()
    inspected = reloaded.inspect_integrity()

    assert status.status == "blocked"
    assert "immutable path changed" in status.terminal_reason
    assert inspected["state"]["status"] == "blocked"
    assert "immutable path changed" in inspected["state"]["terminal_reason"]


@pytest.mark.parametrize("mutation", ["add", "modify", "delete"])
@_async_test
async def test_executor_immutable_directory_change_blocks_before_audit(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = _store(
        tmp_path,
        immutable_paths=("protected",),
        protected_directory=True,
    )

    class MutatingDirectoryExecutor(FakeBackend):
        async def executor(
            self,
            *,
            task: str,
            decision: ManagerDecision,
            state: dict,
            round_index: int,
        ) -> RoleEpisode:
            episode = await super().executor(
                task=task,
                decision=decision,
                state=state,
                round_index=round_index,
            )
            protected = self.store.workspace / "protected"
            if mutation == "add":
                protected.joinpath("new.txt").write_text("new\n", encoding="utf-8")
            elif mutation == "modify":
                protected.joinpath("keep.txt").write_text(
                    "changed\n",
                    encoding="utf-8",
                )
            else:
                protected.joinpath("keep.txt").unlink()
            return episode

    backend = MutatingDirectoryExecutor(
        store,
        managers=[_execute()],
        audits=[
            AuditReport(
                status="complete",
                summary="should not run",
                evidence=("protected/keep.txt",),
            )
        ],
    )
    with pytest.raises(ControllerError, match="immutable path changed"):
        await LongHorizonController(store, backend).run()

    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert backend.auditor_calls == 0
    assert [event["kind"] for event in store.events()].count("round_unknown") == 1
    assert store.events()[-1]["kind"] == "run_blocked"


@_async_test
async def test_pause_target_must_be_after_last_completed_round(tmp_path: Path) -> None:
    store = _store(tmp_path, max_rounds=3)
    first_backend = FakeBackend(
        store,
        managers=[_execute()],
        audits=[
            AuditReport(
                status="complete",
                summary="first round",
                evidence=("answer.txt",),
            )
        ],
    )
    await LongHorizonController(store, first_backend).run(pause_after_round=1)

    second_backend = FakeBackend(store, managers=[], audits=[])
    with pytest.raises(ControllerError, match="later than the last completed round"):
        await LongHorizonController(store, second_backend).resume(
            pause_after_round=1
        )
    assert second_backend.manager_event_tails == []


@_async_test
async def test_pause_target_must_be_positive_before_first_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backend = FakeBackend(store, managers=[], audits=[])
    with pytest.raises(ControllerError, match="positive integer"):
        await LongHorizonController(store, backend).run(pause_after_round=0)
    assert backend.manager_event_tails == []


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
