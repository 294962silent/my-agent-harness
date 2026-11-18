from __future__ import annotations

import json
from pathlib import Path

import pytest

from my_coding_agent.longhorizon.schema import AuditReport, ManagerDecision, RoleEpisode
from my_coding_agent.longhorizon.state import ResumeBlockedError, assert_resume_allowed, reduce_events
from my_coding_agent.longhorizon.store import LongHorizonStore, StoreIntegrityError


def _episode(
    store: LongHorizonStore,
    role: str,
    round_index: int,
    *,
    cleanup_ok: bool = True,
) -> dict:
    sessions = store.root / "sessions"
    sessions.mkdir(exist_ok=True)
    session = sessions / f"round_{round_index:04d}_{role}_abcdef123456.jsonl"
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
        usage=(),
        session_path=str(session),
        cleanup_ok=cleanup_ok,
    ).to_dict()


def _store(tmp_path: Path) -> LongHorizonStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return LongHorizonStore.prepare(
        tmp_path / "run",
        run_id="run",
        task="task",
        workspace=workspace,
        model={"provider": "fake", "model": "fake", "max_retries": 0},
        limits={"max_rounds": 3},
    )


def _append_complete_execute_round(
    store: LongHorizonStore,
    round_index: int,
) -> None:
    store.append("round_started", {"round": round_index})
    decision = ManagerDecision(
        next="execute",
        reason="edit",
        subtask="write answer",
        acceptance_criteria=("verified",),
    )
    store.append(
        "manager_completed",
        {
            "round": round_index,
            "decision": decision.to_dict(),
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
                summary="ok",
                evidence=("answer.py",),
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": round_index})


def test_reducer_builds_ready_active_and_complete_states(tmp_path: Path) -> None:
    store = _store(tmp_path)
    initial = reduce_events(store.manifest, store.events())
    assert initial.status == "ready"
    assert initial.next_round == 1
    assert initial.completed_rounds == ()

    store.append("round_started", {"round": 1})
    active = reduce_events(store.manifest, store.events())
    assert active.status == "blocked"
    assert active.active_round == 1
    assert active.next_round == 1

    first_decision = ManagerDecision(
        next="execute",
        reason="edit",
        subtask="write answer",
        acceptance_criteria=("verified",),
    )
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": first_decision.to_dict(),
            "episode": _episode(store, "manager", 1),
        },
    )
    store.append(
        "executor_completed",
        {"round": 1, "episode": _episode(store, "executor", 1)},
    )
    store.append(
        "auditor_completed",
        {
            "round": 1,
            "episode": _episode(store, "auditor", 1),
            "report": AuditReport(
                status="complete",
                summary="ok",
                evidence=("answer.py",),
                verified_facts=("fact-a", "fact-a", "fact-b"),
            ).to_dict(),
        },
    )
    store.append("round_completed", {"round": 1})
    first = reduce_events(store.manifest, store.events())
    assert first.last_completed_decision == first_decision.to_dict()

    store.append("round_started", {"round": 2})
    done = ManagerDecision(next="done", reason="all verified")
    store.append(
        "manager_completed",
        {
            "round": 2,
            "decision": done.to_dict(),
            "episode": _episode(store, "manager", 2),
        },
    )
    store.append("round_completed", {"round": 2})
    store.append(
        "run_completed",
        {"decision": done.to_dict()},
    )
    final = reduce_events(store.manifest, store.events())
    assert final.status == "complete"
    assert final.completed_rounds == (1, 2)
    assert final.next_round == 3
    assert final.verified_facts == ("fact-a", "fact-b")
    assert final.terminal_reason == "all verified"
    assert final.last_completed_decision == done.to_dict()
    with pytest.raises(ResumeBlockedError):
        assert_resume_allowed(final)


