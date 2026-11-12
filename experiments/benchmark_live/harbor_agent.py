"""Host-side Harbor adapter. No host filesystem or verifier tools reach the model."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.models.task.task import Task

from my_agent_core.agent import Agent
from my_agent_core.events import AgentEnd
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_agent_llm import Config, LLM
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, GuardedLLM
from .tasks import oracle_gate


class BudgetedHarborAgent(BaseAgent):
    """Context projection on/off; identical model, instruction, shell and limits."""

    @staticmethod
    def name():
        return "budgeted-my-agent-harness"

    def version(self):
        return "2"

    def __init__(self, *, task_dir, logs_dir, oracle_result, ledger_path, artifact_projection="on", **kwargs):
        super().__init__(logs_dir=Path(logs_dir), **kwargs)
        if artifact_projection not in ("on", "off"):
            raise ValueError("artifact_projection must be on/off")
        self.task_dir = Path(task_dir)
        self.gate = oracle_gate(self.task_dir, oracle_result)
        if Task(self.task_dir).checksum != self.gate["harbor_task_checksum"]:
            raise ValueError("oracle checksum differs from current official task")
        self.ledger = BudgetLedger(Path(ledger_path))
        self.arm = artifact_projection

    async def setup(self, environment):
        # Official task/environment preparation is Harbor's responsibility.
        pass

    async def run(self, instruction, environment, context):
        key = os.environ.get("LIVE_BENCH_API_KEY")
        if not key:
            raise ValueError("LIVE_BENCH_API_KEY must be supplied to the host process")
        root = self.logs_dir
        root.mkdir(parents=True, exist_ok=True)
        run_id = uuid4().hex
        # Fixed endpoint/model: no .env loading or default provider fallback.
        raw = LLM(
            Config(
                provider="openai",
                model="deepseek-v4.1-flash",
                api_key=key,
                base_url="https://opencode.ai/inference/go/openai/v1",
                max_retries=0,
                timeout=120,
                max_tokens=4096,
            )
        )
        guarded = GuardedLLM(raw, self.ledger, run_id)

        @tool(is_parallel_safe=False)
        async def shell(command: str) -> str:
            """Execute a shell command inside the official task container, timeout 60s."""
            result = await environment.exec(command=command, timeout_sec=60)
            return json.dumps(
                {"exit_code": result.return_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False
            )

        store = ArtifactStore(root / "artifacts")
        agent = Agent(
            llm=guarded,
            tools=[shell, *make_artifact_tools(store)],
            session=Session(path=root / "session.jsonl"),
            system_prompt="Complete the user task in the container using shell. Do not access benchmark solutions, hidden verifier tests, or scoring files. Finish with a concise description of your changes.",
            context_budget=32000,
            artifact_store=store if self.arm == "on" else None,
            memory_dir=False,
            task_store=False,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            run_limits=RunLimits(max_turns=16, max_seconds=600),
        )
        status = "started"
        terminal = {}

        def record_end(event):
            if isinstance(event, AgentEnd):
                terminal.update(stop_reason=event.stop_reason, limit_details=event.limit_details)

        agent.subscribe(record_end)
        try:
            await asyncio.wait_for(agent.run(instruction), timeout=600)
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
            context.n_cache_tokens = sum(r["usage"].get("cache_read_tokens", 0) for r in rows) if known else None
            context.metadata = {
                "run_id": run_id,
                "adapter_protocol": "v2",
                "arm": self.arm,
                "status": status,
                "requests": len(rows),
                "usage_available": known,
                **terminal,
                "oracle_gate": self.gate,
            }
            (root / "live_run.json").write_text(json.dumps(context.metadata, indent=2), encoding="utf-8")
