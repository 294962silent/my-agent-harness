"""Strict data contracts for the long-horizon MEA runtime."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


_JSON_FENCE_RE = re.compile(
    r"```json[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    flags=re.IGNORECASE | re.DOTALL,
)


class SchemaError(ValueError):
    """A role or persisted value does not satisfy the shared contract."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_value(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SchemaError(f"non-finite JSON number is forbidden: {value}")


def _decode_json(source: str) -> Any:
    return json.loads(
        source,
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
    )


def _matching_object_end(text: str, start: int) -> int | None:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _embedded_json_objects(text: str) -> list[tuple[int, int, dict[str, Any]]]:
    found: list[tuple[int, int, dict[str, Any]]] = []
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            return found
        end = _matching_object_end(text, start)
        if end is None:
            cursor = start + 1
            continue
        try:
            value = _decode_json(text[start:end])
        except (json.JSONDecodeError, TypeError, ValueError):
            cursor = start + 1
            continue
        if isinstance(value, dict):
            found.append((start, end, value))
        cursor = end


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse exactly one plain, fenced, or prose-embedded JSON object."""

    if not isinstance(text, str):
        raise SchemaError("role output must be text")
    stripped = text.strip()
    pure_fence = _JSON_FENCE_RE.fullmatch(stripped)
    if pure_fence is not None:
        source = pure_fence.group("body")
    else:
        fences = list(_JSON_FENCE_RE.finditer(stripped))
        if len(fences) > 1:
            raise SchemaError("ambiguous JSON code blocks")
        if not fences and "```" in stripped:
            raise SchemaError("only fenced json code blocks are supported")
        source = fences[0].group("body") if fences else stripped
    if not source:
        raise SchemaError("empty JSON output")
    try:
        value = _decode_json(source)
    except SchemaError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if fences:
            raise SchemaError(f"invalid JSON output: {exc}") from exc
        candidates = _embedded_json_objects(stripped)
        if len(candidates) > 1:
            raise SchemaError("ambiguous JSON objects")
        if not candidates:
            raise SchemaError(f"invalid JSON output: {exc}") from exc
        value = candidates[0][2]
    if not isinstance(value, dict):
        raise SchemaError("role output must be a JSON object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    name: str,
) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise SchemaError(f"{name} missing fields: {sorted(missing)}")
    if extra:
        raise SchemaError(f"{name} has forbidden fields: {sorted(extra)}")


def _text(value: object, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise SchemaError(f"{field} must be a nonempty string")
    return value


def _text_tuple(value: object, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{field} must be a list")
    result = tuple(_text(item, field) for item in value)
    if nonempty and not result:
        raise SchemaError(f"{field} must not be empty")
    return result


@dataclass(frozen=True)
class ManagerDecision:
    next: Literal["execute", "done", "blocked", "ask"]
    reason: str
    subtask: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    question: str = ""
    related_rounds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.next not in {"execute", "done", "blocked", "ask"}:
            raise SchemaError("manager.next must be execute, done, blocked or ask")
        _text(self.reason, "manager.reason")
        if not isinstance(self.subtask, str) or not isinstance(self.question, str):
            raise SchemaError("manager subtask/question must be strings")
        criteria = _text_tuple(self.acceptance_criteria, "manager.acceptance_criteria", nonempty=self.next == "execute")
        if self.next == "execute" and not self.subtask.strip():
            raise SchemaError("execute requires a nonempty subtask")
        if self.next == "ask" and not self.question.strip():
            raise SchemaError("ask requires a nonempty question")
        if not isinstance(self.related_rounds, tuple):
            object.__setattr__(self, "related_rounds", tuple(self.related_rounds))
        if any(type(index) is not int or index < 1 for index in self.related_rounds):
            raise SchemaError("manager.related_rounds must contain positive integers")
        if len(set(self.related_rounds)) != len(self.related_rounds):
            raise SchemaError("manager.related_rounds contains duplicates")
        object.__setattr__(self, "acceptance_criteria", criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "next": self.next,
            "reason": self.reason,
            "subtask": self.subtask,
            "acceptance_criteria": list(self.acceptance_criteria),
            "question": self.question,
            "related_rounds": list(self.related_rounds),
        }


@dataclass(frozen=True)
class AuditReport:
    status: Literal["complete", "incomplete", "blocked"]
    summary: str
    evidence: tuple[str, ...]
    verified_facts: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"complete", "incomplete", "blocked"}:
            raise SchemaError("audit.status must be complete, incomplete or blocked")
        _text(self.summary, "audit.summary")
        evidence = _text_tuple(self.evidence, "audit.evidence", nonempty=True)
        verified = _text_tuple(self.verified_facts, "audit.verified_facts")
        remaining = _text_tuple(self.remaining, "audit.remaining")
        if self.status == "complete" and remaining:
            raise SchemaError(
                "audit.remaining must be empty when audit.status is complete"
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "verified_facts", verified)
        object.__setattr__(self, "remaining", remaining)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "verified_facts": list(self.verified_facts),
            "remaining": list(self.remaining),
        }


@dataclass(frozen=True)
class RoleEpisode:
    output: str
    terminal: str
    iterations: int
    tool_names: tuple[str, ...]
    usage: tuple[dict[str, int], ...]
    session_path: str
    cleanup_ok: bool

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise SchemaError("episode.output must be a string")
        _text(self.terminal, "episode.terminal")
        if type(self.iterations) is not int or self.iterations < 0:
            raise SchemaError("episode.iterations must be a nonnegative integer")
        names = _text_tuple(self.tool_names, "episode.tool_names")
        if len(set(names)) != len(names):
            raise SchemaError("episode.tool_names contains duplicates")
        if not isinstance(self.usage, (list, tuple)):
            raise SchemaError("episode.usage must be a list")
        normalized_usage: list[dict[str, int]] = []
        for item in self.usage:
            if not isinstance(item, Mapping):
                raise SchemaError("episode.usage entries must be objects")
            row: dict[str, int] = {}
            for key, value in item.items():
                if not isinstance(key, str) or type(value) is not int or value < 0:
                    raise SchemaError("episode.usage values must be nonnegative integers")
                row[key] = value
            normalized_usage.append(row)
        _text(self.session_path, "episode.session_path")
        if type(self.cleanup_ok) is not bool:
            raise SchemaError("episode.cleanup_ok must be boolean")
        object.__setattr__(self, "tool_names", names)
        object.__setattr__(self, "usage", tuple(normalized_usage))

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "terminal": self.terminal,
            "iterations": self.iterations,
            "tool_names": list(self.tool_names),
            "usage": [dict(item) for item in self.usage],
            "session_path": self.session_path,
            "cleanup_ok": self.cleanup_ok,
        }


class RoleBackend(Protocol):
    async def manager(
        self,
        *,
        task: str,
        state: Mapping[str, Any],
        round_index: int,
    ) -> RoleEpisode: ...

    async def executor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        state: Mapping[str, Any],
        round_index: int,
    ) -> RoleEpisode: ...

    async def auditor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        executor_output: str,
        state: Mapping[str, Any],
        round_index: int,
    ) -> RoleEpisode: ...


RoleResult = RoleEpisode


@dataclass(frozen=True)
class RoundRecord:
    round_index: int
    decision: ManagerDecision
    executor: RoleEpisode
    auditor: RoleEpisode
    audit: AuditReport

    def __post_init__(self) -> None:
        if type(self.round_index) is not int or self.round_index < 1:
            raise SchemaError("round_index must be a positive integer")
        if not isinstance(self.decision, ManagerDecision):
            raise SchemaError("decision must be ManagerDecision")
        if not isinstance(self.executor, RoleEpisode) or not isinstance(self.auditor, RoleEpisode):
            raise SchemaError("executor/auditor must be RoleEpisode")
        if not isinstance(self.audit, AuditReport):
            raise SchemaError("audit must be AuditReport")

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "decision": self.decision.to_dict(),
            "executor": self.executor.to_dict(),
            "auditor": self.auditor.to_dict(),
            "audit": self.audit.to_dict(),
        }


def parse_manager_decision(text: str) -> ManagerDecision:
    value = parse_json_object(text)
    _require_exact_keys(
        value,
        required={"next", "reason"},
        optional={"subtask", "acceptance_criteria", "question", "related_rounds"},
        name="manager decision",
    )
    return ManagerDecision(
        next=value["next"],
        reason=value["reason"],
        subtask=value.get("subtask", ""),
        acceptance_criteria=value.get("acceptance_criteria", ()),
        question=value.get("question", ""),
        related_rounds=value.get("related_rounds", ()),
    )


def parse_audit_report(text: str) -> AuditReport:
    value = parse_json_object(text)
    _require_exact_keys(
        value,
        required={"status", "summary", "evidence"},
        optional={"verified_facts", "remaining"},
        name="audit report",
    )
    return AuditReport(
        status=value["status"],
        summary=value["summary"],
        evidence=value["evidence"],
        verified_facts=value.get("verified_facts", ()),
        remaining=value.get("remaining", ()),
    )


def parse_role_episode(text: str) -> RoleEpisode:
    value = parse_json_object(text)
    _require_exact_keys(
        value,
        required={
            "output",
            "terminal",
            "iterations",
            "tool_names",
            "usage",
            "session_path",
            "cleanup_ok",
        },
        name="role episode",
    )
    return RoleEpisode(**value)


def parse_round_record(text: str) -> RoundRecord:
    value = parse_json_object(text)
    _require_exact_keys(
        value,
        required={"round_index", "decision", "executor", "auditor", "audit"},
        name="round record",
    )
    decision = value["decision"]
    executor = value["executor"]
    auditor = value["auditor"]
    audit = value["audit"]
    if not all(isinstance(item, Mapping) for item in (decision, executor, auditor, audit)):
        raise SchemaError("round record nested values must be objects")
    return RoundRecord(
        round_index=value["round_index"],
        decision=parse_manager_decision(json.dumps(decision, ensure_ascii=False)),
        executor=parse_role_episode(json.dumps(executor, ensure_ascii=False)),
        auditor=parse_role_episode(json.dumps(auditor, ensure_ascii=False)),
        audit=parse_audit_report(json.dumps(audit, ensure_ascii=False)),
    )
