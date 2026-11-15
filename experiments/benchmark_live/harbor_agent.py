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
import traceback
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.models.task.task import Task

from my_agent_core.agent import Agent
from my_agent_core.events import AgentEnd
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.context_compaction import CompactionPolicy
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, BudgetStop, GuardedLLM, create_live_llm, LIVE_MODEL
from .observation import ExperimentLLMOwner, ObservationRequiredError
from .tasks import oracle_gate, agent_instruction, runtime_constraints


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
    """Context projection and compaction arms with otherwise shared limits."""

    @staticmethod
    def name():
        return "budgeted-my-agent-harness"

    def version(self):
        return "2"

    def __init__(self, *, task_dir, logs_dir, oracle_result, ledger_path, artifact_projection="on",
                 compaction_mode="baseline",
                 observation_mode="off", observation_required=False,
                 observation_base_url="https://cloud.langfuse.com",
                 observation_public_key_env="LANGFUSE_PUBLIC_KEY",
                 observation_secret_key_env="LANGFUSE_SECRET_KEY",
                 observation_close_timeout=15,
                 campaign_id=None, run_id=None, max_requests=16, max_seconds=600,
                 context_budget=32000, output_tokens=4096, shell_timeout=60,
                 spend_ceiling_usd=None, **kwargs):
        super().__init__(logs_dir=Path(logs_dir), **kwargs)
        if artifact_projection not in ("on", "off"):
            raise ValueError("artifact_projection must be on/off")
        if compaction_mode not in ("baseline", "layered"):
            raise ValueError("compaction_mode must be baseline/layered")
        if observation_mode not in ("off", "langfuse"):
            raise ValueError("observation_mode must be off/langfuse")
        if observation_required and observation_mode != "langfuse":
            raise ValueError("required observation needs observation_mode=langfuse")
        if observation_mode == "langfuse" and campaign_id is None:
            raise ValueError("langfuse observation requires a frozen campaign_id")
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
        self.arm = artifact_projection
        self.compaction_mode = compaction_mode
        self.observation_mode = observation_mode
        self.observation_required = bool(observation_required)
        self.observation_base_url = observation_base_url
        self.observation_public_key_env = observation_public_key_env
        self.observation_secret_key_env = observation_secret_key_env
        self.observation_close_timeout = observation_close_timeout
        self.experiment_arm = {
            ("baseline", "off"): "baseline",
            ("layered", "on"): "enhanced",
            ("layered", "off"): "loo_artifact",
        }.get((self.compaction_mode, self.arm), f"{self.compaction_mode}-{self.arm}")
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

        try:
            raw = create_live_llm(max_output_tokens=self.output_tokens, timeout=120)
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

            @tool(is_parallel_safe=False)
            async def shell(command: str) -> str:
                """Execute a shell command in the official container, bounded by remaining run and shell deadlines."""
                remaining = min(self.ledger.remaining_time(run_id), deadline - asyncio.get_running_loop().time())
                if remaining <= 0:
                    raise BudgetStop("agent total time cap reached")
                result = await environment.exec(command=command, timeout_sec=min(self.shell_timeout, remaining))
                return json.dumps(
                    {"exit_code": result.return_code, "stdout": result.stdout, "stderr": result.stderr},
                    ensure_ascii=False,
                )

            store = ArtifactStore(root / "artifacts")
            agent = Agent(
                llm=llm,
                tools=[shell, *make_artifact_tools(store)],
                session=Session(path=root / "session.jsonl"),
                system_prompt="Complete the user task in the container using shell. Do not access benchmark solutions, hidden verifier tests, or scoring files. Finish with a concise description of your changes.",
                context_budget=self.context_budget,
                artifact_store=store if self.arm == "on" else None,
                compaction_policy=CompactionPolicy(mode=self.compaction_mode),
                memory_dir=False,
                task_store=False,
                skill_dirs=[],
                subagent_dirs=[],
                extension_dirs=[],
                plugin_dirs=[],
                retry_policy=AutoRetryPolicy(max_retries=0),
                run_limits=RunLimits(max_turns=self.max_requests, max_seconds=self.max_seconds),
            )

            def record_end(event):
                if isinstance(event, AgentEnd):
                    terminal.update(stop_reason=event.stop_reason, limit_details=event.limit_details)

            agent.subscribe(record_end)
            await asyncio.wait_for(agent.run(instruction), timeout=self.max_seconds)
            status = terminal.get("stop_reason") or "missing_terminal_event"
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
            schemas = agent.registry.get_schemas() if agent is not None else []
            tool_schema_sha256 = hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()
            limits = {
                "max_requests": self.max_requests,
                "max_seconds": self.max_seconds,
                "context_budget": self.context_budget,
                "output_tokens": self.output_tokens,
                "shell_timeout": self.shell_timeout,
                "compaction_mode": self.compaction_mode,
                "observation_mode": self.observation_mode,
                "observation_required": self.observation_required,
            }
            identity = {
                "artifact_projection": self.arm,
                "compaction_mode": self.compaction_mode,
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
                "status": status,
                "requests": len(rows),
                "usage_available": known,
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
