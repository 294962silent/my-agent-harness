"""Host-side Harbor adapter. No host filesystem or verifier tools reach the model."""

from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.models.task.task import Task

from my_agent_core.agent import Agent
from my_agent_core.events import AgentEnd
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, BudgetStop, GuardedLLM, create_live_llm, LIVE_MODEL
from .tasks import oracle_gate, agent_instruction, runtime_constraints


class BudgetedHarborAgent(BaseAgent):
    """Context projection on/off; identical model, instruction, shell and limits."""

    @staticmethod
    def name():
        return "budgeted-my-agent-harness"

    def version(self):
        return "2"

    def __init__(self, *, task_dir, logs_dir, oracle_result, ledger_path, artifact_projection="on",
                 campaign_id=None, run_id=None, max_requests=16, max_seconds=600,
                 context_budget=32000, output_tokens=4096, shell_timeout=60,
                 spend_ceiling_usd=None, **kwargs):
        super().__init__(logs_dir=Path(logs_dir), **kwargs)
        if artifact_projection not in ("on", "off"):
            raise ValueError("artifact_projection must be on/off")
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
        self.run_id = run_id or uuid4().hex
        if campaign_id is not None:
            self.ledger.register_run(self.run_id, max_requests=max_requests, time_cap=max_seconds)
        self.max_requests, self.max_seconds = max_requests, max_seconds
        self.context_budget, self.output_tokens, self.shell_timeout = context_budget, output_tokens, shell_timeout
        self.arm = artifact_projection

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
        raw = create_live_llm(max_output_tokens=self.output_tokens, timeout=120)
        guarded = GuardedLLM(raw, self.ledger, run_id)
        deadline = asyncio.get_running_loop().time() + self.max_seconds

        @tool(is_parallel_safe=False)
        async def shell(command: str) -> str:
            """Execute a shell command in the official container, bounded by remaining run and shell deadlines."""
            remaining = min(self.ledger.remaining_time(run_id), deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                raise BudgetStop("agent total time cap reached")
            result = await environment.exec(command=command, timeout_sec=min(self.shell_timeout, remaining))
            return json.dumps(
                {"exit_code": result.return_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False
            )

        store = ArtifactStore(root / "artifacts")
        agent = Agent(
            llm=guarded,
            tools=[shell, *make_artifact_tools(store)],
            session=Session(path=root / "session.jsonl"),
            system_prompt="Complete the user task in the container using shell. Do not access benchmark solutions, hidden verifier tests, or scoring files. Finish with a concise description of your changes.",
            context_budget=self.context_budget,
            artifact_store=store if self.arm == "on" else None,
            memory_dir=False,
            task_store=False,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            retry_policy=AutoRetryPolicy(max_retries=0),
            run_limits=RunLimits(max_turns=self.max_requests, max_seconds=self.max_seconds),
        )
        status = "started"
        terminal = {}

        def record_end(event):
            if isinstance(event, AgentEnd):
                terminal.update(stop_reason=event.stop_reason, limit_details=event.limit_details)

        agent.subscribe(record_end)
        try:
            await asyncio.wait_for(agent.run(instruction), timeout=self.max_seconds)
            status = terminal.get("stop_reason") or "missing_terminal_event"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = type(exc).__name__  # Never persist provider exception bodies.
            raise RuntimeError("budgeted agent stopped; inspect sanitized live_run.json") from None
        finally:
            await agent.aclose()
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
            context.metadata = {
                "run_id": run_id,
                "adapter_protocol": "v2",
                "arm": self.arm,
                "status": status,
                "requests": len(rows),
                "usage_available": known,
                **terminal,
                "oracle_gate": self.gate,
                "model": LIVE_MODEL,
                "limits": {"max_requests": self.max_requests, "max_seconds": self.max_seconds,
                           "context_budget": self.context_budget, "output_tokens": self.output_tokens,
                           "shell_timeout": self.shell_timeout},
                "schema_sha256": hashlib.sha256(json.dumps(agent.registry.get_schemas(), sort_keys=True).encode()).hexdigest(),
            }
            (root / "live_run.json").write_text(json.dumps(context.metadata, indent=2), encoding="utf-8")
