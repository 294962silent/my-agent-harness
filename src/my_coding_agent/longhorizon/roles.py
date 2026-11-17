"""Production role backends for the Long-horizon MEA controller."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from my_agent_core import Agent
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.events import AgentEnd, MessageEnd, ToolExecutionStart, TurnEnd
from my_agent_core.env_filter import is_sensitive_env_key
from my_agent_core.hooks import ToolCallHook
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_agent_llm import Message
from my_agent_llm.models import TurnOutcome
from my_agent_llm.observation import observation_context
from my_coding_agent.permissions import PermissionGate
from my_coding_agent.tools import build_coding_tools
from my_coding_agent.tools.bash import BashResult, make_bash_tool
from my_coding_agent.tools.find import make_find_tool
from my_coding_agent.tools.grep import make_grep_tool
from my_coding_agent.tools.ls import make_ls_tool
from my_coding_agent.tools.read import make_read_tool

from .schema import AuditReport, ManagerDecision, RoleEpisode

EXECUTOR_TOOL_NAMES = (
    "read",
    "write",
    "edit",
    "bash",
    "grep",
    "find",
    "ls",
    "artifact_read",
    "artifact_search",
)
AUDITOR_TOOL_NAMES = ("read", "grep", "find", "ls", "bash")
AUDITOR_BASH_COMMANDS = (
    "python -m pytest -q",
    "python -m pytest",
    "uv run pytest -q",
    "uv run pytest",
)
_ERROR_MESSAGE_LIMIT = 1000
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
    re.compile(
        r"""(?ix)
        (
            ["']?
            (?:api[_-]?key|access[_-]?token|token|secret|password|authorization)
            ["']?\s*[:=]\s*["']?
        )
        [^"'\s,;}]+
        """
    ),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
)


class RoleProtocolError(RuntimeError):
    """A role returned output that violates its execution contract."""


@dataclass(frozen=True)
class CleanupEvidence:
    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


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
        workspace: Path | str | None = None,
    ) -> RoleEpisode: ...


MANAGER_SYSTEM_PROMPT = """\
You are the Manager for one bounded long-horizon MEA round.
Use no tools and do not claim that work occurred.
Return exactly one JSON object matching:
{"next":"execute|done|blocked|ask","reason":str,"subtask":str,
 "acceptance_criteria":[str],"question":str,"related_rounds":[positive_int]}
Use execute only with a bounded subtask and nonempty acceptance criteria.
Use done only from verified state, blocked when no safe action remains, and ask
only for a required human decision.
"""

EXECUTOR_SYSTEM_PROMPT = """\
You are the Executor for one bounded long-horizon MEA subtask.
Use only the registered file and shell tools. Stay inside the workspace.
Perform the requested change and run relevant checks. Never claim an action you
did not perform. Finish with a concise factual summary of changes, checks, and
remaining problems.
"""

AUDITOR_SYSTEM_PROMPT = """\
You are the independent read-only Auditor for one bounded long-horizon MEA
subtask. Use only the registered read, search, list, and safe shell tools.
Never modify files or fabricate evidence. Inspect the real workspace and verify
the claimed change against the acceptance criteria.

Bash accepts exactly one standalone command with no arguments, shell
metacharacters, or compound syntax. The only allowed commands are exactly:
`python -m pytest -q`, `python -m pytest`, `uv run pytest -q`, and
`uv run pytest`. Never use pipes, redirection, semicolons, `&&`, `cd`, or
additional convenience flags. Use read/grep/find/ls for inspection.
Once the evidence is sufficient, return the JSON object immediately.
Do not continue exploring. Return exactly one JSON object matching:
{"status":"complete|incomplete|blocked","summary":str,"evidence":[str],
 "verified_facts":[str],"remaining":[str]}
status=complete requires remaining=[]. Put nonblocking caveats in summary or
evidence, never in remaining. If any acceptance criterion is unmet or any
follow-up action is needed, use status=incomplete or status=blocked.
"""


def _safe_auditor_bash_tool(
    workspace: Path,
    raw_bash: Any | None = None,
) -> Any:
    underlying = raw_bash if raw_bash is not None else make_bash_tool(workspace)

    @tool(
        name="bash",
        description=(
            "Run one exact read-only pytest command in the auditor workspace. "
            "Allowed commands: "
            + ", ".join(AUDITOR_BASH_COMMANDS)
        ),
        prompt_snippet="Run one exact allowlisted pytest command",
        prompt_guidelines=["No arguments, pipes, redirection, chaining, or directory changes are allowed."],
        is_parallel_safe=False,
    )
    async def bash(
        command: str,
        timeout: int = 120,
        on_update: Callable[[Any], None] | None = None,
        signal: Any | None = None,
    ) -> Any:
        if command not in AUDITOR_BASH_COMMANDS:
            message = "Blocked: auditor bash permits only four exact pytest commands."
            return BashResult(ok=False, data=message, error=message)
        return await underlying(
            command,
            timeout=timeout,
            run_in_background=False,
            on_update=on_update,
            signal=signal,
        )

    return bash


def _json_mapping(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)


def _decision_mapping(decision: ManagerDecision) -> dict[str, Any]:
    if hasattr(decision, "to_dict"):
        return decision.to_dict()
    return {
        "next": decision.next,
        "reason": decision.reason,
        "subtask": decision.subtask,
        "acceptance_criteria": list(decision.acceptance_criteria),
        "question": decision.question,
        "related_rounds": list(decision.related_rounds),
    }


def _write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()


def _safe_error_message(
    failure: BaseException,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    message = str(failure)
    secrets = {
        value
        for name, value in os.environ.items()
        if value and len(value) >= 4 and is_sensitive_env_key(name)
    }
    secrets.update(value for value in extra_secrets if value and len(value) >= 4)
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "<redacted>")
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1<redacted>" if pattern.groups else "<redacted>"
        message = pattern.sub(replacement, message)
    message = re.sub(r"\s+", " ", message).strip()
    if not message:
        message = type(failure).__name__
    if len(message) > _ERROR_MESSAGE_LIMIT:
        message = message[: _ERROR_MESSAGE_LIMIT - 3] + "..."
    return message


def _usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RoleProtocolError("usage must be a mapping")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or type(item) is not int or item < 0:
            raise RoleProtocolError("usage values must be nonnegative integers")
        result[key] = item
    return result


def _message_text(message: Message) -> str:
    value = getattr(message, "text_content", None)
    if isinstance(value, str):
        return value
    content = message.content
    return content if isinstance(content, str) else ""


class _ScopedIterator:
    """Enter each generator step in the context of that step's caller."""

    def __init__(self, stream: Any, scope: Callable[[], Any]) -> None:
        self._stream = stream
        self._scope = scope

    def __iter__(self) -> _ScopedIterator:
        return self

    def __next__(self) -> Any:
        with self._scope():
            return next(self._stream)

    def close(self) -> Any:
        close = getattr(self._stream, "close", None)
        if not callable(close):
            return None
        with self._scope():
            return close()


class _ScopedAsyncIterator:
    """Keep ContextVar tokens inside each individual ``__anext__`` call."""

    def __init__(self, stream: Any, scope: Callable[[], Any]) -> None:
        self._stream = stream
        self._scope = scope

    def __aiter__(self) -> _ScopedAsyncIterator:
        return self

    async def __anext__(self) -> Any:
        with self._scope():
            return await anext(self._stream)

    async def aclose(self) -> Any:
        close = getattr(self._stream, "aclose", None)
        if not callable(close):
            return None
        with self._scope():
            return await close()


class _ContextualLLM:
    """Bind all model generations made for one role episode to one trace."""

    _ASYNC_GENERATORS = {"achat_stream", "astream_events"}
    _SYNC_GENERATORS = {"stream"}

    def __init__(
        self,
        raw: Any,
        *,
        root_id: str,
        purpose: str,
    ) -> None:
        self._raw = raw
        self._root_id = root_id
        self._purpose = purpose

    def _scope(self):
        return observation_context(root_id=self._root_id, purpose=self._purpose)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._raw, name)
        if name in {"chat", "achat"}:
            async_method = name == "achat"
            if async_method:
                async def call(*args: Any, **kwargs: Any) -> Any:
                    with self._scope():
                        return await value(*args, **kwargs)

                return call

            def sync_call(*args: Any, **kwargs: Any) -> Any:
                with self._scope():
                    return value(*args, **kwargs)

            return sync_call
        if name in self._ASYNC_GENERATORS:
            def async_iterator(*args: Any, **kwargs: Any) -> _ScopedAsyncIterator:
                return _ScopedAsyncIterator(value(*args, **kwargs), self._scope)

            return async_iterator
        if name in self._SYNC_GENERATORS:
            def iterator(*args: Any, **kwargs: Any) -> _ScopedIterator:
                return _ScopedIterator(iter(value(*args, **kwargs)), self._scope)

            return iterator
        return value


class LLMRoleBackend:
    """Shared-LLM role runner with injectable Agent and Session seams."""

    def __init__(
        self,
        *,
        llm: Any,
        workspace: Path | str,
        run_dir: Path | str,
        limits: Mapping[str, Any] | None = None,
        agent_factory: Callable[..., Any] | None = None,
        session_factory: Callable[..., Session] | None = None,
        executor_tools_factory: Callable[[Path], tuple[list[Any], Any]] | None = None,
        auditor_tools_factory: Callable[[Path], list[Any]] | None = None,
    ) -> None:
        self.llm = llm
        self.workspace = Path(workspace).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.prompt_dir = self.run_dir / "prompts"
        self.session_dir = self.run_dir / "sessions"
        self.cleanup_dir = self.run_dir / "cleanup"
        self.errors_dir = self.run_dir / "errors"
        self.limits = dict(limits or {})
        max_turns = self.limits.get("max_turns")
        max_seconds = self.limits.get("max_seconds")
        self.agent_run_limits = RunLimits(
            max_turns=int(max_turns) if max_turns is not None else None,
            max_seconds=float(max_seconds) if max_seconds is not None else None,
        )
        self.manager_timeout = (
            float(max_seconds) if max_seconds is not None else None
        )
        self.context_budget = self.limits.get("context_budget")
        self._agent_factory = agent_factory or Agent
        self._session_factory = session_factory or Session
        self._executor_tools_factory = executor_tools_factory
        self._auditor_tools_factory = auditor_tools_factory

    async def manager(
        self,
        *,
        task: str,
        state: Mapping[str, Any],
        round_index: int,
    ) -> RoleEpisode:
        with observation_context(
            root_id=self.run_dir.name,
            purpose=f"longhorizon_manager_r{round_index}",
        ):
            user_prompt = (
                f"{MANAGER_SYSTEM_PROMPT.strip()}\n\n"
                f"Original task:\n{task.rstrip()}\n\n"
                f"Current verified state:\n{_json_mapping(state)}\n\n"
                f"Round: {round_index}\n"
            )
            prompt_path = self.prompt_dir / f"round_{round_index:04d}_manager.txt"
            _write_once(prompt_path, user_prompt)
            call = self.llm.achat(
                [Message(role="user", content=user_prompt)],
                tools=[],
            )
            response = (
                await asyncio.wait_for(call, timeout=self.manager_timeout)
                if self.manager_timeout is not None
                else await call
            )
            tool_calls = getattr(response, "tool_calls", None)
            outcome = getattr(response, "outcome", None)
            if tool_calls or outcome is TurnOutcome.TOOL_CALLS:
                raise RoleProtocolError("manager must not return tool calls")
            if not isinstance(outcome, TurnOutcome):
                raise RoleProtocolError(
                    "manager response is missing a provider-neutral outcome"
                )
            if outcome is not TurnOutcome.COMPLETED:
                raise RoleProtocolError(
                    f"manager returned non-completed outcome: {outcome.value}"
                )
            content = getattr(response, "content", None)
            if not isinstance(content, str):
                raise RoleProtocolError("manager response content must be text")
            usage = _usage(getattr(response, "usage", None))
            terminal = "end_turn"
            self.session_dir.mkdir(parents=True, exist_ok=True)
            session_path = (
                self.session_dir / f"round_{round_index:04d}_manager.jsonl"
            )
            _write_once(
                session_path,
                json.dumps(
                    {
                        "role": "manager",
                        "round": round_index,
                        "prompt_file": prompt_path.name,
                        "output": content,
                        "terminal": terminal,
                        "usage": usage,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n",
            )
            self._write_cleanup(
                role="manager",
                round_index=round_index,
                evidence=CleanupEvidence(ok=True),
            )
            return RoleEpisode(
                output=content,
                terminal=terminal,
                iterations=1,
                tool_names=(),
                usage=(usage,) if usage else (),
                session_path=str(session_path),
                cleanup_ok=True,
            )

    async def executor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        state: Mapping[str, Any],
        round_index: int,
    ) -> RoleEpisode:
        with observation_context(
            root_id=self.run_dir.name,
            purpose=f"longhorizon_executor_r{round_index}",
        ):
            user_prompt = self._executor_prompt(task, decision, state, round_index)
            tools, artifact_store = self._executor_tools()
            return await self._run_agent_role(
                role="executor",
                system_prompt=EXECUTOR_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                tools=tools,
                artifact_store=artifact_store,
                hooks=[],
                round_index=round_index,
            )

    async def auditor(
        self,
        *,
        task: str,
        decision: ManagerDecision,
        executor_output: str,
        state: Mapping[str, Any],
        round_index: int,
        workspace: Path | str | None = None,
    ) -> RoleEpisode:
        auditor_workspace = (
            Path(workspace).expanduser()
            if workspace is not None
            else self.workspace
        )
        if auditor_workspace.is_symlink() or not auditor_workspace.is_dir():
            raise RoleProtocolError("auditor workspace must be a real directory")
        auditor_workspace = auditor_workspace.resolve()
        with observation_context(
            root_id=self.run_dir.name,
            purpose=f"longhorizon_auditor_r{round_index}",
        ):
            user_prompt = self._auditor_prompt(
                task, decision, executor_output, state, round_index
            )
            tools = self._auditor_tools(auditor_workspace)
            gate = PermissionGate(
                mode="review",
                confirm_callback=None,
                allow_without_callback=False,
                workspace=auditor_workspace,
            )
            return await self._run_agent_role(
                role="auditor",
                system_prompt=AUDITOR_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                tools=tools,
                artifact_store=None,
                hooks=[(ToolCallHook, gate)],
                round_index=round_index,
                session_cwd=auditor_workspace,
            )

    def _executor_tools(self) -> tuple[list[Any], Any]:
        if self._executor_tools_factory is not None:
            tools, artifact_store = self._executor_tools_factory(self.workspace)
        else:
            artifact_store = ArtifactStore(self.workspace)
            tools = build_coding_tools(
                self.workspace,
                artifact_store=artifact_store,
            )
        self._assert_tools("executor", tools, EXECUTOR_TOOL_NAMES)
        return list(tools), artifact_store

    def _auditor_tools(self, workspace: Path) -> list[Any]:
        if self._auditor_tools_factory is not None:
            tools = list(self._auditor_tools_factory(workspace))
        else:
            tools = [
                make_read_tool(workspace),
                make_grep_tool(workspace),
                make_find_tool(workspace),
                make_ls_tool(workspace),
            ]
        safe_bash = _safe_auditor_bash_tool(workspace)
        replaced = False
        for index, item in enumerate(tools):
            if getattr(item, "name", None) == "bash":
                tools[index] = safe_bash
                replaced = True
                break
        if not replaced:
            tools.append(safe_bash)
        self._assert_tools("auditor", tools, AUDITOR_TOOL_NAMES)
        return tools

    @staticmethod
    def _assert_tools(role: str, tools: list[Any], expected: tuple[str, ...]) -> None:
        names = tuple(getattr(tool, "name", None) for tool in tools)
        if names != expected:
            raise RoleProtocolError(
                f"{role} tools differ: expected={expected} actual={names}"
            )

    def _executor_prompt(
        self,
        task: str,
        decision: ManagerDecision,
        state: Mapping[str, Any],
        round_index: int,
    ) -> str:
        return (
            f"Original task:\n{task.rstrip()}\n\n"
            f"Manager decision:\n{_json_mapping(_decision_mapping(decision))}\n\n"
            f"Verified state:\n{_json_mapping(state)}\n\n"
            f"Round: {round_index}\n"
        )

    def _auditor_prompt(
        self,
        task: str,
        decision: ManagerDecision,
        executor_output: str,
        state: Mapping[str, Any],
        round_index: int,
    ) -> str:
        return (
            f"Original task:\n{task.rstrip()}\n\n"
            f"Manager decision:\n{_json_mapping(_decision_mapping(decision))}\n\n"
            f"Executor claim (untrusted):\n{executor_output.rstrip()}\n\n"
            f"Previously verified state:\n{_json_mapping(state)}\n\n"
            f"Round: {round_index}\n"
        )

    async def _run_agent_role(
        self,
        *,
        role: str,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        artifact_store: Any | None,
        hooks: list[tuple[type, Callable[..., Any]]],
        round_index: int,
        session_cwd: Path | str | None = None,
    ) -> RoleEpisode:
        prompt_path = self.prompt_dir / f"round_{round_index:04d}_{role}.txt"
        _write_once(prompt_path, f"{system_prompt.strip()}\n\n{user_prompt}")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_path = self.session_dir / f"round_{round_index:04d}_{role}_{uuid4().hex[:12]}.jsonl"
        session = self._session_factory(
            path=session_path,
            cwd=str(Path(session_cwd).resolve() if session_cwd is not None else self.workspace),
            metadata={"longhorizon_role": role, "round": round_index},
        )
        agent = self._agent_factory(
            llm=_ContextualLLM(
                self.llm,
                root_id=self.run_dir.name,
                purpose=f"longhorizon_{role}_r{round_index}",
            ),
            tools=tools,
            session=session,
            system_prompt=system_prompt,
            context_budget=self.context_budget,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            memory_dir=False,
            memory_index=None,
            memory_autodistill=False,
            task_store=False,
            retry_policy=AutoRetryPolicy(max_retries=0),
            run_limits=self.agent_run_limits,
            artifact_store=artifact_store,
            hooks=hooks,
        )
        outputs: list[str] = []
        tool_names: list[str] = []
        usages: list[dict[str, int]] = []
        agent_end: AgentEnd | None = None
        turn_count = 0
        failure: BaseException | None = None
        try:
            stream: AsyncIterator[Any] = agent.prompt_stream(user_prompt)
            async for event in stream:
                if isinstance(event, MessageEnd) and event.message.role == "assistant":
                    text = _message_text(event.message)
                    if text:
                        outputs.append(text)
                    usage = _usage((event.message.metadata or {}).get("usage"))
                    if usage:
                        usages.append(usage)
                elif isinstance(event, ToolExecutionStart):
                    if event.tool_name not in tool_names:
                        tool_names.append(event.tool_name)
                elif isinstance(event, TurnEnd):
                    turn_count += 1
                elif isinstance(event, AgentEnd):
                    agent_end = event
        except BaseException as exc:
            failure = exc
        finally:
            cleanup = await self._close_role_agent(
                role=role,
                round_index=round_index,
                agent=agent,
                session=session,
            )
        if failure is not None:
            safe_message = self._safe_role_error_message(failure)
            self._write_error_evidence(
                role=role,
                round_index=round_index,
                failure=failure,
                safe_message=safe_message,
                cleanup=cleanup,
            )
            raise RoleProtocolError(
                f"{role} execution failed: {type(failure).__name__}: "
                f"{safe_message}; cleanup_ok={cleanup.ok}; "
                f"cleanup_errors={list(cleanup.errors)}"
            ) from failure
        if agent_end is None:
            raise RoleProtocolError(f"{role} produced no AgentEnd")
        if not usages:
            for message in agent_end.messages:
                if message.role != "assistant":
                    continue
                usage = _usage((message.metadata or {}).get("usage"))
                if usage:
                    usages.append(usage)
        final_output = agent_end.final_text
        if not final_output and outputs:
            final_output = outputs[-1]
        return RoleEpisode(
            output=final_output or "",
            terminal=agent_end.stop_reason,
            iterations=agent_end.iterations if agent_end.iterations else turn_count,
            tool_names=tuple(tool_names),
            usage=tuple(usages),
            session_path=str(session_path),
            cleanup_ok=cleanup.ok,
        )

    def _safe_role_error_message(self, failure: BaseException) -> str:
        config = getattr(self.llm, "config", None)
        api_key = getattr(config, "api_key", None)
        extra = (api_key,) if isinstance(api_key, str) else ()
        return _safe_error_message(failure, extra_secrets=extra)

    def _write_error_evidence(
        self,
        *,
        role: str,
        round_index: int,
        failure: BaseException,
        safe_message: str,
        cleanup: CleanupEvidence,
    ) -> None:
        path = self.errors_dir / f"round_{round_index:04d}_{role}.json"
        payload = {
            "schema_version": 1,
            "role": role,
            "round": round_index,
            "error_type": type(failure).__name__,
            "message": safe_message,
            "cleanup_ok": cleanup.ok,
            "errors": list(cleanup.errors),
        }
        try:
            _write_once(
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            )
        except OSError:
            # Preserve the role failure even if diagnostic storage is unavailable.
            return

    async def _close_role_agent(
        self,
        *,
        role: str,
        round_index: int,
        agent: Any,
        session: Any,
    ) -> CleanupEvidence:
        errors: list[str] = []
        close = getattr(agent, "aclose", None)
        if not callable(close):
            errors.append("Agent.acloseUnavailable")
        else:
            try:
                await close()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        save = getattr(session, "save", None)
        if not callable(save):
            errors.append("Session.saveUnavailable")
        else:
            try:
                result = save()
                if isinstance(result, Awaitable):
                    await result
            except BaseException as exc:
                errors.append(type(exc).__name__)
        evidence = CleanupEvidence(ok=not errors, errors=tuple(errors))
        self._write_cleanup(
            role=role,
            round_index=round_index,
            evidence=evidence,
        )
        return evidence

    def _write_cleanup(
        self,
        *,
        role: str,
        round_index: int,
        evidence: CleanupEvidence,
    ) -> None:
        path = self.cleanup_dir / f"round_{round_index:04d}_{role}.json"
        _write_once(
            path,
            json.dumps(
                {
                    "role": role,
                    "round": round_index,
                    **evidence.to_dict(),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
        )


__all__ = [
    "AUDITOR_BASH_COMMANDS",
    "AUDITOR_TOOL_NAMES",
    "AuditReport",
    "CleanupEvidence",
    "EXECUTOR_TOOL_NAMES",
    "LLMRoleBackend",
    "ManagerDecision",
    "RoleBackend",
    "RoleEpisode",
    "RoleProtocolError",
]
