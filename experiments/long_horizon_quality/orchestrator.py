"""Cache-stable Manager -> Executor -> Auditor orchestration core."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol

from my_coding_agent.longhorizon.schema import (
    parse_audit_report,
    parse_manager_decision,
)


class OrchestrationError(RuntimeError):
    """The role surface violated the orchestration contract."""


@dataclass(frozen=True)
class RoleMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise OrchestrationError("role message has an unsupported role")
        if not isinstance(self.content, str) or not self.content.strip():
            raise OrchestrationError("role message content must be nonempty text")


@dataclass(frozen=True)
class RoleRequest:
    purpose: str
    messages: tuple[RoleMessage, ...]

    def __post_init__(self) -> None:
        if not self.purpose or self.messages[0].role != "system":
            raise OrchestrationError("role request requires purpose and system prefix")


class RoleCallbacks(Protocol):
    async def manager(self, request: RoleRequest) -> str: ...

    async def executor(self, request: RoleRequest) -> str: ...

    async def auditor(self, request: RoleRequest) -> str: ...


@dataclass(frozen=True)
class MEAOutcome:
    status: str
    rounds: int
    verified_facts: tuple[str, ...]
    last_audit: dict[str, Any] | None
    reason: str
    role_prompt_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "verified_facts": list(self.verified_facts),
            "last_audit": self.last_audit,
            "reason": self.reason,
            "role_prompt_hashes": self.role_prompt_hashes,
        }


MANAGER_SYSTEM_PROMPT = """\
You are the Manager in a long-horizon coding experiment.
Use the official user task and the verified facts from prior independent audits.
Return exactly one JSON object with:
{"next":"execute|done|blocked|ask","reason":"...","subtask":"...",
 "acceptance_criteria":["..."],"question":"...","related_rounds":[1]}
next=execute requires a bounded subtask and nonempty acceptance_criteria.
next=done requires a prior clean complete audit and no remaining work.
Never claim an action that no Executor or Auditor verified.
"""

EXECUTOR_SYSTEM_PROMPT = """\
You are the Executor in a long-horizon coding experiment.
Complete only the Manager's bounded subtask within the official task.
Use only the provided tools and official container. Do not inspect reference
solutions, hidden verifier tests, scoring files or experiment receipts.
Never modify protected inputs. Finish with a concise factual summary of
changes, commands, test results and remaining problems.
"""

AUDITOR_SYSTEM_PROMPT = """\
You are the independent Auditor in a long-horizon coding experiment.
The Executor summary is untrusted. Inspect the actual workspace and run only
approved read-only checks. Return exactly one JSON object with:
{"status":"complete|incomplete|blocked","summary":"...",
 "evidence":["..."],"verified_facts":["..."],"remaining":["..."]}
