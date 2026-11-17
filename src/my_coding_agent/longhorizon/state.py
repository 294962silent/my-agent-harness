"""Pure reducer and resume gates for the long-horizon journal."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .schema import (
    AuditReport,
    ManagerDecision,
    RoleEpisode,
    SchemaError,
    canonical_json,
    digest_value,
    parse_audit_report,
    parse_manager_decision,
    parse_role_episode,
)


EVENT_KINDS = frozenset(
    {
        "prepared",
        "round_started",
        "manager_completed",
        "executor_completed",
        "auditor_completed",
        "round_completed",
        "run_completed",
        "run_blocked",
        "round_unknown",
        "follow_up_added",
        "follow_up_consumed",
    }
)
EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous",
        "manifest_sha256",
        "kind",
        "payload",
        "time_unix",
        "sha256",
    }
)


class StateIntegrityError(ValueError):
    """Journal history cannot form a valid run state."""


class ResumeBlockedError(StateIntegrityError):
    """A run is active, dangling or already complete."""


@dataclass(frozen=True)
class RunState:
    """Journal consistency projection, not proof that finalization committed."""

    status: Literal["ready", "waiting_input", "blocked", "complete"]
    next_round: int
    completed_rounds: tuple[int, ...]
    verified_facts: tuple[str, ...]
    last_audit: dict[str, Any] | None
    last_manager: dict[str, Any] | None
    last_completed_decision: dict[str, Any] | None
    active_round: int | None
    terminal_reason: str


def _event_hash(row: Mapping[str, Any]) -> str:
    return digest_value({key: value for key, value in row.items() if key != "sha256"})


def validate_events(
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected_manifest = digest_value(dict(manifest))
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or set(event) != EVENT_FIELDS:
            raise StateIntegrityError(f"event {index} has invalid fields")
        row = dict(event)
        checksum = row.get("sha256")
        if (
            row.get("schema_version") != 1
            or row.get("sequence") != index
            or row.get("previous") != previous
            or row.get("manifest_sha256") != expected_manifest
            or row.get("kind") not in EVENT_KINDS
            or not isinstance(row.get("payload"), Mapping)
            or not isinstance(checksum, str)
            or checksum != _event_hash(row)
        ):
            raise StateIntegrityError(f"event {index} failed hash-chain validation")
        rows.append(row)
        previous = checksum
    if not rows or rows[0]["kind"] != "prepared":
        raise StateIntegrityError("journal must start with prepared")
    if sum(row["kind"] == "prepared" for row in rows) != 1:
        raise StateIntegrityError("journal must contain exactly one prepared event")
    return rows


def _round(value: Mapping[str, Any], expected_round: int | None = None) -> int:
    round_index = value.get("round")
    if type(round_index) is not int or round_index < 1:
        raise StateIntegrityError("round must be a positive integer")
    if expected_round is not None and round_index != expected_round:
        raise StateIntegrityError("round event does not match active round")
    return round_index


def _exact_payload_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise StateIntegrityError(
            f"event payload fields differ: expected={sorted(expected)} actual={sorted(payload)}"
        )


def _parse_decision(value: object) -> ManagerDecision:
    if not isinstance(value, Mapping):
        raise StateIntegrityError("manager decision payload must be an object")
    try:
        return parse_manager_decision(json.dumps(value, ensure_ascii=False))
    except SchemaError as exc:
        raise StateIntegrityError(str(exc)) from exc


def _parse_report(value: object) -> AuditReport:
    if not isinstance(value, Mapping):
        raise StateIntegrityError("audit report payload must be an object")
    try:
        return parse_audit_report(json.dumps(value, ensure_ascii=False))
    except SchemaError as exc:
        raise StateIntegrityError(str(exc)) from exc


def _parse_episode(value: object) -> RoleEpisode:
    if not isinstance(value, Mapping):
        raise StateIntegrityError("episode payload must be an object")
    try:
        return parse_role_episode(json.dumps(value, ensure_ascii=False))
    except SchemaError as exc:
        raise StateIntegrityError(str(exc)) from exc


def _append_unique(values: list[str], additions: Sequence[str]) -> None:
    seen = set(values)
    for item in additions:
        if item not in seen:
            values.append(item)
            seen.add(item)


def reduce_events(
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> RunState:
    """Reduce a journal without claiming checkpoint-backed run finality.

    Public status and resume paths must use ``LongHorizonStore.inspect_integrity``
    (or ``resume_state``) before treating a complete state as authoritative.
    """

    rows = validate_events(manifest, events)
    limit_mapping = manifest.get("limits")
    max_rounds = (
        limit_mapping.get("max_rounds")
        if isinstance(limit_mapping, Mapping)
        else 1000
    )
    if type(max_rounds) is not int or max_rounds < 1:
        raise StateIntegrityError("manifest.max_rounds is invalid")

    completed: list[int] = []
    verified_facts: list[str] = []
    last_audit: dict[str, Any] | None = None
    last_manager: dict[str, Any] | None = None
    last_completed_decision: dict[str, Any] | None = None
    active: int | None = None
    pending: dict[str, Any] = {}
    terminal_reason = ""
    run_complete = False
    run_blocked = False
    unknown = False
    cleanup_failed = False
    auditor_blocked_reason = ""
    pending_followups: dict[str, str] = {}

    for event in rows[1:]:
        if (run_complete or run_blocked) and event["kind"] != "run_blocked":
            raise StateIntegrityError("events follow a terminal run event")
        if run_blocked and event["kind"] == "run_blocked":
            raise StateIntegrityError("run_blocked repeated")
        kind = event["kind"]
        payload = event["payload"]
        if kind == "round_started":
            _exact_payload_keys(payload, {"round"})
            round_index = _round(payload)
            if active is not None:
                raise StateIntegrityError("round_started repeated before round_completed")
            expected = len(completed) + 1
            if round_index != expected or round_index > max_rounds:
                raise StateIntegrityError("round history is not a contiguous prefix")
            active = round_index
            pending = {}
        elif kind == "manager_completed":
            if "episode" not in payload:
                raise StateIntegrityError("manager_completed requires episode")
            _exact_payload_keys(payload, {"round", "decision", "episode"})
            if active is None:
                raise StateIntegrityError("manager_completed requires round_started")
            round_index = _round(payload, active)
            if "decision" in pending:
                raise StateIntegrityError("manager_completed repeated")
            if "executor" in pending:
                raise StateIntegrityError("manager_completed followed executor_completed")
            decision = _parse_decision(payload["decision"])
            pending["decision"] = decision
            last_manager = decision.to_dict()
            manager_episode = _parse_episode(payload["episode"])
            pending["manager"] = manager_episode
            if not manager_episode.cleanup_ok:
                cleanup_failed = True
                terminal_reason = "manager cleanup failed"
        elif kind == "executor_completed":
            _exact_payload_keys(payload, {"round", "episode"})
            _round(payload, active)
            decision = pending.get("decision")
            if not isinstance(decision, ManagerDecision) or decision.next != "execute":
                raise StateIntegrityError("executor_completed requires an execute decision")
            if "executor" in pending:
                raise StateIntegrityError("executor_completed repeated")
            if "auditor" in pending:
                raise StateIntegrityError("executor_completed followed auditor_completed")
            episode = _parse_episode(payload["episode"])
            pending["executor"] = episode
            if not episode.cleanup_ok:
                cleanup_failed = True
                terminal_reason = "executor cleanup failed"
        elif kind == "auditor_completed":
            _exact_payload_keys(payload, {"round", "episode", "report"})
            _round(payload, active)
            if "executor" not in pending:
                raise StateIntegrityError("auditor_completed requires executor_completed")
            if "auditor" in pending:
                raise StateIntegrityError("auditor_completed repeated")
            episode = _parse_episode(payload["episode"])
            pending["auditor"] = episode
            if not episode.cleanup_ok:
                cleanup_failed = True
                terminal_reason = "auditor cleanup failed"
            report = _parse_report(payload["report"])
            pending["audit"] = report
            last_audit = report.to_dict()
            if report.status == "blocked":
                auditor_blocked_reason = report.summary
        elif kind == "round_completed":
            _exact_payload_keys(payload, {"round"})
            round_index = _round(payload, active)
            decision = pending.get("decision")
            if not isinstance(decision, ManagerDecision):
                raise StateIntegrityError("round_completed requires manager_completed")
            if decision.next == "execute":
                if "executor" not in pending or "auditor" not in pending:
                    raise StateIntegrityError("execute round is missing executor/auditor")
            elif "executor" in pending or "auditor" in pending:
                raise StateIntegrityError("non-execute round must not run executor/auditor")
            if decision.next == "done" and (
                last_audit is None or last_audit.get("status") != "complete"
            ):
                raise StateIntegrityError("done requires a prior clean audit")
            audit = pending.get("audit")
            if isinstance(audit, AuditReport):
                _append_unique(verified_facts, audit.verified_facts)
            completed.append(round_index)
            active = None
            last_completed_decision = decision.to_dict()
            if decision.next != "execute":
                terminal_reason = decision.reason
            elif not cleanup_failed:
                terminal_reason = ""
            pending = {}
        elif kind == "round_unknown":
            _exact_payload_keys(payload, {"round", "reason"})
            if active is None:
                raise StateIntegrityError("round_unknown requires round_started")
            round_index = _round(payload, active)
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise StateIntegrityError("round_unknown.reason must be nonempty")
            terminal_reason = reason.strip()
            unknown = True
            active = round_index
        elif kind == "follow_up_added":
            _exact_payload_keys(payload, {"follow_up_id", "text"})
            if active is not None or last_manager is None or last_manager.get("next") != "ask":
                raise StateIntegrityError("follow_up_added requires waiting_input")
            follow_up_id = payload.get("follow_up_id")
            text = payload.get("text")
            if (
                not isinstance(follow_up_id, str)
                or not follow_up_id
                or follow_up_id in pending_followups
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise StateIntegrityError("follow_up_added payload is invalid")
            pending_followups[follow_up_id] = text
        elif kind == "follow_up_consumed":
            _exact_payload_keys(payload, {"follow_up_id"})
            follow_up_id = payload.get("follow_up_id")
            if not isinstance(follow_up_id, str) or follow_up_id not in pending_followups:
                raise StateIntegrityError("follow_up_consumed references an unknown follow-up")
            pending_followups.pop(follow_up_id)
        elif kind == "run_completed":
            _exact_payload_keys(payload, {"decision"})
            if active is not None:
                raise StateIntegrityError("run_completed cannot follow a dangling round")
            decision = _parse_decision(payload["decision"])
            if (
                rows[event["sequence"] - 1]["kind"] != "round_completed"
                or last_completed_decision is None
                or canonical_json(decision.to_dict())
                != canonical_json(last_completed_decision)
                or decision.next != "done"
            ):
                raise StateIntegrityError(
                    "run_completed must match the immediately preceding done round"
                )
            if decision.next != "done":
                raise StateIntegrityError("run_completed requires a done decision")
            if last_audit is None or last_audit.get("status") != "complete":
                raise StateIntegrityError("run_completed requires a prior clean audit")
            if cleanup_failed:
                raise StateIntegrityError("run_completed requires successful role cleanup")
            if pending_followups:
                raise StateIntegrityError("run_completed cannot leave unconsumed follow-ups")
            last_manager = decision.to_dict()
            terminal_reason = decision.reason
            run_complete = True
        elif kind == "run_blocked":
            _exact_payload_keys(payload, {"reason"})
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise StateIntegrityError("run_blocked.reason must be nonempty")
            terminal_reason = reason.strip()
            run_blocked = True
        else:
            raise StateIntegrityError(f"unsupported journal event: {kind}")

    if run_blocked:
        status = "blocked"
    elif run_complete:
        status: Literal["ready", "waiting_input", "blocked", "complete"] = "complete"
    elif active is not None or unknown or cleanup_failed or auditor_blocked_reason:
        status = "blocked"
    elif last_manager is not None and last_manager.get("next") == "blocked":
        status = "blocked"
    elif last_manager is not None and last_manager.get("next") == "ask":
        status = "waiting_input"
    else:
        status = "ready"
    if auditor_blocked_reason and not terminal_reason:
        terminal_reason = auditor_blocked_reason
    return RunState(
        status=status,
        next_round=len(completed) + 1,
        completed_rounds=tuple(completed),
        verified_facts=tuple(verified_facts),
        last_audit=dict(last_audit) if last_audit is not None else None,
        last_manager=dict(last_manager) if last_manager is not None else None,
        last_completed_decision=(
            dict(last_completed_decision)
            if last_completed_decision is not None
            else None
        ),
        active_round=active,
        terminal_reason=terminal_reason,
    )


def assert_resume_allowed(state: RunState) -> None:
    if state.status == "complete":
        raise ResumeBlockedError("completed run cannot be resumed")
    if state.active_round is not None:
        raise ResumeBlockedError(
            f"round {state.active_round} has started but not completed; replay is forbidden"
        )


def pending_follow_ups(
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """Return the durable, validated follow-up inbox in append order."""

    rows = validate_events(manifest, events)
    reduce_events(manifest, rows)
    pending: dict[str, str] = {}
    order: list[str] = []
    for event in rows:
        payload = event["payload"]
        if event["kind"] == "follow_up_added":
            follow_up_id = payload["follow_up_id"]
            if follow_up_id not in pending:
                order.append(follow_up_id)
            pending[follow_up_id] = payload["text"]
        elif event["kind"] == "follow_up_consumed":
            pending.pop(payload["follow_up_id"], None)
    return tuple(
        {"follow_up_id": follow_up_id, "text": pending[follow_up_id]}
        for follow_up_id in order
        if follow_up_id in pending
    )
