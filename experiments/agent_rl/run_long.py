"""在 T8 长程任务上实测模型表现（host backend，本地可跑）。

不需要 Docker：用 subprocess 直接在工作区里跑 pytest。

用法：
    uv run python -m experiments.agent_rl.run_long --task t8-002 --steps 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness")
sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness/src")

from experiments.agent_rl.loop_breaker import LoopBreaker  # noqa: E402
from experiments.agent_rl.prod_pressure import ProdPressure  # noqa: E402
from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"

SYSTEM = """You are implementing a multi-module Python project. Output exactly ONE JSON action per turn.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"ls","arguments":{"path":"DIR"}}
{"name":"done","arguments":{}}

Rules:
- Implement modules in dependency order (parser before validator, etc.).
- Run the tests with bash: {"name":"bash","arguments":{"command":"uv run python -m pytest -q tests"}}
- Only use "done" after ALL tests pass.

Output ONLY the JSON object. No explanation, no markdown fences.
"""


def run_tests(workspace: Path) -> tuple[bool, str]:
    """跑测试，返回 (是否全过, 输出)。"""
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


def parse_action(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for cand in re.findall(r"\{.*\}", cleaned, re.S):
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "name" in data:
            args = data.get("arguments")
            if not isinstance(args, dict):
                args = {k: v for k, v in data.items() if k != "name"}
            return {"name": data["name"], "arguments": args}
    return None


def exec_action(workspace: Path, action: dict[str, Any]) -> str:
    name = action.get("name")
    args = action.get("arguments") or {}

    if name == "read":
        target = workspace / str(args.get("path", ""))
        if not target.exists():
            return "ERROR: file not found"
        return target.read_text(encoding="utf-8", errors="replace")[:2500]

    if name == "ls":
        target = workspace / str(args.get("path", "."))
        if not target.exists():
            return "ERROR: dir not found"
        items = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return " ".join(items)[:800]

    if name in ("write", "edit"):
        target = workspace / str(args.get("path", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "write":
            target.write_text(str(args.get("content", "")), encoding="utf-8")
            return f"wrote {target.name} ({len(str(args.get('content', '')))} chars)"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        old = str(args.get("oldText", ""))
        if not old or old not in content:
            return "ERROR: oldText not found in file"
        target.write_text(content.replace(old, str(args.get("newText", "")), 1), encoding="utf-8")
        return f"edited {target.name}"

    if name == "bash":
        cmd = str(args.get("command", "true"))
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=workspace, capture_output=True, text=True,
                timeout=120, encoding="utf-8", errors="replace",
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return f"exit={proc.returncode}\n{out[:1500]}"
        except subprocess.TimeoutExpired:
            return "ERROR: command timeout"

    return f"ERROR: unknown action {name}"


ENABLE_PRESSURE = [True]


class _NullPressure:
    """关闭产出压力时的空实现。"""

    def observe(self, tool: str) -> tuple[str, str]:
        return "none", ""

    def stats(self) -> dict:
        return {"disabled": True}


async def run_one(task, max_steps: int, temperature: float, model: str | None) -> dict:
    started = time.time()
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "stages": task.meta.get("num_stages"),
        "success": False,
        "steps": 0,
        "duration_s": 0.0,
        "tool_sequence": [],
        "test_progression": [],
        "interventions": {},
        "error": None,
    }

    with tempfile.TemporaryDirectory(prefix="long-") as tmp:
        workspace = Path(tmp) / "ws"
        task.materialize(workspace)

        ok0, out0 = run_tests(workspace)
        record["test_progression"].append({"step": -1, "passed": False, "tail": out0.strip().splitlines()[-1][:80] if out0.strip() else ""})

        files_list = "\n".join(f"  {n} ({len(c)} bytes)" for n, c in sorted(task.files.items()))
        prompt_base = f"{SYSTEM}\n\n=== TASK ===\n{task.prompt}\n\n=== FILES ===\n{files_list}\n\n"

        client = make_model_client(model=model, temperature=temperature)
        breaker = LoopBreaker()
        pressure = ProdPressure(max_steps=max_steps) if ENABLE_PRESSURE[0] else _NullPressure()
        history: list[str] = []

        from my_agent_llm import Message

        for step in range(max_steps):
            convo = prompt_base + "\n".join(history[-6:]) + "\n=== YOUR TURN ===\n"
            try:
                resp = await client.achat([Message(role="user", content=convo)])
                text = getattr(resp, "content", "") or ""
            except Exception as exc:  # noqa: BLE001
                record["error"] = f"llm: {type(exc).__name__}: {exc}"
                break

            if "DONE" in text.strip()[-40:]:
                record["tool_sequence"].append("done")
                break

            action = parse_action(text)
            if action is None:
                record["tool_sequence"].append("?")
                history.append("[observation] ERROR: no JSON action found. Reply with ONE JSON object.")
                continue

            tool = action.get("name", "?")
            record["tool_sequence"].append(tool)
            observation = exec_action(workspace, action)

            ok, out = run_tests(workspace)
            tail = out.strip().splitlines()[-1][:80] if out.strip() else ""
            record["test_progression"].append({"step": step, "passed": ok, "tail": tail})
            record["steps"] = step + 1

            if ok:
                record["success"] = True
                break

            key = str(action.get("arguments", {}).get("path", "")) or str(action.get("arguments", {}).get("command", ""))[:40]
            level, hint = breaker.observe(tool, f"{tool}::{key}")
            # 产出压力：检测"只读不写"
            p_level, p_hint = pressure.observe(tool)
            hints = [h for h in (hint, p_hint) if h]
            extra = (chr(10) + chr(10).join(hints)) if hints else ""
            if level == "terminate":
                record["error"] = "loop_terminated"
                break

            history.append(f"[observation] {observation[:900]}\n[tests] {tail}{extra}")

        record["interventions"] = breaker.stats()
        record["pressure"] = pressure.stats()

        # 最终判定
        if not record["success"]:
            ok_final, _ = run_tests(workspace)
            record["success"] = ok_final

    record["duration_s"] = round(time.time() - started, 1)
    return record


async def main_async(args: argparse.Namespace) -> None:
    all_tasks = {t.task_id: t for t in generate_all() if t.template == "t8_long_horizon"}
    tasks = [all_tasks[args.task]] if args.task else list(all_tasks.values())[: args.limit]

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "long_horizon.jsonl"
    if out.exists():
        out.unlink()

    for index, task in enumerate(tasks, 1):
        print(f"[{index}/{len(tasks)}] {task.task_id} ({task.meta['num_stages']} 阶段, 最多 {args.steps} 步) ...", flush=True)
        record = await run_one(task, args.steps, args.temperature, args.model)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        prog = record["test_progression"]
        last = prog[-1]["tail"] if prog else ""
        print(
            f"    {'OK  ' if record['success'] else 'FAIL'} {record['steps']} 步 {record['duration_s']:.0f}s "
            f"| 最后测试: {last}",
            flush=True,
        )
        print(f"    工具序列: {' → '.join(record['tool_sequence'][:15])}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default=None)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-pressure", action="store_true")
    _args = parser.parse_args()
    ENABLE_PRESSURE[0] = not _args.no_pressure
    asyncio.run(main_async(_args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
