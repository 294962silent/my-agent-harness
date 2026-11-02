"""在线稠密奖励：在沙盒里每步检查中间 artifact。

与 dense_reward.py 的区别：
- dense_reward.py：离线，从已有轨迹算分（用动作类型代理）
- 本脚本：在线，**真的每步跑一次测试**，得到真实的测试进展

成本：每步多一次容器内 pytest（约 1-2 秒），10 步 ≈ 多 15 秒。
价值：奖励来自真实状态，不依赖动作类型猜测。

用法（仓库根目录）：
    uv run python -m experiments.agent_rl.online_dense --task t1-002 --steps 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.dense_reward import DenseStep, DenseTrajectory  # noqa: E402
from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.sandbox import Sandbox  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


@dataclass
class OnlineTrace:
    """在线采集的轨迹（含每步真实测试状态）。"""

    task_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    success: bool = False
    duration_s: float = 0.0
    total_test_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "duration_s": round(self.duration_s, 1),
            "total_test_runs": self.total_test_runs,
            "steps": self.steps,
        }


def _run_tests(box: Sandbox, check_cmd: list[str]) -> tuple[int, int]:
    """在容器里跑测试，返回 (通过数, 失败数)。"""
    import re

    result = box.run(check_cmd)
    out = (result.stdout or "") + (result.stderr or "")
    passed = failed = 0
    for m in re.finditer(r"(\d+) passed", out):
        passed = max(passed, int(m.group(1)))
    for m in re.finditer(r"(\d+) (?:failed|error)", out):
        failed = max(failed, int(m.group(1)))
    return passed, failed


BACKEND = ["docker"]


async def run_online(task: Any, max_steps: int, temperature: float, model: str | None) -> OnlineTrace:
    """跑一条轨迹，每步都用沙盒检查真实测试状态。"""
    started = time.time()
    trace = OnlineTrace(task_id=task.task_id)

    with tempfile.TemporaryDirectory(prefix="online-") as tmp:
        workspace = Path(tmp) / "ws"
        task.materialize(workspace)

        with Sandbox(workspace, task_id=f"online-{task.task_id}", timeout_s=task.timeout_s, backend=BACKEND[0]) as box:
            # 初始状态
            passed0, failed0 = _run_tests(box, task.check_cmd)
            trace.total_test_runs += 1
            prev_passed = passed0

            files_preview = "\n".join(f"--- {n} ---\n{c[:800]}" for n, c in sorted(task.files.items()))[:4000]
            history: list[str] = []
            prompt_base = f"{SYSTEM}\n\n=== TASK ===\n{task.prompt}\n\n=== FILES ===\n{files_preview}\n\n"

            client = make_model_client(model=model, temperature=temperature)

            for step_index in range(max_steps):
                convo = prompt_base + "\n".join(history) + "\n=== YOUR TURN ===\n"
                # 直接用底层客户端对话
                from my_agent_llm import Message

                response = await client.achat([Message(role="user", content=convo)])
                text = getattr(response, "content", "") or ""

                if "DONE" in text.strip()[-30:]:
                    trace.steps.append({"step": step_index, "tool": "done", "raw": text[:200]})
                    break

                action = _parse_action(text)
                if action is None:
                    trace.steps.append({"step": step_index, "tool": "?", "raw": text[:200], "parsed": False})
                    history.append("[observation] ERROR: no JSON action. Reply with one JSON object.")
                    continue

                observation = _exec(box, workspace, action)
                # 每步跑测试，得到真实进展
                passed, failed = _run_tests(box, task.check_cmd)
                trace.total_test_runs += 1
                trace.steps.append(
                    {
                        "step": step_index,
                        "tool": action.get("name"),
                        "args": action.get("arguments"),
                        "observation": observation[:300],
                        "passed": passed,
                        "failed": failed,
                        "gain": max(0, passed - prev_passed),
                    }
                )
                prev_passed = max(prev_passed, passed)
                history.append(f"[observation] {observation[:800]}\n[tests] {passed} passed, {failed} failed")
                if sum(len(h) for h in history) > 10000:
                    history = history[-4:]

            # 最终判定
            final = box.run(task.check_cmd)
            trace.success = bool(final.ok)

    trace.duration_s = time.time() - started
    return trace


SYSTEM = """You are fixing code in a repository. Output exactly ONE JSON action per turn.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"done","arguments":{}}

