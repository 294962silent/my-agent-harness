"""Host-side Harbor adapter. No host filesystem or verifier tools reach the model."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import hashlib
import math
import os
import re
from pathlib import Path
import shlex
import traceback
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.models.task.task import Task

from my_agent_core.agent import Agent
from my_agent_core.events import AgentEnd, MessageEnd
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.context_compaction import CompactionPolicy
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_coding_agent.tools.artifact import make_artifact_tools
from my_coding_agent.longhorizon.schema import (
    SchemaError,
    parse_audit_report,
    parse_manager_decision,
)
from my_agent_llm import Message, TurnOutcome
from my_agent_llm.observation import observation_context

from .budget import BudgetLedger, BudgetStop, GuardedLLM, create_live_llm, LIVE_MODEL
from .observation import ExperimentLLMOwner, ObservationRequiredError
from .tasks import oracle_gate, agent_instruction, runtime_constraints
from experiments.long_horizon_quality.orchestrator import (
    AUDITOR_SYSTEM_PROMPT,
    EXECUTOR_SYSTEM_PROMPT,
    MANAGER_SYSTEM_PROMPT,
    RoleRequest,
    run_mea,
)

SINGLE_AGENT_SYSTEM_PROMPT = (
    "Complete the user task in the container using shell. Do not access benchmark "
    "solutions, hidden verifier tests, or scoring files. Finish with a concise "
    "description of your changes."
)
_SHELL_META_RE = re.compile(r"(?:\r|\n|;|&&|\|\||[|<>`]|\$\()")
_AUDIT_FIRST_COMMANDS = frozenset(
    {"cat", "find", "git", "grep", "head", "ls", "pwd", "sed", "tail", "wc"}
)
_MEA_EXECUTOR_RESERVED_REQUESTS = 24
_MEA_AUDITOR_MAX_REQUESTS = 20
_MEA_EXECUTOR_ROUND_CAPS = {1: 16, 2: 32, 3: 12, 4: 8}


def _message_text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    text = getattr(message, "text_content", "")
    return text if isinstance(text, str) else ""


async def _run_agent_and_capture(agent, instruction: str, timeout: float) -> tuple[str, dict]:
    assistant_text: list[str] = []
    terminal: dict = {}

    def observe(event) -> None:
        if isinstance(event, MessageEnd) and getattr(event.message, "role", None) == "assistant":
            text = _message_text(event.message)
            if text.strip():
                assistant_text.append(text)
        if isinstance(event, AgentEnd):
            terminal.update(
                stop_reason=event.stop_reason,
                limit_details=event.limit_details,
            )

    agent.subscribe(observe)
    await asyncio.wait_for(agent.run(instruction), timeout=timeout)
    if not assistant_text:
        raise RuntimeError("agent ended without assistant text")
    return assistant_text[-1], terminal


def _safe_auditor_command(command: object) -> tuple[bool, str]:
    if not isinstance(command, str):
        return False, "command must be text"
    text = command.strip()
    if not text or len(text) > 1000:
        return False, "command is empty or too long"
    if _SHELL_META_RE.search(text):
        return False, "shell metacharacters are forbidden"
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return False, "command quoting is invalid"
    if not tokens:
        return False, "command is empty"
    if any(token == ".." or "/../" in f"/{token}/" or token.startswith("/") for token in tokens):
        return False, "absolute or parent paths are forbidden"
    first = tokens[0]
    if first in {"python", "python3"}:
        if len(tokens) < 3 or tokens[1:3] != ["-m", "pytest"]:
            return False, "python is restricted to -m pytest"
        return True, text
    if first == "uv":
        if len(tokens) < 3 or tokens[1:3] != ["run", "pytest"]:
            return False, "uv is restricted to run pytest"
        return True, text
    if first not in _AUDIT_FIRST_COMMANDS:
        return False, "command is not allowlisted"
    if first == "find" and any(
        token in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"}
        for token in tokens[1:]
    ):
        return False, "find mutation flags are forbidden"
    if first == "sed":
        if "-n" not in tokens[1:] or any(
            token.startswith("-i") or token == "--in-place" for token in tokens[1:]
        ):
            return False, "sed is restricted to non-mutating -n mode"
    if first == "git":
        if len(tokens) < 2 or tokens[1] not in {
            "branch",
            "cat-file",
            "diff",
            "log",
            "ls-files",
            "reflog",
            "rev-parse",
            "show",
            "status",
        }:
            return False, "git is restricted to read-only subcommands"
        if tokens[1] == "branch" and any(
            token in {
                "-c",
                "-C",
                "-d",
                "-D",
                "-m",
                "-M",
                "--copy",
                "--delete",
                "--move",
            }
            for token in tokens[2:]
        ):
            return False, "git branch mutation flags are forbidden"
        if tokens[1] == "reflog" and any(
            token in {"delete", "drop", "expire"} for token in tokens[2:]
        ):
            return False, "git reflog mutation is forbidden"
    return True, text


def _executor_turn_budget(
    *,
    available: int,
    round_index: int,
    max_rounds: int,
) -> int:
    if round_index < 1 or round_index > max_rounds:
        raise ValueError("executor round is outside the frozen MEA limit")
    remaining_rounds = max_rounds - round_index + 1
    reserve = min(
        _MEA_EXECUTOR_RESERVED_REQUESTS,
        2 * (remaining_rounds - 1) + 6,
    )
    cap = _MEA_EXECUTOR_ROUND_CAPS.get(round_index, 8)
    return min(cap, available - reserve)


def _append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _bounded(text: str, limit: int) -> str:
    clean = str(text).replace("\r", " ")
    return clean if len(clean) <= limit else clean[: limit - 14] + "...[truncated]"


def _safe_exception(exc: BaseException) -> dict:
    message = str(exc)
    for name in ("LIVE_BENCH_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", message)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for name in ("LIVE_BENCH_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        value = os.environ.get(name)
        if value:
            trace = trace.replace(value, "[REDACTED]")
    return {
        "type": type(exc).__name__,
        "message": _bounded(message, 1000),
        "traceback": _bounded(trace, 8000),
    }


async def _request_manager_json(
    *,
    llm,
    request: RoleRequest,
    remaining_timeout,
    role_log: Path,
) -> str:
    base_messages = [
        Message(role=message.role, content=message.content)
        for message in request.messages
    ]
    messages = base_messages
    last_failure = "manager response invalid"
    for attempt in (1, 2):
        with observation_context(purpose=request.purpose):
            response = await asyncio.wait_for(
                llm.achat(
                    messages,
                    tools=[],
                    response_format={"type": "json_object"},
                ),
                timeout=max(1.0, remaining_timeout()),
            )
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            raise RuntimeError("manager returned tool calls")
        outcome = getattr(response, "outcome", None)
        finish_reason = getattr(response, "finish_reason", None)
        content = getattr(response, "content", None)
        _append_jsonl(
            role_log,
            {
                "purpose": request.purpose,
                "attempt": attempt,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "output": content,
                "outcome": (
                    outcome.value if isinstance(outcome, TurnOutcome) else outcome
                ),
                "finish_reason": finish_reason,
            },
        )
        if (
            outcome is not None
            and outcome not in (TurnOutcome.COMPLETED, TurnOutcome.LENGTH)
        ):
            raise RuntimeError("manager response did not complete")
        if isinstance(content, str) and content.strip():
            try:
                parse_manager_decision(content)
            except SchemaError as exc:
                last_failure = str(exc)
            else:
                return content
        else:
            last_failure = "manager response content is empty"
        if attempt == 1:
            state_index = len(base_messages) - 1
            messages = list(base_messages)
            messages[state_index] = Message(
                role="user",
                content=(
                    base_messages[state_index].content
                    + "\n\nCorrection: your previous response was unusable. "
                    "Return one compact JSON object with the exact required keys "
                    "and no prose, Markdown, or repeated evidence."
                ),
            )
    raise RuntimeError(f"manager response invalid after bounded repair: {last_failure}")


def _render_audit_transcript(messages, *, limit: int = 24_000) -> str:
    """Render the newest bounded audit evidence without truncating the tail."""
    entries: list[str] = []
    for message in messages:
        text = _message_text(message).strip()
        metadata = getattr(message, "metadata", None) or {}
        tool_calls = metadata.get("tool_calls") if isinstance(metadata, dict) else None
        tool_call_id = metadata.get("tool_call_id") if isinstance(metadata, dict) else None
        if not text and not tool_calls:
            continue
        header = f"[{getattr(message, 'role', 'unknown')}]"
        if tool_call_id:
            header += f" tool_call_id={tool_call_id}"
        body = _bounded(text, 3_000)
        if tool_calls:
            calls = _bounded(
                json.dumps(tool_calls, ensure_ascii=False, default=str),
                1_000,
            )
            body = f"{body}\ntool_calls={calls}".strip()
        entries.append(f"{header}\n{body}")

    selected: list[str] = []
    remaining = max(1, limit)
    for entry in reversed(entries):
        separator = 2 if selected else 0
        if len(entry) + separator > remaining:
            continue
        selected.append(entry)
        remaining -= len(entry) + separator
        if remaining <= 0:
            break
    selected.reverse()
    return "\n\n".join(selected)


def _fallback_audit_report(*, validation_error: str) -> str:
    """Return a conservative parseable blocked report after projection failure."""
    return json.dumps(
        {
            "status": "blocked",
            "summary": (
                "The Auditor collected evidence but did not produce a valid "
                "JSON report within the bounded repair budget; no completion "
                "claim is made."
            ),
            "evidence": ["bounded Auditor transcript captured"],
            "verified_facts": [],
            "remaining": [
                "Fresh independent audit required: "
                + _bounded(validation_error, 500)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def _request_auditor_projection(
    *,
    llm,
    request: RoleRequest,
    transcript: str,
    last_output: str | None,
    validation_error: str,
    remaining_timeout,
    role_log: Path,
) -> str:
    """Project collected audit evidence into the exact JSON contract."""
    system = request.messages[0].content
    original = request.messages[-1].content
    messages = [
        Message(role="system", content=system),
        Message(
            role="user",
            content=(
                "Original audit request:\n"
                + original
                + "\n\nBounded Auditor transcript (tool calls and results):\n"
                + transcript
                + "\n\nLast Auditor output:\n"
                + _bounded(last_output or "", 4_000)
                + "\n\nValidation failure:\n"
                + _bounded(validation_error, 1_000)
                + "\n\nReturn exactly one JSON object with status, summary, "
                "evidence, verified_facts, and remaining. Do not call tools. "
                "Use only evidence present in the transcript. If the evidence "
                "is insufficient, use status=blocked. status=complete requires "
                "remaining=[]. Return compact JSON only."
            ),
        ),
    ]
    purpose = f"{request.purpose}_projection"
    with observation_context(purpose=purpose):
        response = await asyncio.wait_for(
            llm.achat(
                messages,
                tools=[],
                response_format={"type": "json_object"},
                max_tokens=2_048,
            ),
            timeout=max(1.0, remaining_timeout()),
        )
    tool_calls = getattr(response, "tool_calls", None)
    outcome = getattr(response, "outcome", None)
    finish_reason = getattr(response, "finish_reason", None)
    content = getattr(response, "content", None)
    _append_jsonl(
        role_log,
        {
            "purpose": purpose,
            "attempt": 3,
            "mode": "transcript_json_projection",
            "output": content,
            "outcome": (
                outcome.value if isinstance(outcome, TurnOutcome) else outcome
            ),
            "finish_reason": finish_reason,
            "validation_error": validation_error,
        },
    )
    if tool_calls:
        raise RuntimeError("Auditor JSON projection returned tool calls")
    if (
        outcome is not None
        and outcome not in (TurnOutcome.COMPLETED, TurnOutcome.LENGTH)
    ):
        raise RuntimeError("Auditor JSON projection did not complete")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Auditor JSON projection returned empty content")
    parse_audit_report(content)
    return content


class ObservedLLM:
    """Run every budget-guarded model call inside one metadata-only experiment scope."""

    def __init__(self, guarded, owner, *, run_id: str, arm: str, task_id: str):
        self.guarded = guarded
        self.owner = owner
        self.config = guarded.config
        self.model = guarded.model
        self.run_id = run_id
        self.arm = arm
        self.task_id = task_id

    @contextmanager
    def _scope(self):
        with self.owner.observe(
            self.run_id,
            arm=self.arm,
            stage="final",
            task_id=self.task_id,
        ):
            yield

    def chat(self, messages, **kwargs):
        with self._scope():
            return self.guarded.chat(messages, **kwargs)

    async def achat(self, messages, **kwargs):
        with self._scope():
            return await self.guarded.achat(messages, **kwargs)

    def stream(self, messages, **kwargs):
        with self._scope():
            chunks = list(self.guarded.stream(messages, **kwargs))
        yield from chunks

    async def achat_stream(self, messages, **kwargs):
        async def collect():
            with self._scope():
                return [chunk async for chunk in self.guarded.achat_stream(messages, **kwargs)]

        chunks = await collect()
        for chunk in chunks:
            yield chunk

    async def astream_events(self, messages, *, signal=None, **kwargs):
        if signal is not None and signal.is_cancelled():
            raise asyncio.CancelledError()
        async def collect():
            with self._scope():
                return [event async for event in self.guarded.astream_events(messages, signal=signal, **kwargs)]

        events = await collect()
        for event in events:
            yield event

    def __getattr__(self, name):
        return getattr(self.guarded, name)


class BudgetedHarborAgent(BaseAgent):
    """Harbor adapter for single-ReAct and Manager/Executor/Auditor arms."""

    @staticmethod
    def name():
        return "budgeted-my-agent-harness"

    def version(self):
        return "2"

    def __init__(self, *, task_dir, logs_dir, oracle_result, ledger_path, artifact_projection="on",
                 compaction_mode="baseline",
                 orchestration_mode="react", experiment_arm=None, max_rounds=3,
                 observation_mode="off", observation_required=False,
                 observation_base_url="https://cloud.langfuse.com",
                 observation_public_key_env="LANGFUSE_PUBLIC_KEY",
                 observation_secret_key_env="LANGFUSE_SECRET_KEY",
                 observation_close_timeout=15,
                 campaign_id=None, run_id=None, max_requests=16, max_seconds=600,
                 context_budget=32000, output_tokens=4096, shell_timeout=60,
                 spend_ceiling_usd=None, provider_timeout=90, **kwargs):
        super().__init__(logs_dir=Path(logs_dir), **kwargs)
        if artifact_projection not in ("on", "off"):
            raise ValueError("artifact_projection must be on/off")
        if compaction_mode not in ("baseline", "layered"):
            raise ValueError("compaction_mode must be baseline/layered")
        if orchestration_mode not in ("react", "mea"):
            raise ValueError("orchestration_mode must be react/mea")
        if type(max_rounds) is not int or not 1 <= max_rounds <= 4:
            raise ValueError("max_rounds must be an integer in 1..4")
        if observation_mode not in ("off", "langfuse"):
            raise ValueError("observation_mode must be off/langfuse")
        if observation_required and observation_mode != "langfuse":
            raise ValueError("required observation needs observation_mode=langfuse")
        if observation_mode == "langfuse" and campaign_id is None:
            raise ValueError("langfuse observation requires a frozen campaign_id")
        if (type(provider_timeout) not in (int, float)
                or not math.isfinite(provider_timeout)
                or not 0 < provider_timeout <= 300):
            raise ValueError("provider_timeout must be in 0..300 seconds")
        if (type(observation_close_timeout) not in (int, float)
                or not math.isfinite(observation_close_timeout)
                or not 0 < observation_close_timeout <= 60):
            raise ValueError("invalid observation close timeout")
        self.task_dir = Path(task_dir)
        self.gate = oracle_gate(self.task_dir, oracle_result)
        if Task(self.task_dir).checksum != self.gate["harbor_task_checksum"]:
            raise ValueError("oracle checksum differs from current official task")
        if campaign_id is not None and (not Path(ledger_path).is_absolute() or not Path(ledger_path).is_file()):
            raise ValueError("campaign requires the existing absolute shared ledger")
        self.ledger = BudgetLedger(Path(ledger_path), campaign_id=campaign_id, spend_ceiling_usd=spend_ceiling_usd)
        if (type(max_requests) is not int or not 1 <= max_requests <= 64
                or type(context_budget) is not int or context_budget != 32000
                or type(output_tokens) is not int or not 1 <= output_tokens <= 4096
                or type(shell_timeout) not in (int, float) or not 0 < shell_timeout <= 120):
            raise ValueError("invalid frozen Harbor limits")
        official = runtime_constraints(self.task_dir)["time_cap"]
        if type(max_seconds) not in (int, float) or not 0 < max_seconds <= official:
            raise ValueError("agent time exceeds official task timeout or global limit")
        if campaign_id is not None and not run_id:
            raise ValueError("campaign requires a frozen run_id")
        self.campaign_id = campaign_id
        self.run_id = run_id or uuid4().hex
        if campaign_id is not None:
            self.ledger.register_run(self.run_id, max_requests=max_requests, time_cap=max_seconds)
        self.max_requests, self.max_seconds = max_requests, max_seconds
        self.context_budget, self.output_tokens, self.shell_timeout = context_budget, output_tokens, shell_timeout
        self.compaction_mode = compaction_mode
        self.compaction_policy = CompactionPolicy(
            mode=compaction_mode,
            recent_tool_groups=1,
            micro_min_chars=256,
            output_reserve=self.output_tokens,
        )
        self.arm = artifact_projection
        self.orchestration_mode = orchestration_mode
        self.max_rounds = max_rounds
        self.observation_mode = observation_mode
        self.observation_required = bool(observation_required)
        self.observation_base_url = observation_base_url
        self.observation_public_key_env = observation_public_key_env
        self.observation_secret_key_env = observation_secret_key_env
        self.observation_close_timeout = observation_close_timeout
        self.provider_timeout = provider_timeout
        default_arm = {
            ("baseline", "off"): "baseline",
            ("layered", "on"): "enhanced",
            ("layered", "off"): "loo_artifact",
        }.get((self.compaction_mode, self.arm), f"{self.compaction_mode}-{self.arm}")
        if experiment_arm is None:
            self.experiment_arm = default_arm
        elif (
            not isinstance(experiment_arm, str)
            or not experiment_arm
            or len(experiment_arm) > 64
            or any(char in experiment_arm for char in "/\\\r\n\t")
        ):
            raise ValueError("experiment_arm is invalid")
        else:
            self.experiment_arm = experiment_arm
        self.task_id = self.task_dir.name

    async def setup(self, environment):
        # Official task/environment preparation is Harbor's responsibility.
        pass

    async def run(self, instruction, environment, context):
        if instruction != agent_instruction(self.task_dir):
            raise ValueError("Harbor instruction differs from frozen official input")
        if any(row["run_id"] == self.run_id for row in self.ledger.snapshot()["requests"]):
            raise BudgetStop("frozen run already consumed; retries are forbidden")
        root = self.logs_dir
        root.mkdir(parents=True, exist_ok=True)
        run_id = self.run_id
        status = "started"
        terminal = {}
        mea_payload = None
        failure = None
        owner = None
        agent = None
        observation = {
            "mode": self.observation_mode,
            "required": self.observation_required,
            "campaign_id": self.campaign_id,
            "base_url": self.observation_base_url,
            "arm": self.experiment_arm,
            "stage": "final",
            "task_id": self.task_id,
            "flush_complete": None,
            "close": None,
            "error_type": None,
        }
        exception_metadata = None
        schema_entries: list[dict] = []

        try:
            raw = create_live_llm(
                max_output_tokens=self.output_tokens,
                timeout=self.provider_timeout,
            )
            guarded = GuardedLLM(raw, self.ledger, run_id)
            if self.observation_mode == "langfuse":
                owner = ExperimentLLMOwner.from_environment(
                    raw,
                    campaign_id=self.campaign_id,
                    enabled=True,
                    required=self.observation_required,
                    public_key_env=self.observation_public_key_env,
                    secret_key_env=self.observation_secret_key_env,
                    base_url=self.observation_base_url,
                    close_timeout=self.observation_close_timeout,
                )
                llm = ObservedLLM(
                    guarded,
                    owner,
                    run_id=run_id,
                    arm=self.experiment_arm,
                    task_id=self.task_id,
                )
            else:
                llm = guarded
            deadline = asyncio.get_running_loop().time() + self.max_seconds
            role_log = root / "quality_roles.jsonl"

            def remaining_seconds() -> float:
                return min(
                    self.ledger.remaining_time(run_id),
                    deadline - asyncio.get_running_loop().time(),
                )

            def remaining_requests() -> int:
                used = sum(
                    1
                    for row in self.ledger.snapshot()["requests"]
                    if row["run_id"] == run_id
                )
                return self.max_requests - used

            @tool(is_parallel_safe=False)
            async def shell(command: str) -> str:
                """Execute a shell command in the official container, bounded by remaining run and shell deadlines."""
                remaining = remaining_seconds()
                if remaining <= 0:
                    raise BudgetStop("agent total time cap reached")
                result = await environment.exec(command=command, timeout_sec=min(self.shell_timeout, remaining))
                return json.dumps(
                    {"exit_code": result.return_code, "stdout": result.stdout, "stderr": result.stderr},
                    ensure_ascii=False,
                )

            @tool(is_parallel_safe=False)
            async def audit_shell(command: str) -> str:
                """Run one read-only audit command from the frozen allowlist."""
                allowed, detail = _safe_auditor_command(command)
                if not allowed:
                    return json.dumps(
                        {
                            "exit_code": 126,
                            "stdout": "",
                            "stderr": detail,
                            "allowed_examples": [
                                "pwd",
                                "ls -la",
                                "cat FILE",
                                "grep PATTERN FILE",
                                "find . -maxdepth 2 -type f",
                                "git status --short",
                                "git diff",
                                "git log --oneline -5",
                                "git branch -a",
                                "git reflog",
                                "git show HEAD",
                                "git rev-parse HEAD",
                                "python -m pytest -q",
                                "uv run pytest -q",
                            ],
                        },
                        ensure_ascii=False,
                    )
                remaining = remaining_seconds()
                if remaining <= 0:
                    raise BudgetStop("agent total time cap reached")
                executable = detail
                try:
                    tokens = shlex.split(detail, posix=True)
                except ValueError:
                    tokens = []
                if tokens and (
                    tokens[0] in {"python", "python3"}
                    or tokens[:2] == ["uv", "run"]
                ):
                    executable = "PYTHONDONTWRITEBYTECODE=1 " + detail
                result = await environment.exec(
                    command=executable,
                    timeout_sec=min(self.shell_timeout, remaining),
                )
                return json.dumps(
                    {
                        "exit_code": result.return_code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    ensure_ascii=False,
                )

            store = ArtifactStore(root / "artifacts")
            artifact_tools = make_artifact_tools(store)

            def build_agent(
                *,
                system_prompt: str,
                tools: list,
                session_name: str,
                schema_role: str,
                include_artifacts: bool,
                max_turns: int | None = None,
            ) -> Agent:
                session_path = root / "quality-sessions" / session_name
                session_path.parent.mkdir(parents=True, exist_ok=True)
                remaining = max(1.0, remaining_seconds())
                built = Agent(
                    llm=llm,
                    tools=tools,
                    session=Session(path=session_path),
                    system_prompt=system_prompt,
                    context_budget=self.context_budget,
                    artifact_store=store
                    if include_artifacts and self.arm == "on"
                    else None,
                    compaction_policy=self.compaction_policy,
                    memory_dir=False,
                    task_store=False,
                    skill_dirs=[],
                    subagent_dirs=[],
                    extension_dirs=[],
                    plugin_dirs=[],
                    retry_policy=AutoRetryPolicy(max_retries=0),
                    run_limits=RunLimits(
                        max_turns=max_turns or self.max_requests,
                        max_seconds=remaining,
                    ),
                )
                schema_entries.append(
                    {
                        "role": schema_role,
                        "tools": built.registry.get_schemas(),
                    }
                )
                return built

            if self.orchestration_mode == "react":
                agent = build_agent(
                    system_prompt=SINGLE_AGENT_SYSTEM_PROMPT,
                    tools=[shell, *artifact_tools],
                    session_name="session.jsonl",
                    schema_role="react",
                    include_artifacts=True,
                )
                final_text, terminal = await _run_agent_and_capture(
                    agent,
                    instruction,
                    max(1.0, remaining_seconds()),
                )
                _append_jsonl(
                    role_log,
                    {
                        "purpose": "react_final",
                        "output": final_text,
                        "terminal": terminal,
                    },
                )
                status = terminal.get("stop_reason") or "missing_terminal_event"
            else:
                max_rounds = self.max_rounds

                class Callbacks:
                    async def manager(self, request: RoleRequest) -> str:
                        return await _request_manager_json(
                            llm=llm,
                            request=request,
                            remaining_timeout=lambda: max(1.0, remaining_seconds()),
                            role_log=role_log,
                        )

                    async def executor(self, request: RoleRequest) -> str:
                        system, user = request.messages
                        available = remaining_requests()
                        suffix = request.purpose.rsplit("_r", 1)[-1]
                        round_index = (
                            int(suffix) if suffix.isdigit() else max_rounds
                        )
                        executor_turns = _executor_turn_budget(
                            available=available,
                            round_index=round_index,
                            max_rounds=max_rounds,
                        )
                        if executor_turns < 1:
                            raise RuntimeError(
                                "executor has no budget after reserving manager/auditor"
                            )
                        local = build_agent(
                            system_prompt=system.content,
                            tools=[shell, *artifact_tools],
                            session_name=request.purpose + ".jsonl",
                            schema_role="executor",
                            include_artifacts=True,
                            max_turns=executor_turns,
                        )
                        try:
                            output, role_terminal = await _run_agent_and_capture(
                                local,
                                user.content,
                                max(1.0, remaining_seconds()),
                            )
                        finally:
                            await local.aclose()
                        _append_jsonl(
                            role_log,
                            {
                                "purpose": request.purpose,
                                "output": output,
                                "terminal": role_terminal,
                            },
                        )
                        return output

                    async def auditor(self, request: RoleRequest) -> str:
                        system, user = request.messages
                        available = remaining_requests()
                        auditor_budget = min(
                            _MEA_AUDITOR_MAX_REQUESTS,
                            available - 2,
                        )
                        if auditor_budget < 1:
                            fallback = _fallback_audit_report(
                                validation_error="auditor has no request budget"
                            )
                            _append_jsonl(
                                role_log,
                                {
                                    "purpose": request.purpose,
                                    "attempt": 3,
                                    "mode": "local_blocked_fallback",
                                    "output": fallback,
                                    "validation_error": "auditor has no request budget",
                                },
                            )
                            return fallback
                        turns_per_attempt = max(1, auditor_budget // 2)
                        last_failure = "auditor response invalid"
                        last_output = None
                        best_transcript = ""
                        for attempt in (1, 2):
                            current_available = remaining_requests()
                            auditor_turns = min(
                                turns_per_attempt,
                                current_available - 2,
                            )
                            if auditor_turns < 1:
                                break
                            prompt = user.content
                            if attempt == 2:
                                prompt = (
                                    prompt
                                    + "\n\nCorrection: your previous Auditor output "
                                    + "failed validation: "
                                    + last_failure
                                    + ". Re-run only the necessary read-only checks "
                                    + "and return one compact JSON object. For "
                                    + "status=complete, remaining must be []; put "
                                    + "nonblocking observations in summary."
                                )
                            local = build_agent(
                                system_prompt=system.content,
                                tools=[audit_shell],
                                session_name=(
                                    f"{request.purpose}_attempt{attempt}.jsonl"
                                ),
                                schema_role="auditor",
                                include_artifacts=False,
                                max_turns=auditor_turns,
                            )
                            run_failure = None
                            cancelled_error = None
                            try:
                                try:
                                    output, role_terminal = await _run_agent_and_capture(
                                        local,
                                        prompt,
                                        max(1.0, remaining_seconds()),
                                    )
                                except asyncio.CancelledError as exc:
                                    cancelled_error = exc
                                    output = None
                                    role_terminal = None
                                except Exception as exc:
                                    run_failure = f"{type(exc).__name__}: {exc}"
                                    output = None
                                    role_terminal = None
                            finally:
                                try:
                                    transcript = _render_audit_transcript(
                                        local.session.get_full_history_messages()
                                    )
                                    if len(transcript) >= len(best_transcript):
                                        best_transcript = transcript
                                except Exception:
                                    pass
                                finally:
                                    await local.aclose()
                            if cancelled_error is not None:
                                raise cancelled_error
                            if output is not None:
                                last_output = output
                            if run_failure is not None:
                                last_failure = run_failure
                                _append_jsonl(
                                    role_log,
                                    {
                                        "purpose": request.purpose,
                                        "attempt": attempt,
                                        "output": output,
                                        "terminal": role_terminal,
                                        "validation_error": last_failure,
                                    },
                                )
                                if attempt == 2:
                                    break
                                continue
                            try:
                                parse_audit_report(output)
                            except SchemaError as exc:
                                last_failure = str(exc)
                                valid = False
                            else:
                                valid = True
                            _append_jsonl(
                                role_log,
                                {
                                    "purpose": request.purpose,
                                    "attempt": attempt,
                                    "output": output,
                                    "terminal": role_terminal,
                                    "validation_error": (
                                        None if valid else last_failure
                                    ),
                                },
                            )
                            if valid:
                                return output
                        if remaining_requests() >= 2:
                            try:
                                projected = await _request_auditor_projection(
                                    llm=llm,
                                    request=request,
                                    transcript=best_transcript,
                                    last_output=last_output,
                                    validation_error=last_failure,
                                    remaining_timeout=lambda: max(
                                        1.0, remaining_seconds()
                                    ),
                                    role_log=role_log,
                                )
                            except BudgetStop:
                                raise
                            except Exception as exc:
                                last_failure = (
                                    f"{type(exc).__name__}: {exc}"
                                )
                            else:
                                return projected
                        fallback = _fallback_audit_report(
                            validation_error=last_failure
                        )
                        _append_jsonl(
                            role_log,
                            {
                                "purpose": request.purpose,
                                "attempt": 3,
                                "mode": "local_blocked_fallback",
                                "output": fallback,
                                "validation_error": last_failure,
                            },
                        )
                        return fallback

                outcome = await run_mea(
                    task=instruction,
                    callbacks=Callbacks(),
                    max_rounds=self.max_rounds,
                )
                mea_payload = outcome.to_dict()
                status = outcome.status
        except asyncio.CancelledError as exc:
            status = "cancelled"
            exception_metadata = _safe_exception(exc)
            failure = exc
        except Exception as exc:
            status = type(exc).__name__  # Never persist provider exception bodies.
            exception_metadata = _safe_exception(exc)
            failure = RuntimeError("budgeted agent stopped; inspect sanitized live_run.json")
        finally:
            cleanup_errors = []
            if agent is not None:
                try:
                    await agent.aclose()
                except BaseException as exc:
                    cleanup_errors.append(_safe_exception(exc))
            if owner is not None:
                try:
                    observation["flush_complete"] = await owner.aflush_and_check()
                except BaseException as exc:
                    observation["flush_complete"] = False
                    observation["error_type"] = type(exc).__name__
                    observation["error"] = _safe_exception(exc)
                try:
                    observation["close"] = await owner.aclose()
                except BaseException as exc:
                    observation["error_type"] = type(exc).__name__
                    observation["error"] = _safe_exception(exc)
            rows = [r for r in self.ledger.snapshot()["requests"] if r["run_id"] == run_id]
            known = bool(rows) and all(r["cost_micro_usd"] is not None for r in rows)
            context.cost_usd = sum(r["cost_micro_usd"] for r in rows) / 1_000_000 if known else None
            context.n_input_tokens = (
                sum(
                    r["usage"]["prompt_tokens"]
                    + r["usage"].get("cache_read_tokens", 0)
                    + r["usage"].get("cache_write_tokens", 0)
                    for r in rows
                )
                if known
                else None
            )
            context.n_output_tokens = sum(r["usage"]["completion_tokens"] for r in rows) if known else None
            cache_complete = known and all("cache_read_tokens" in r["usage"] for r in rows)
            context.n_cache_tokens = sum(r["usage"]["cache_read_tokens"] for r in rows) if cache_complete else None
            tool_schema_sha256 = hashlib.sha256(
                json.dumps(schema_entries, sort_keys=True).encode()
            ).hexdigest()
            cache_available = (
                known
                and all(
                    "prompt_tokens" in r["usage"]
                    and "cache_read_tokens" in r["usage"]
                    for r in rows
                )
            )
            cache_write_available = cache_available and all(
                "cache_write_tokens" in r["usage"] for r in rows
            )
            prompt_tokens = (
                sum(r["usage"]["prompt_tokens"] for r in rows)
                if cache_available
                else None
            )
            cache_read_tokens = (
                sum(r["usage"]["cache_read_tokens"] for r in rows)
                if cache_available
                else None
            )
            cache_write_tokens = (
                sum(r["usage"]["cache_write_tokens"] for r in rows)
                if cache_write_available
                else None
            )
            cache_denominator = (
                (prompt_tokens or 0)
                + (cache_read_tokens or 0)
                + (cache_write_tokens or 0)
                if cache_available
                else None
            )
            cache_usage = {
                "prompt_tokens": prompt_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cache_write_available": cache_write_available,
                "cache_hit_rate": (
                    (cache_read_tokens or 0) / cache_denominator
                    if cache_denominator
                    else None
                ),
            }
            if self.orchestration_mode == "mea":
                role_prompt_hashes = {
                    "manager": hashlib.sha256(MANAGER_SYSTEM_PROMPT.encode()).hexdigest(),
                    "executor": hashlib.sha256(EXECUTOR_SYSTEM_PROMPT.encode()).hexdigest(),
                    "auditor": hashlib.sha256(AUDITOR_SYSTEM_PROMPT.encode()).hexdigest(),
                }
            else:
                role_prompt_hashes = {
                    "react": hashlib.sha256(
                        SINGLE_AGENT_SYSTEM_PROMPT.encode()
                    ).hexdigest()
                }
            limits = {
                "max_requests": self.max_requests,
                "max_seconds": self.max_seconds,
                "context_budget": self.context_budget,
                "output_tokens": self.output_tokens,
                "shell_timeout": self.shell_timeout,
                "compaction_mode": self.compaction_mode,
                "compaction_profile": self.compaction_policy.capability_profile(),
                "orchestration_mode": self.orchestration_mode,
                "max_rounds": self.max_rounds,
                "mea_executor_reserved_requests": _MEA_EXECUTOR_RESERVED_REQUESTS,
                "mea_executor_round_caps": _MEA_EXECUTOR_ROUND_CAPS,
                "mea_auditor_max_requests": _MEA_AUDITOR_MAX_REQUESTS,
                "observation_mode": self.observation_mode,
                "observation_required": self.observation_required,
            }
            identity = {
                "artifact_projection": self.arm,
                "compaction_mode": self.compaction_mode,
                "orchestration_mode": self.orchestration_mode,
                "role_prompt_hashes": role_prompt_hashes,
                "observation_mode": self.observation_mode,
                "observation_required": self.observation_required,
                "limits": limits,
                "tool_schema_sha256": tool_schema_sha256,
            }
            close_state = observation["close"] or {}
            observation_ok = (
                self.observation_mode == "off"
                or (
                    observation["flush_complete"] is True
                    and close_state.get("runtime_close_complete") is True
                    and close_state.get("export_errors") == 0
                    and close_state.get("close_errors") == 0
                    and close_state.get("dropped") == 0
                    and close_state.get("runtime_alive_after_close") is False
                    and close_state.get("llm_clients_closed") is True
                )
            )
            observation["ready"] = observation_ok
            if cleanup_errors:
                observation["agent_cleanup_errors"] = cleanup_errors
                status = "cleanup_failed"
                failure = failure or RuntimeError("agent cleanup failed")
            if not observation_ok:
                status = "observation_failed"
                if self.observation_required:
                    failure = failure or ObservationRequiredError("required Langfuse observation failed")
            context.metadata = {
                "run_id": run_id,
                "adapter_protocol": "v2",
                "arm": self.experiment_arm,
                "artifact_projection": self.arm,
                "compaction_mode": self.compaction_mode,
                "orchestration_mode": self.orchestration_mode,
                "role_prompt_hashes": role_prompt_hashes,
                "status": status,
                "requests": len(rows),
                "usage_available": known,
                "cache_usage": cache_usage,
                "mea": mea_payload,
                **terminal,
                "oracle_gate": self.gate,
                "model": LIVE_MODEL,
                "observation_mode": self.observation_mode,
                "observation_required": self.observation_required,
                "observation": observation,
                "observation_backend": "langfuse_metadata_only" if self.observation_mode == "langfuse" else "local_only",
                "langfuse_remote_readback": False,
                "exception": exception_metadata,
                "limits": limits,
                "schema_sha256": tool_schema_sha256,
                "tool_schema_sha256": tool_schema_sha256,
                "experiment_identity_sha256": hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode()
                ).hexdigest(),
            }
            (root / "live_run.json").write_text(json.dumps(context.metadata, indent=2), encoding="utf-8")
            if exception_metadata is not None or observation.get("error") is not None:
                exception_text = json.dumps(
                    {"agent": exception_metadata, "observation": observation.get("error")},
                    indent=2,
                    sort_keys=True,
                )
                (root / "exception-metadata.json").write_text(exception_text + "\n", encoding="utf-8")
        if failure is not None:
            raise failure
