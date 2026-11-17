from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from my_coding_agent.longhorizon.schema import AuditReport, ManagerDecision, RoleEpisode, digest_value
from my_coding_agent.longhorizon.state import reduce_events
from my_coding_agent.longhorizon.store import LongHorizonStore, StoreIntegrityError


TASK = "Fix the CLI program.\n"


def _episode(
    store: LongHorizonStore,
    role: str,
    round_index: int,
    *,
    cleanup_ok: bool = True,
    token: str = "abcdef123456",
) -> dict:
    sessions = store.root / "sessions"
    sessions.mkdir(exist_ok=True)
    session = sessions / f"round_{round_index:04d}_{role}_{token}.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "tool",
                    "content": "ok",
                    "metadata": {"tool_name": "read", "is_error": False},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return RoleEpisode(
        output=role,
        terminal="end_turn",
        iterations=1,
        tool_names=("read",),
        usage=({"prompt_tokens": 2, "completion_tokens": 1},),
        session_path=str(session),
        cleanup_ok=cleanup_ok,
    ).to_dict()


def _complete_round(store: LongHorizonStore, round_index: int) -> None:
    store.append("round_started", {"round": round_index})
    store.append(
        "manager_completed",
        {
            "round": round_index,
            "decision": ManagerDecision(
                next="execute",
                reason="edit",
                subtask="change answer",
                acceptance_criteria=("answer is 42",),
            ).to_dict(),
            "episode": _episode(store, "manager", round_index),
        },
    )
    store.append(
        "executor_completed",
        {
            "round": round_index,
            "episode": _episode(store, "executor", round_index),
        },
    )
    store.append(
        "auditor_completed",
        {
            "round": round_index,
            "episode": _episode(store, "auditor", round_index),
            "report": AuditReport(
                status="complete",
                summary="verified",
                evidence=("answer.py",),
                verified_facts=("answer is 42",),
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": round_index})


def _store(tmp_path: Path, *, limits: dict | None = None) -> tuple[LongHorizonStore, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    store = LongHorizonStore.prepare(
        workspace / ".run",
        run_id="run-1",
        task=TASK,
        workspace=workspace,
        model={"provider": "fake", "model": "fake-model", "max_retries": 0},
        limits=limits or {"max_rounds": 4, "snapshot_max_files": 20, "snapshot_max_bytes": 4096},
    )
    return store, workspace


def test_prepare_manifest_task_and_hash_chain(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    assert store.manifest["run_id"] == "run-1"
    assert store.manifest["task_sha256"] == hashlib.sha256(TASK.encode()).hexdigest()
    assert store.manifest["initial_workspace_sha256"] == store.workspace_digest()
    assert (store.root / "task.txt").read_text(encoding="utf-8") == TASK
    events = store.events()
    assert [event["kind"] for event in events] == ["prepared"]
    assert events[0]["previous"] is None
    assert events[0]["sha256"] == digest_value({key: value for key, value in events[0].items() if key != "sha256"})
    first = store.append("round_started", {"round": 1})
    assert first["previous"] == events[0]["sha256"]
    assert store.events()[1]["sequence"] == 1
    assert store.root.resolve().is_relative_to(workspace.resolve())


def test_inspect_integrity_checks_initial_workspace_without_checkpoint(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    assert store.inspect_integrity()["state"]["status"] == "ready"
    workspace.joinpath("unexpected.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(StoreIntegrityError, match="initial workspace"):
        store.inspect_integrity()


def test_prepare_rejects_credentials_in_model_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(StoreIntegrityError, match="credential"):
        LongHorizonStore.prepare(
            tmp_path / "run",
            run_id="run",
            task="task",
            workspace=workspace,
            model={"provider": "openai", "api_key": "secret"},
            limits={"max_rounds": 1},
        )


def test_journal_tamper_torn_and_duplicate_sequence_fail_closed(tmp_path: Path) -> None:
    store, _workspace = _store(tmp_path)
    journal = store.root / "journal.jsonl"
    store.append("round_started", {"round": 1})
    original = journal.read_bytes()
    journal.write_bytes(original.replace(b'"round":1', b'"round":2'))
    with pytest.raises(StoreIntegrityError):
        store.events()
    journal.write_bytes(original[:-1])
    with pytest.raises(StoreIntegrityError):
        store.events()
    rows = [json.loads(line) for line in original.splitlines()]
    rows.append(dict(rows[-1]))
    journal.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    with pytest.raises(StoreIntegrityError):
        store.events()


def test_append_rejects_invalid_order_without_growing_journal(tmp_path: Path) -> None:
    store, _workspace = _store(tmp_path)
    before = (store.root / "journal.jsonl").read_bytes()
    with pytest.raises(StoreIntegrityError, match="would invalidate"):
        store.append("round_started", {"round": 2})
    assert (store.root / "journal.jsonl").read_bytes() == before


def test_append_fsyncs_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _workspace = _store(tmp_path)
    calls = 0
    original = os.fsync

    def counted(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        original(descriptor)

    monkeypatch.setattr(os, "fsync", counted)
    store.append("round_started", {"round": 1})
    assert calls >= 1


def test_snapshot_ignores_runtime_and_is_bounded(tmp_path: Path) -> None:
    store, workspace = _store(
        tmp_path,
        limits={"max_rounds": 4, "snapshot_max_files": 2, "snapshot_max_bytes": 32},
    )
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "value.txt").write_text("value", encoding="utf-8")
    for ignored in (".git", ".venv", "__pycache__", ".pytest_cache"):
        (workspace / ignored).mkdir()
        (workspace / ignored / "noise").write_text("x", encoding="utf-8")
    snapshot = store.capture_workspace()
    assert snapshot.file_count == 2
    assert snapshot.byte_count == 9
    assert snapshot.path.is_dir()
    (workspace / "third.txt").write_text("3", encoding="utf-8")
    with pytest.raises(StoreIntegrityError):
        store.capture_workspace()


def test_checkpoint_has_sidecar_hash_and_explicit_restore(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    answer = workspace / "answer.py"
    answer.write_text("answer = 42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    state = reduce_events(store.manifest, store.events())
    checkpoint = store.write_checkpoint(
        1,
        asdict(state),
        snapshot,
    )
    checkpoint_path = store.root / "checkpoints" / "0001.json"
    assert checkpoint_path.is_file()
    assert (store.root / "checkpoints" / "0001.sha256").read_text().strip() == checkpoint["checkpoint_sha256"]
    assert hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == checkpoint["checkpoint_sha256"]
    answer.write_text("answer = 0\n", encoding="utf-8")
    (workspace / "extra.txt").write_text("extra", encoding="utf-8")
    loaded = store.load_checkpoint(1)
    assert loaded["round_index"] == 1
    store.restore_checkpoint(1)
    assert answer.read_text(encoding="utf-8") == "answer = 42\n"
    assert not (workspace / "extra.txt").exists()


def test_checkpoint_detects_snapshot_drift(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    (workspace / "answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    store.write_checkpoint(1, asdict(reduce_events(store.manifest, store.events())), snapshot)
    stored = snapshot.path / "tree" / "answer.py"
    stored.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(StoreIntegrityError):
        store.load_checkpoint(1)


def test_snapshot_rejects_symlink(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(StoreIntegrityError):
        store.capture_workspace()


def test_dangling_started_round_blocks_and_completed_state_cannot_resume(tmp_path: Path) -> None:
    store, _workspace = _store(tmp_path)
    store.append("round_started", {"round": 1})
    with pytest.raises(StoreIntegrityError):
        store.resume_state()

    completed, _workspace = _store(tmp_path / "completed")
    _complete_round(completed, 1)
    completed.append("round_started", {"round": 2})
    finished = ManagerDecision(next="done", reason="finished")
    completed.append(
        "manager_completed",
        {
            "round": 2,
            "decision": finished.to_dict(),
            "episode": _episode(completed, "manager", 2),
        },
    )
    completed.append("round_completed", {"round": 2})
    completed.append(
        "run_completed",
        {"decision": finished.to_dict()},
    )
    with pytest.raises(StoreIntegrityError):
        completed.resume_state()


def test_resume_rejects_workspace_drift_from_latest_checkpoint(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    answer = workspace / "answer.py"
    answer.write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    store.write_checkpoint(1, asdict(reduce_events(store.manifest, store.events())), snapshot)
    answer.write_text("0\n", encoding="utf-8")
    with pytest.raises(StoreIntegrityError, match="workspace drifted"):
        store.resume_state()


def test_inspect_integrity_checks_session_evidence_and_round_checkpoint(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    session = store.root / "sessions" / "round_0001_manager.jsonl"
    session.parent.mkdir()
    session.write_text('{"manager":true}\n', encoding="utf-8")
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="blocked", reason="stop").to_dict(),
            "episode": RoleEpisode(
                output="blocked",
                terminal="end_turn",
                iterations=1,
                tool_names=(),
                usage=(),
                session_path=str(session),
                cleanup_ok=True,
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": 1})
    state = reduce_events(store.manifest, store.events())
    store.write_checkpoint(1, asdict(state), snapshot)
    inspected = store.inspect_integrity()
    assert inspected["state"]["status"] == "blocked"
    assert inspected["completed_rounds"] == [1]
    session.unlink()
    with pytest.raises(StoreIntegrityError, match="session"):
        store.inspect_integrity()


def test_inspect_integrity_rejects_duplicate_session_paths(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    shared = _episode(store, "manager", 1)
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(
                next="execute",
                reason="edit",
                subtask="write answer",
                acceptance_criteria=("verified",),
            ).to_dict(),
            "episode": shared,
        },
    )
    store.append("executor_completed", {"round": 1, "episode": shared})
    store.append(
        "auditor_completed",
        {
            "round": 1,
            "episode": _episode(store, "auditor", 1),
            "report": AuditReport(
                status="complete",
                summary="ok",
                evidence=("answer.py",),
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": 1})
    store.write_checkpoint(
        1,
        asdict(reduce_events(store.manifest, store.events())),
        snapshot,
    )
    with pytest.raises(StoreIntegrityError, match="session paths must be unique"):
        store.inspect_integrity()


@pytest.mark.parametrize(
    ("bad_content", "message"),
    [
        ("", "nonempty"),
        ("not-json\n", "valid JSONL"),
    ],
)
def test_inspect_integrity_rejects_invalid_manager_session_jsonl(
    tmp_path: Path,
    bad_content: str,
    message: str,
) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    store.write_checkpoint(
        1,
        asdict(reduce_events(store.manifest, store.events())),
        snapshot,
    )
    manager_event = next(
        event for event in store.events() if event["kind"] == "manager_completed"
    )
    Path(manager_event["payload"]["episode"]["session_path"]).write_text(
        bad_content,
        encoding="utf-8",
    )
    with pytest.raises(StoreIntegrityError, match=message):
        store.inspect_integrity()


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("manager.jsonl", "run/sessions"),
        ("sessions/round_0002_manager.jsonl", "role and round"),
    ],
)
def test_inspect_integrity_enforces_session_location_and_name(
    tmp_path: Path,
    relative_path: str,
    message: str,
) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    session = store.root / relative_path
    session.parent.mkdir(exist_ok=True)
    session.write_text('{"session":"manager"}\n', encoding="utf-8")
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="blocked", reason="stop").to_dict(),
            "episode": RoleEpisode(
                output="blocked",
                terminal="end_turn",
                iterations=1,
                tool_names=(),
                usage=(),
                session_path=str(session),
                cleanup_ok=True,
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": 1})
    store.write_checkpoint(
        1,
        asdict(reduce_events(store.manifest, store.events())),
        snapshot,
    )
    with pytest.raises(StoreIntegrityError, match=message):
        store.inspect_integrity()


def test_inspect_integrity_requires_successful_auditor_tool_evidence(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    store.write_checkpoint(
        1,
        asdict(reduce_events(store.manifest, store.events())),
        snapshot,
    )
    auditor_event = next(
        event for event in store.events() if event["kind"] == "auditor_completed"
    )
    Path(auditor_event["payload"]["episode"]["session_path"]).write_text(
        '{"type":"message","message":{"role":"assistant","content":"claim"}}\n',
        encoding="utf-8",
    )
    with pytest.raises(StoreIntegrityError, match="successful allowed tool result"):
        store.inspect_integrity()


def test_create_audit_workspace_from_executor_snapshot(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    audit_workspace = store.create_audit_workspace(1, snapshot)
    assert audit_workspace == store.root / "audit-workspaces" / "round_0001"
    assert (audit_workspace / "answer.py").read_text(encoding="utf-8") == "42\n"
    with pytest.raises(StoreIntegrityError, match="already exists"):
        store.create_audit_workspace(1, snapshot)


def test_final_checkpoint_is_independent_from_round_checkpoints(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    round_state = reduce_events(store.manifest, store.events())
    store.write_checkpoint(1, asdict(round_state), snapshot)
    round_bytes = (store.root / "checkpoints" / "0001.json").read_bytes()
    store.append("round_started", {"round": 2})
    done = ManagerDecision(next="done", reason="complete")
    store.append(
        "manager_completed",
        {
            "round": 2,
            "decision": done.to_dict(),
            "episode": _episode(store, "manager", 2),
        },
    )
    store.append("round_completed", {"round": 2})
    final_round_state = reduce_events(store.manifest, store.events())
    store.write_checkpoint(2, asdict(final_round_state), snapshot)
    store.append("run_completed", {"decision": done.to_dict()})
    final_state = reduce_events(store.manifest, store.events())
    with pytest.raises(StoreIntegrityError, match="final checkpoint"):
        store.inspect_integrity()
    final = store.write_final_checkpoint(asdict(final_state), snapshot)
    inspected = store.inspect_integrity()
    assert (store.root / "checkpoints" / "0001.json").read_bytes() == round_bytes
    assert final["checkpoint_sha256"] == (store.root / "checkpoints" / "final.sha256").read_text().strip()
    assert inspected["state"]["status"] == "complete"
    assert inspected["final_checkpoint"]["checkpoint_sha256"] == final["checkpoint_sha256"]


def test_inspect_integrity_rejects_round_checkpoint_hash_drift(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    workspace.joinpath("answer.py").write_text("42\n", encoding="utf-8")
    snapshot = store.capture_workspace()
    _complete_round(store, 1)
    store.write_checkpoint(1, asdict(reduce_events(store.manifest, store.events())), snapshot)
    (store.root / "checkpoints" / "0001.sha256").write_text(
        "0" * 64 + "\n",
        encoding="ascii",
    )
    with pytest.raises(StoreIntegrityError, match="checkpoint file hash drifted"):
        store.inspect_integrity()


def test_inspect_integrity_rejects_cleanup_failure_evidence(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    session = store.root / "sessions" / "round_0001_manager.jsonl"
    session.parent.mkdir()
    session.write_text('{"session":"manager"}\n', encoding="utf-8")
    episode = RoleEpisode(
        output="blocked",
        terminal="end_turn",
        iterations=1,
        tool_names=(),
        usage=(),
        session_path=str(session),
        cleanup_ok=False,
    ).to_dict()
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="blocked", reason="stop").to_dict(),
            "episode": episode,
        },
    )
    store.append("round_completed", {"round": 1})
    store.write_checkpoint(
        1,
        asdict(reduce_events(store.manifest, store.events())),
        store.capture_workspace(),
    )
    with pytest.raises(StoreIntegrityError, match="cleanup"):
        store.inspect_integrity()
    assert workspace.is_dir()