Output ONLY the JSON object. No explanation, no markdown fences.
"""


def _parse_action(text: str) -> dict[str, Any] | None:
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for candidate in re.findall(r"\{.*\}", cleaned, re.S):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "name" in data:
            args = data.get("arguments")
            if not isinstance(args, dict):
                args = {k: v for k, v in data.items() if k != "name"}
            return {"name": data["name"], "arguments": args}
    return None


def _exec(box: Sandbox, workspace: Path, action: dict[str, Any]) -> str:
    name = action.get("name")
    args = action.get("arguments") or {}
    if name == "read":
        target = workspace / str(args.get("path", ""))
        return target.read_text(encoding="utf-8", errors="replace")[:2000] if target.exists() else "ERROR: no file"
    if name in ("write", "edit"):
        target = workspace / str(args.get("path", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "write":
            target.write_text(str(args.get("content", "")), encoding="utf-8")
            return f"wrote {target.name}"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        old = str(args.get("oldText", ""))
        if not old or old not in content:
            return "ERROR: oldText not found"
        target.write_text(content.replace(old, str(args.get("newText", "")), 1), encoding="utf-8")
        return f"edited {target.name}"
    if name == "bash":
        r = box.run(["sh", "-lc", str(args.get("command", "true"))])
        return ((r.stdout or "") + (r.stderr or ""))[:800]
    if name == "ls":
        r = box.run(["ls", "-la", str(args.get("path", "."))])
        return (r.stdout or "")[:800]
    return f"ERROR: unknown {name}"


def to_dense(trace: OnlineTrace) -> DenseTrajectory:
    """把在线轨迹转成稠密奖励结构（用真实测试进展）。"""
    steps: list[DenseStep] = []
    prev = 0
    for s in trace.steps:
        if s.get("passed") is None:
            continue
        passed = s["passed"]
        failed = s.get("failed", 0)
        step = DenseStep(step=s["step"], tool=str(s.get("tool")))
        step.tests_passed = passed
        step.tests_total = max(passed + failed, 1)
        step.test_gain = max(0, passed - prev) / step.tests_total
        step.regression = -0.3 * min(failed, 3) / step.tests_total if failed and prev > 0 else 0.0
        step.total = step.test_gain + step.regression
        prev = max(prev, passed)
        steps.append(step)
    return DenseTrajectory(task_id=trace.task_id, success=trace.success, steps=steps)


async def main_async(args: argparse.Namespace) -> None:
    all_tasks = {t.task_id: t for t in generate_all()}
    if args.task:
        tasks = [all_tasks[args.task]]
    else:
        tasks = [t for t in generate_all() if t.template == args.template][: args.limit]

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "online_dense.jsonl"
    if out.exists():
        out.unlink()

    for index, task in enumerate(tasks, 1):
        print(f"[{index}/{len(tasks)}] {task.task_id} ...", flush=True)
        trace = await run_online(task, args.steps, args.temperature, args.model)
        dense = to_dense(trace)
        record = {**trace.to_dict(), "dense": dense.to_dict()}
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"    {'OK' if trace.success else 'FAIL'} {trace.duration_s:.0f}s "
            f"steps={len(trace.steps)} 测试次数={trace.total_test_runs} "
            f"稠密总分={dense.dense_total:.2f} 密度={dense.density:.0%}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default=None)
    parser.add_argument("--template", default="t1_bugfix")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--model", default=None)
    parser.add_argument("--backend", default="docker", choices=["docker", "host"])
    args = parser.parse_args()
    BACKEND[0] = args.backend
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