def test_reducer_supports_waiting_input_and_rejects_bad_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="ask", reason="need choice", question="Which?").to_dict(),
            "episode": _episode(store, "manager", 1),
        },
    )
    store.append("round_completed", {"round": 1})
    waiting = reduce_events(store.manifest, store.events())
    assert waiting.status == "waiting_input"
    assert waiting.terminal_reason == "need choice"

    bad = _store(tmp_path / "bad")
    with pytest.raises(StoreIntegrityError):
        bad.append("round_started", {"round": 2})

    completed = _store(tmp_path / "completed")
    completed.append("round_started", {"round": 1})
    with pytest.raises(StoreIntegrityError):
        completed.append("round_completed", {"round": 1})


def test_reducer_rejects_duplicate_and_conflicting_round_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    with pytest.raises(StoreIntegrityError):
        store.append("round_started", {"round": 1})


def test_done_requires_prior_clean_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="done", reason="too early").to_dict(),
            "episode": _episode(store, "manager", 1),
        },
    )
    with pytest.raises(StoreIntegrityError):
        store.append("round_completed", {"round": 1})


def test_cleanup_failure_blocks_state_and_completion(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
            "episode": _episode(store, "manager", 1),
        },
    )
    failed = _episode(store, "executor", 1, cleanup_ok=False)
    store.append("executor_completed", {"round": 1, "episode": failed})
    store.append(
        "auditor_completed",
        {
            "round": 1,
            "episode": _episode(store, "auditor", 1),
            "report": AuditReport(status="complete", summary="ok", evidence=("test",)).to_dict(),
        },
    )
    store.append("round_completed", {"round": 1})
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert state.terminal_reason == "executor cleanup failed"
    with pytest.raises(StoreIntegrityError):
        store.append(
            "run_completed",
            {"decision": ManagerDecision(next="done", reason="done").to_dict()},
        )


def test_round_unknown_requires_started_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(StoreIntegrityError):
        store.append("round_unknown", {"round": 1, "reason": "unknown"})


def test_manager_completed_requires_episode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    with pytest.raises(StoreIntegrityError, match="manager_completed requires episode"):
        store.append(
            "manager_completed",
            {
                "round": 1,
                "decision": ManagerDecision(
                    next="blocked",
                    reason="stop",
                ).to_dict(),
            },
        )


def test_run_completed_requires_immediately_matching_done_round(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _append_complete_execute_round(store, 1)

    store.append("round_started", {"round": 2})
    ask = ManagerDecision(next="ask", reason="input", question="Which?")
    store.append(
        "manager_completed",
        {
            "round": 2,
            "decision": ask.to_dict(),
            "episode": _episode(store, "manager", 2),
        },
    )
    store.append("round_completed", {"round": 2})
    forged = ManagerDecision(next="done", reason="forged completion")
    with pytest.raises(
        StoreIntegrityError,
        match="run_completed must match the immediately preceding done round",
    ):
        store.append("run_completed", {"decision": forged.to_dict()})


def test_reducer_rejects_complete_audit_with_nonempty_remaining(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
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
            "episode": _episode(store, "manager", 1),
        },
    )
    store.append(
        "executor_completed",
        {"round": 1, "episode": _episode(store, "executor", 1)},
    )
    with pytest.raises(
        StoreIntegrityError,
        match="audit.remaining must be empty when audit.status is complete",
    ):
        store.append(
            "auditor_completed",
            {
                "round": 1,
                "episode": _episode(store, "auditor", 1),
                "report": {
                    "status": "complete",
                    "summary": "verified",
                    "evidence": ["answer.py"],
                    "verified_facts": [],
                    "remaining": ["informational note"],
                },
            },
        )
    assert store.events()[-1]["kind"] == "executor_completed"


def test_run_blocked_transitions_to_blocked_and_closes_event_stream(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("round_started", {"round": 1})
    store.append(
        "manager_completed",
        {
            "round": 1,
            "decision": ManagerDecision(next="blocked", reason="manager blocked").to_dict(),
            "episode": _episode(store, "manager", 1),
        },
    )
    store.append("round_completed", {"round": 1})
    store.append("run_blocked", {"reason": "checkpoint write failed"})
    state = reduce_events(store.manifest, store.events())
    assert state.status == "blocked"
    assert state.terminal_reason == "checkpoint write failed"
    with pytest.raises(StoreIntegrityError):
        store.append("follow_up_added", {"follow_up_id": "late", "text": "late"})