status=complete requires remaining=[] and direct evidence for each fact.
Never modify the workspace and never accept a claim without checking it.
"""

ROLE_PROMPT_HASHES = {
    "manager": hashlib.sha256(MANAGER_SYSTEM_PROMPT.encode()).hexdigest(),
    "executor": hashlib.sha256(EXECUTOR_SYSTEM_PROMPT.encode()).hexdigest(),
    "auditor": hashlib.sha256(AUDITOR_SYSTEM_PROMPT.encode()).hexdigest(),
}


def _state_prompt(
    *,
    task: str,
    round_index: int,
    verified_facts: tuple[str, ...],
    last_audit: dict[str, Any] | None,
) -> str:
    state = {
        "round": round_index,
        "verified_facts": list(verified_facts),
        "last_audit": last_audit,
    }
    return (
        "Original task:\n"
        + task
        + "\n\nAppend-only current state:\n"
        + json.dumps(state, ensure_ascii=False, sort_keys=True)
    )


async def _call_role(
    callback,
    request: RoleRequest,
) -> str:
    try:
        value = await callback(request)
    except OrchestrationError:
        raise
    except Exception as exc:
        raise OrchestrationError(f"{request.purpose} callback failed") from exc
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"{request.purpose} returned empty text")
    return value


async def run_mea(
    *,
    task: str,
    callbacks: RoleCallbacks,
    max_rounds: int = 3,
) -> MEAOutcome:
    if not isinstance(task, str) or not task.strip():
        raise OrchestrationError("task must be nonempty text")
    if type(max_rounds) is not int or max_rounds < 1:
        raise OrchestrationError("max_rounds must be positive")

    manager_messages: list[RoleMessage] = [
        RoleMessage(role="system", content=MANAGER_SYSTEM_PROMPT)
    ]
    verified_facts: tuple[str, ...] = ()
    last_audit: dict[str, Any] | None = None
    completed_rounds = 0

    for round_index in range(1, max_rounds + 1):
        completed_rounds = round_index
        manager_messages.append(
            RoleMessage(
                role="user",
                content=_state_prompt(
                    task=task,
                    round_index=round_index,
                    verified_facts=verified_facts,
                    last_audit=last_audit,
                ),
            )
        )
        manager_text = await _call_role(
            callbacks.manager,
            RoleRequest(
                purpose=f"mea_manager_r{round_index}",
                messages=tuple(manager_messages),
            ),
        )
        manager_messages.append(RoleMessage(role="assistant", content=manager_text))
        try:
            decision = parse_manager_decision(manager_text)
        except ValueError as exc:
            raise OrchestrationError(f"manager output invalid: {exc}") from exc

        if decision.next == "done":
            if last_audit is None or last_audit.get("status") != "complete":
                return MEAOutcome(
                    status="blocked",
                    rounds=completed_rounds,
                    verified_facts=verified_facts,
                    last_audit=last_audit,
                    reason="done requires a prior clean complete audit",
                    role_prompt_hashes=ROLE_PROMPT_HASHES,
                )
            return MEAOutcome(
                status="complete",
                rounds=completed_rounds,
                verified_facts=verified_facts,
                last_audit=last_audit,
                reason=decision.reason,
                role_prompt_hashes=ROLE_PROMPT_HASHES,
            )
        if decision.next == "ask":
            return MEAOutcome(
                status="waiting_input",
                rounds=completed_rounds,
                verified_facts=verified_facts,
                last_audit=last_audit,
                reason=decision.question,
                role_prompt_hashes=ROLE_PROMPT_HASHES,
            )
        if decision.next == "blocked":
            return MEAOutcome(
                status="blocked",
                rounds=completed_rounds,
                verified_facts=verified_facts,
                last_audit=last_audit,
                reason=decision.reason,
                role_prompt_hashes=ROLE_PROMPT_HASHES,
            )
        if decision.next != "execute":
            raise OrchestrationError("unsupported manager decision")

        executor_text = await _call_role(
            callbacks.executor,
            RoleRequest(
                purpose=f"mea_executor_r{round_index}",
                messages=(
                    RoleMessage(role="system", content=EXECUTOR_SYSTEM_PROMPT),
                    RoleMessage(
                        role="user",
                        content=(
                            "Original task:\n"
                            + task
                            + "\n\nManager decision:\n"
                            + json.dumps(
                                decision.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        ),
                    ),
                ),
            ),
        )
        auditor_text = await _call_role(
            callbacks.auditor,
            RoleRequest(
                purpose=f"mea_auditor_r{round_index}",
                messages=(
                    RoleMessage(role="system", content=AUDITOR_SYSTEM_PROMPT),
                    RoleMessage(
                        role="user",
                        content=(
                            "Original task:\n"
                            + task
                            + "\n\nManager decision:\n"
                            + json.dumps(
                                decision.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            + "\n\nExecutor claim (untrusted):\n"
                            + executor_text
                        ),
                    ),
                ),
            ),
        )
        try:
            audit = parse_audit_report(auditor_text)
        except ValueError as exc:
            raise OrchestrationError(f"auditor output invalid: {exc}") from exc
        last_audit = audit.to_dict()
        if audit.status == "blocked":
            return MEAOutcome(
                status="blocked",
                rounds=completed_rounds,
                verified_facts=verified_facts,
                last_audit=last_audit,
                reason=audit.summary,
                role_prompt_hashes=ROLE_PROMPT_HASHES,
            )
        if audit.status == "complete":
            verified_facts = tuple(
                dict.fromkeys([*verified_facts, *audit.verified_facts])
            )

    return MEAOutcome(
        status="blocked",
        rounds=completed_rounds,
        verified_facts=verified_facts,
        last_audit=last_audit,
        reason="max rounds reached before a terminal decision",
        role_prompt_hashes=ROLE_PROMPT_HASHES,
    )
