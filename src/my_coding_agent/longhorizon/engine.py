"""Controller loop for the persistent long-horizon MEA state machine."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import asdict
from typing import Any

from .schema import (
    RoleBackend,
    RoleEpisode,
    parse_audit_report,
    parse_manager_decision,
)
from .state import RunState, pending_follow_ups, reduce_events
from .store import LongHorizonStore, StoreIntegrityError


class ControllerError(RuntimeError):
    """The controller stopped because the round or run cannot continue safely."""


class LongHorizonController:
    def __init__(
        self,
        store: LongHorizonStore,
        backend: RoleBackend,
        max_rounds: int | None = None,
    ) -> None:
        if not isinstance(store, LongHorizonStore):
            raise TypeError("store must be LongHorizonStore")
        if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 1):
            raise ValueError("max_rounds must be a positive integer or None")
        manifest_max = store.manifest.get("limits", {}).get("max_rounds", 1000)
        self.store = store
        self.backend = backend
        self.max_rounds = min(manifest_max, max_rounds or manifest_max)

    def status(self) -> RunState:
        data = self.store.inspect_integrity()["state"]
        return RunState(
            status=data["status"],
            next_round=data["next_round"],
            completed_rounds=tuple(data["completed_rounds"]),
            verified_facts=tuple(data["verified_facts"]),
            last_audit=data["last_audit"],
            last_manager=data["last_manager"],
            last_completed_decision=data["last_completed_decision"],
            active_round=data["active_round"],
            terminal_reason=data["terminal_reason"],
        )

    def follow_up(self, message: str | None = None, *, text: str | None = None) -> None:
        if message is not None and text is not None:
            raise ControllerError("provide follow-up message or text, not both")
        text = message if message is not None else text
        if not isinstance(text, str) or not text.strip():
            raise ControllerError("follow-up text must be nonempty")
        with self.store.controller_lock():
            state = self._state()
            if state.status != "waiting_input":
                raise ControllerError("follow-up is accepted only while waiting_input")
            follow_up_id = uuid.uuid4().hex
            self.store.append(
                "follow_up_added",
                {"follow_up_id": follow_up_id, "text": text},
            )
            self._sync_inbox()

    async def aclose(self) -> None:
        """Close backend-owned clients after all controller work is quiescent."""

        with self.store.controller_lock():
            close = getattr(self.backend, "aclose", None) or getattr(self.backend, "close", None)
            if close is None:
                return
            result = close()
            if inspect.isawaitable(result):
                await result

    def restore(self, round_index: int) -> None:
        with self.store.controller_lock():
            self.store.restore_checkpoint(round_index)

    async def run(self) -> RunState:
        with self.store.controller_lock():
            state = self._state()
            if state.status != "ready":
                raise ControllerError(f"run cannot start from status {state.status}")
            return await self._drive(state)

    async def run_until_terminal(self) -> RunState:
        return await self.run()

    async def resume(self) -> RunState:
        with self.store.controller_lock():
            try:
                state = self.store.resume_state()
            except StoreIntegrityError as exc:
                raise ControllerError(f"resume blocked: {exc}") from exc
            pending = pending_follow_ups(self.store.manifest, self.store.events())
            if state.status == "waiting_input" and not pending:
                return state
            self._sync_inbox()
            return await self._drive(state)

    def _state(self) -> RunState:
        return reduce_events(self.store.manifest, self.store.events())

    def _sync_inbox(self) -> None:
        followups = pending_follow_ups(self.store.manifest, self.store.events())
        self.store.write_inbox([dict(item) for item in followups])

    def _validate_episode(
        self,
        episode: RoleEpisode,
        *,
        role: str,
        round_index: int,
    ) -> RoleEpisode:
        if not isinstance(episode, RoleEpisode):
            raise ControllerError(f"{role} backend did not return RoleEpisode")
        if not episode.output.strip():
            raise ControllerError(f"{role} episode output is empty")
        if episode.terminal != "end_turn":
            raise ControllerError(f"{role} terminal is not end_turn")
        if not episode.cleanup_ok:
            raise ControllerError(f"{role} cleanup failed")
        try:
            self.store.validate_session_path(
                episode.session_path,
                expected_role=role,
                expected_round=round_index,
            )
        except StoreIntegrityError as exc:
            raise ControllerError(f"{role} session is invalid") from exc
        return episode

    def _all_session_evidence_valid(self) -> bool:
        try:
            self.store.inspect_integrity()
        except StoreIntegrityError:
            return False
        return True

    def _clean_audit(self, audit: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(audit, dict)
            and audit.get("status") == "complete"
            and not audit.get("remaining")
        )

    def _checkpoint(self, round_index: int, state: RunState) -> dict[str, Any]:
        snapshot = self.store.capture_workspace()
        return self.store.write_checkpoint(round_index, asdict(state), snapshot)

    def _set_terminal_status(self, state: RunState) -> None:
        self.store.set_status(
            {
                "status": state.status,
                "next_round": state.next_round,
                "completed_rounds": list(state.completed_rounds),
                "terminal_reason": state.terminal_reason,
            }
        )

    def _fail_round(self, round_index: int, reason: str) -> None:
        try:
            events = self.store.events()
            state = reduce_events(self.store.manifest, events)
            if (
                state.active_round is not None
                and (not events or events[-1]["kind"] != "round_unknown")
            ):
                self.store.append(
                    "round_unknown",
                    {"round": round_index, "reason": reason},
                )
            events = self.store.events()
            if not events or events[-1]["kind"] != "run_blocked":
                self.store.append("run_blocked", {"reason": reason})
        except Exception:
            # Preserve the original controller failure while still making a best-effort
            # status update from whatever terminal evidence was durably appended.
            pass
        try:
            state = self._state()
            self._set_terminal_status(state)
        except Exception:
            pass

    def _consume_followups(self) -> None:
        followups = pending_follow_ups(self.store.manifest, self.store.events())
        for item in followups:
            self.store.append("follow_up_consumed", {"follow_up_id": item["follow_up_id"]})
        self._sync_inbox()

    async def _drive(self, state: RunState) -> RunState:
        while state.status == "ready" or (
            state.status == "waiting_input"
            and pending_follow_ups(self.store.manifest, self.store.events())
        ):
            round_index = state.next_round
            if round_index > self.max_rounds:
                reason = "max_rounds reached before a terminal decision"
                self.store.append("run_blocked", {"reason": reason})
                blocked = reduce_events(self.store.manifest, self.store.events())
                self._set_terminal_status(blocked)
                raise ControllerError(reason)
            self.store.append("round_started", {"round": round_index})
            try:
                self.store.set_status({"status": "running", "round": round_index})
                pending = pending_follow_ups(self.store.manifest, self.store.events())
                manager_state = {**asdict(state), "pending_follow_ups": list(pending)}
                manager_episode = self._validate_episode(
                    await self.backend.manager(
                        task=self._task(),
                        state=manager_state,
                        round_index=round_index,
                    ),
                    role="manager",
                    round_index=round_index,
                )
                try:
                    decision = parse_manager_decision(manager_episode.output)
                except ValueError as exc:
                    raise ControllerError(f"manager output invalid: {exc}") from exc
                self.store.append(
                    "manager_completed",
                    {
                        "round": round_index,
                        "decision": decision.to_dict(),
                        "episode": manager_episode.to_dict(),
                    },
                )
                self._consume_followups()

                if decision.next == "execute":
                    executor_episode = self._validate_episode(
                        await self.backend.executor(
                            task=self._task(),
                            decision=decision,
                            state=asdict(state),
                            round_index=round_index,
                        ),
                        role="executor",
                        round_index=round_index,
                    )
                    self.store.append(
                        "executor_completed",
                        {"round": round_index, "episode": executor_episode.to_dict()},
                    )
                    before = self.store.capture_workspace()
                    audit_workspace = self.store.create_audit_workspace(
                        round_index,
                        before,
                    )
                    auditor_episode = self._validate_episode(
                        await self.backend.auditor(
                            task=self._task(),
                            decision=decision,
                            executor_output=executor_episode.output,
                            state=asdict(state),
                            round_index=round_index,
                            workspace=audit_workspace,
                        ),
                        role="auditor",
                        round_index=round_index,
                    )
                    after = self.store.capture_workspace()
                    if before.sha256 != after.sha256:
                        raise ControllerError("auditor mutated the workspace")
                    try:
                        audit = parse_audit_report(auditor_episode.output)
                    except ValueError as exc:
                        raise ControllerError(f"auditor output invalid: {exc}") from exc
                    if audit.status == "complete":
                        try:
                            self.store.validate_auditor_tool_evidence(
                                auditor_episode.session_path
                            )
                        except StoreIntegrityError as exc:
                            raise ControllerError(
                                f"auditor evidence is invalid: {exc}"
                            ) from exc
                    self.store.append(
                        "auditor_completed",
                        {
                            "round": round_index,
                            "episode": auditor_episode.to_dict(),
                            "report": audit.to_dict(),
                        },
                    )
                    self.store.append("round_completed", {"round": round_index})
                    closed = reduce_events(self.store.manifest, self.store.events())
                    self._checkpoint(round_index, closed)
                    state = closed
                    if audit.status == "blocked":
                        self._set_terminal_status(state)
                        return state
                    continue

                if decision.next == "done":
                    if not self._clean_audit(state.last_audit):
                        raise ControllerError("done requires a prior clean complete audit")
                    if not self._all_session_evidence_valid():
                        raise ControllerError("done requires valid cleanup and session evidence")
                    self.store.append("round_completed", {"round": round_index})
                    closed = reduce_events(self.store.manifest, self.store.events())
                    self._checkpoint(round_index, closed)
                    self.store.append(
                        "run_completed",
                        {"decision": decision.to_dict()},
                    )
                    state = reduce_events(self.store.manifest, self.store.events())
                    self.store.write_final_checkpoint(
                        asdict(state),
                        self.store.capture_workspace(),
                    )
                    self._set_terminal_status(state)
                    return state

                if decision.next in {"ask", "blocked"}:
                    self.store.append("round_completed", {"round": round_index})
                    state = reduce_events(self.store.manifest, self.store.events())
                    self._checkpoint(round_index, state)
                    self._set_terminal_status(state)
                    return state
                raise ControllerError(f"unsupported manager route: {decision.next}")
            except BaseException as exc:
                reason = str(exc) if str(exc).strip() else type(exc).__name__
                self._fail_round(round_index, reason)
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, ControllerError):
                    raise
                raise ControllerError(reason) from exc

        return state

    def _task(self) -> str:
        try:
            return self.store.task_text()
        except StoreIntegrityError as exc:
            raise ControllerError("task.txt is unavailable") from exc
