"""Two explicitly pinned SWE tasks. Scoring is a separate official run."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from my_agent_core.agent import Agent
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.events import AgentEnd
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_agent_llm import Config, LLM
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, GuardedLLM

INSTANCE = "psf__requests-1963"
BASE = "110048f9837f8441ea536804115e80b69f400277"
IMAGE = "swebench/sweb.eval.x86_64.psf_1776_requests-1963:latest"
IMAGE_ID = "sha256:287f118ccaa9c0ba39e5f361550bd845d28933f34483861b8deaa4992f9d8d92"
PINNED = {INSTANCE: {"base_commit": BASE, "image": IMAGE, "image_id": IMAGE_ID}}
PINNED["sympy__sympy-20590"] = {
    "base_commit": "cffd4e0f86fefd4802349a9f9b19ed70934ea354",
    "image": "swebench/sweb.eval.x86_64.sympy_1776_sympy-20590:latest",
    "image_id": "sha256:fcfe2aec281d3a1af6a71bedef4637a446bcb076e0acdec6fec03b9b25a346f2",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def gated_task(metadata, evidence):
    """Read only the explicitly projected safe metadata, never evaluator JSON."""
    task = json.loads(Path(metadata).read_text(encoding="utf-8"))
    if isinstance(task, list) and len(task) == 1:
        task = task[0]
    if not isinstance(task, dict) or set(task) - {
        "instance_id",
        "problem_statement",
        "repo",
        "base_commit",
        "image",
        "version",
        "environment_setup_commit",
    }:
        raise ValueError("solver requires safe projected task metadata only")
    gate = json.loads(Path(evidence).read_text(encoding="utf-8"))
    instance = task.get("instance_id")
    pin = PINNED.get(instance)
    if (
        pin is None
        or task.get("base_commit") != pin["base_commit"]
        or task.get("image") != pin["image"]
        or not isinstance(task.get("problem_statement"), str)
        or not task["problem_statement"].strip()
    ):
        raise ValueError("only explicitly pinned official tasks are supported")
    if any(
        gate.get(k) != v
        for k, v in {
            "instance_id": instance,
            **pin,
            "metadata_sha256": sha(metadata),
            "resolved": True,
        }.items()
    ):
        raise ValueError("gold evidence does not bind the exact safe task and image")
    report = Path(gate.get("official_report_path") or gate["official_report"])
    if sha(report) != gate.get("official_report_sha256"):
        raise ValueError("gold official report checksum differs")
    result = json.loads(report.read_text(encoding="utf-8"))
    if (
        instance not in result.get("resolved_ids", [])
        or result.get("completed_instances") != 1
        or result.get("resolved_instances") != 1
        or result.get("error_instances") != 0
    ):
        raise ValueError("official gold preflight did not resolve this task cleanly")
    return task, gate


class SetupError(RuntimeError):
    def __init__(self, error_code):
        self.error_code = error_code
        super().__init__(error_code)


class DockerTask:
    """Own one new isolated container; subprocess argv never uses a host shell."""

    def __init__(self, instance_id=INSTANCE):
        if instance_id not in PINNED:
            raise ValueError("task is outside pinned allowlist")
        self.pin = PINNED[instance_id]
        self.name = "harness-swe-" + uuid4().hex
        self.created = False
        self.stage = "image_inspect"
        self.initial_head = None

    async def call(self, *args, timeout=90):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            proc.kill()
            await proc.communicate()
            raise
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    async def start(self):
        self.stage = "image_inspect"
        code, out, _ = await self.call("image", "inspect", "--format", "{{.Id}}", self.pin["image"])
        if code or out.strip() != self.pin["image_id"]:
            raise SetupError("image_id_mismatch")
        self.stage = "container_create"
        self.created = True  # Claim cleanup before awaiting a possibly interrupted Docker create.
        code, _, _ = await self.call(
            "create",
            "--pull",
            "never",
            "--network",
            "none",
            "--name",
            self.name,
            "--entrypoint",
            "/bin/bash",
            self.pin["image"],
            "-lc",
            "while true; do sleep 3600; done",
        )
        if code:
            raise SetupError("container_create_failed")
        self.stage = "container_start"
        code, _, _ = await self.call("start", self.name)
        if code:
            raise SetupError("container_start_failed")
        self.stage = "base_inspect"
        initial = await self.exec("git rev-parse HEAD")
        if initial["exit_code"]:
            raise SetupError("initial_head_unavailable")
        head = initial["stdout"].strip()
        self.initial_head = head if len(head) == 40 and all(c in "0123456789abcdef" for c in head) else None
        self.stage = "base_reset"
        reset = await self.exec("git reset --hard " + self.pin["base_commit"])
        if reset["exit_code"]:
            raise SetupError("pinned_base_reset_failed")
        self.stage = "base_verify"
        result = await self.exec("git rev-parse HEAD && git status --porcelain")
        if result["exit_code"] or result["stdout"].strip() != self.pin["base_commit"]:
            raise SetupError("pinned_base_not_clean")
        self.stage = "ready"

    async def exec(self, command):
        code, out, err = await self.call(
            "exec",
            "-w",
            "/testbed",
            self.name,
            "timeout",
            "--signal=TERM",
            "--kill-after=5",
            "55",
            "/bin/bash",
            "-lc",
            command,
            timeout=65,
        )
        return {"exit_code": code, "stdout": out, "stderr": err}

    async def patch(self):
        result = await self.exec("git add --intent-to-add . && git diff --binary " + self.pin["base_commit"])
        if result["exit_code"]:
            raise RuntimeError("model diff extraction failed")
        return result["stdout"]

    async def close(self):
        if self.created:
            code, _, err = await self.call("rm", "-f", self.name)
            if code and "No such container" not in err:
                raise RuntimeError("owned container cleanup failed")


async def probe_environment(metadata, gold_evidence, output):
    """Initialize the real pinned container without credentials, ledger or LLM."""
    task, _ = gated_task(metadata, gold_evidence)
    path = Path(output)
    if path.exists():
        raise ValueError("probe output must be a new file")
    env = DockerTask(task["instance_id"])
    result = {"instance_id": task["instance_id"], "model_requests": 0, "passed": False}
    try:
        await env.start()
        result["passed"] = True
    except Exception as exc:
        result["error_code"] = getattr(exc, "error_code", "unclassified_" + type(exc).__name__)
    finally:
        result.update(stage=env.stage, initial_image_head=env.initial_head, verified_base=env.pin["base_commit"])
        try:
            await env.close()
            result["cleanup"] = "completed"
        except Exception as exc:
            result["cleanup"] = type(exc).__name__
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
    return result


async def solve(*, metadata, gold_evidence, ledger_path, arm, output, environment=None, llm_factory=LLM):
    if arm not in ("on", "off"):
        raise ValueError("arm must be on/off")
    task, _ = gated_task(metadata, gold_evidence)
    key = os.environ.get("LIVE_BENCH_API_KEY")
    if not key:
        raise ValueError("LIVE_BENCH_API_KEY is required in host process environment")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    ledger = BudgetLedger(ledger_path)
    env = environment if environment is not None else DockerTask(task["instance_id"])
    run_id = uuid4().hex
    agent = None
    terminal, status = {}, "environment_setup"
    stage, error_code, error_type = "environment_setup", None, None
    try:
        await env.start()
        stage = "agent_setup"
        raw = llm_factory(
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
        guarded = GuardedLLM(raw, ledger, run_id)

        @tool(is_parallel_safe=False)
        async def shell(command: str) -> str:
            """Run a command in /testbed in the offline official task container (60s cap)."""
            return json.dumps(await env.exec(command), ensure_ascii=False)

        store = ArtifactStore(root / "artifacts")
        agent = Agent(
            llm=guarded,
            tools=[shell, *make_artifact_tools(store)],
            session=Session(path=root / "session.jsonl"),
            system_prompt="Repair the issue in /testbed using shell. The container has no network. Do not inspect benchmark gold patches, hidden tests or scoring scripts. Use the visible repository tests as appropriate. Finish after editing files; the host will extract your diff.",
            context_budget=32000,
            artifact_store=store if arm == "on" else None,
            memory_dir=False,
            task_store=False,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            run_limits=RunLimits(max_turns=16, max_seconds=600),
        )

        def record(event):
            if isinstance(event, AgentEnd):
                terminal.update(stop_reason=event.stop_reason, limit_details=event.limit_details)

        agent.subscribe(record)
        status = "running"
        stage = "agent_run"
        await asyncio.wait_for(agent.run(task["problem_statement"]), timeout=600)
        status = terminal.get("stop_reason") or "missing_terminal_event"
        stage = "prediction_extract"
        patch = await env.patch()
        prediction = {
            "instance_id": task["instance_id"],
            "model_name_or_path": "my-agent-harness-" + arm,
            "model_patch": patch,
        }
        (root / "model_patch.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")
        stage = "finished"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        error_type = type(exc).__name__
        error_code = getattr(exc, "error_code", "unclassified_" + error_type)
        if agent is None:
            status = "pre_agent_error"
            stage = getattr(env, "stage", stage)
        else:
            status = error_type
        raise RuntimeError("SWE solver stopped; see sanitized run.json") from None
    finally:
        cleanup_errors = []
        try:
            if agent is not None:
                await agent.aclose()
        except BaseException as exc:
            cleanup_errors.append("agent:" + type(exc).__name__)
        try:
            await env.close()
        except BaseException as exc:
            cleanup_errors.append("container:" + type(exc).__name__)
        rows = [r for r in ledger.snapshot()["requests"] if r["run_id"] == run_id]
        known = bool(rows) and all(r["cost_micro_usd"] is not None for r in rows)
        report = {
            "instance_id": task["instance_id"],
            **PINNED[task["instance_id"]],
            "metadata_sha256": sha(metadata),
            "gold_evidence_sha256": sha(gold_evidence),
            "run_id": run_id,
            "adapter_protocol": "v2",
            "arm": arm,
            "status": status,
            "stage": stage,
            "error_code": error_code,
            "error_type": error_type,
            "initial_image_head": getattr(env, "initial_head", None),
            "requests": len(rows),
            **terminal,
            "usage_available": known,
            "usage": [r["usage"] for r in rows],
            "cost_usd": sum(r["cost_micro_usd"] for r in rows) / 1_000_000 if known else None,
            "official_reward": None,
            "prediction_exists": (root / "model_patch.jsonl").exists(),
            "cleanup_errors": cleanup_errors,
        }
        (root / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--gold-evidence", required=True)
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--arm", choices=("on", "off"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    asyncio.run(solve(**vars(args)))


if __name__ == "__main__":
    main()
