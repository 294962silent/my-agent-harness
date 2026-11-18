"""Tests for the cache-stable MEA orchestration core."""

from __future__ import annotations

import asyncio
import json

import pytest

from experiments.long_horizon_quality.orchestrator import (
    AUDITOR_SYSTEM_PROMPT,
    EXECUTOR_SYSTEM_PROMPT,
    MANAGER_SYSTEM_PROMPT,
    OrchestrationError,
    RoleCallbacks,
    RoleRequest,
    run_mea,
)


def _manager(next_action: str, *, reason: str = "reason") -> str:
    return json.dumps(
        {
            "next": next_action,
            "reason": reason,
            "subtask": "fix the failing test" if next_action == "execute" else "",
            "acceptance_criteria": ["official test passes"]
            if next_action == "execute"
            else [],
            "question": "",
            "related_rounds": [1],
        }
    )


def _audit(status: str = "complete") -> str:
    return json.dumps(
        {
            "status": status,
            "summary": "verified",
            "evidence": ["test passed"],
            "verified_facts": ["official test passes"],
            "remaining": [],
        }
    )


class FakeCallbacks(RoleCallbacks):
    def __init__(self, managers: list[str], audits: list[str]) -> None:
        self.managers = managers
        self.audits = audits
        self.manager_requests: list[RoleRequest] = []
        self.executor_requests: list[RoleRequest] = []
        self.auditor_requests: list[RoleRequest] = []

    async def manager(self, request: RoleRequest) -> str:
        self.manager_requests.append(request)
        return self.managers.pop(0)

    async def executor(self, request: RoleRequest) -> str:
        self.executor_requests.append(request)
        return "Implemented the fix."

    async def auditor(self, request: RoleRequest) -> str:
        self.auditor_requests.append(request)
        return self.audits.pop(0)


def test_mea_uses_append_only_manager_history_and_fresh_roles() -> None:
    callbacks = FakeCallbacks(
        managers=[_manager("execute"), _manager("done")],
        audits=[_audit("complete")],
    )
    outcome = asyncio.run(run_mea(task="Fix the project", callbacks=callbacks))

    assert outcome.status == "complete"
    assert outcome.rounds == 2
    assert outcome.verified_facts == ("official test passes",)
    assert len(callbacks.manager_requests) == 2
    first = callbacks.manager_requests[0].messages
    second = callbacks.manager_requests[1].messages
    assert first[0].content == MANAGER_SYSTEM_PROMPT
    assert tuple(message.role for message in first) == ("system", "user")
    assert tuple(message.role for message in second) == (
        "system",
        "user",
        "assistant",
        "user",
    )
    assert second[:3] == first + (second[2],)
    assert callbacks.executor_requests[0].messages[0].content == EXECUTOR_SYSTEM_PROMPT
    assert callbacks.auditor_requests[0].messages[0].content == AUDITOR_SYSTEM_PROMPT
    assert outcome.role_prompt_hashes["manager"] != outcome.role_prompt_hashes["auditor"]


def test_done_before_clean_audit_fails_closed() -> None:
    callbacks = FakeCallbacks(managers=[_manager("done")], audits=[])
    outcome = asyncio.run(run_mea(task="Fix the project", callbacks=callbacks))
    assert outcome.status == "blocked"
    assert "prior clean complete audit" in outcome.reason
    assert not callbacks.executor_requests
    assert not callbacks.auditor_requests


def test_incomplete_audit_continues_and_preserves_state() -> None:
    incomplete = json.dumps(
        {
            "status": "incomplete",
            "summary": "one more check remains",
            "evidence": ["partial output"],
            "verified_facts": ["partial fact"],
            "remaining": ["run final test"],
        }
    )
    callbacks = FakeCallbacks(
        managers=[_manager("execute"), _manager("done")],
        audits=[incomplete],
    )
    outcome = asyncio.run(run_mea(task="Fix the project", callbacks=callbacks))
    assert outcome.status == "blocked"
    assert outcome.rounds == 2
    assert outcome.verified_facts == ()
    assert "prior clean complete audit" in outcome.reason
    second_user = callbacks.manager_requests[1].messages[-1].content
    assert "one more check remains" in second_user
    assert "run final test" in second_user


def test_invalid_manager_output_is_explicit() -> None:
    callbacks = FakeCallbacks(managers=["not json"], audits=[])
    with pytest.raises(OrchestrationError, match="manager output invalid"):
        asyncio.run(run_mea(task="Fix the project", callbacks=callbacks))
