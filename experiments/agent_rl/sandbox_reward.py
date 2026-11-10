"""沙盒执行奖励：奖励必须来自真实执行，而不是结构检查。

背景（见 DIRECTION_2.md 第十节）：
结构奖励在 SFT 之后已经饱和——9B 模型对简单任务 4 次采样全对，
reward_std=0，没有梯度。真正能区分的信号必须是「这个动作在沙盒里
有没有让测试向前推进」。

设计：
- 每个 prompt 对应一个任务目录快照
- 模型输出一条动作 → 在快照上执行 → 跑测试 → 得到通过数变化
- 奖励 = 通过数增量为主，动作合法性为辅

由于 GRPO 的 reward_fn 是同步的、拿不到训练循环外的状态，
实现方式是：
1. **预生成阶段**：用当前策略对每个任务采样若干条单步动作，
   在沙盒里逐个执行，记录 (prompt, completion, 执行结果)
2. **训练阶段**：把执行结果存成查找表，reward_fn 直接查表

这样奖励依然来自真实执行（可验证），同时满足 TRL 的接口限制。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

VALID_TOOLS = {"read", "write", "edit", "bash", "ls", "find", "grep", "done"}


def _split_json(text: str) -> list[str]:
    out: list[str] = []
    depth, start = 0, -1
    in_str = escape = False
    for idx, ch in enumerate(text or ""):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : idx + 1])
                start = -1
    return out


def parse_action(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    for cand in _split_json(cleaned):
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


def materialize(task: dict, ws: Path) -> None:
    """把任务文件写到工作区。"""
    for rel, content in task["files"].items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def pytest_counts(ws: Path) -> tuple[int, int]:
    """跑测试，返回 (通过数, 总数)。"""
    for cache in ws.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    env = {
        "PYTHONPATH": str(ws),
        "PYTHONIOENCODING": "utf-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/root",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
             "--rootdir", str(ws), "tests"],
            cwd=ws, capture_output=True, text=True, timeout=120, env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        passed = 0
        failed = 0
        for line in out.splitlines():
            m = re.search(r"(\d+) passed", line)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                failed = int(m.group(1))
        return passed, passed + failed
    except subprocess.TimeoutExpired:
        return 0, 0


def exec_action(ws: Path, action: dict) -> str:
    name = action.get("name")
    args = action.get("arguments") or {}
    try:
        if name == "read":
            p = ws / str(args.get("path", ""))
            return p.read_text(encoding="utf-8", errors="replace")[:1500] if p.exists() else "ERROR: no file"
        if name == "ls":
            p = ws / str(args.get("path", "."))
            return " ".join(sorted(x.name for x in p.iterdir()))[:500] if p.exists() else "ERROR: no dir"
        if name in ("write", "edit"):
            p = ws / str(args.get("path", ""))
            p.parent.mkdir(parents=True, exist_ok=True)
            if name == "write":
                p.write_text(str(args.get("content", "")), encoding="utf-8")
                return "wrote"
            old = str(args.get("oldText", ""))
            content = p.read_text(encoding="utf-8") if p.exists() else ""
            if old and old in content:
                p.write_text(content.replace(old, str(args.get("newText", "")), 1), encoding="utf-8")
                return "edited"
            if args.get("edits"):
                for e in args["edits"]:
                    o, n = e.get("oldText", ""), e.get("newText", "")
                    if o and o in content:
                        content = content.replace(o, n, 1)
                p.write_text(content, encoding="utf-8")
                return "edited(multi)"
            return "ERROR: oldText not found"
        if name == "bash":
            proc = subprocess.run(
                str(args.get("command", "true")), shell=True, cwd=ws,
                capture_output=True, text=True, timeout=60,
            )
            return ((proc.stdout or "") + (proc.stderr or ""))[:800]
    except subprocess.TimeoutExpired:
        return "ERROR: timeout"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    return f"ERROR: unknown {name}"


def step_reward(task: dict, completion: str, base_ws: Path) -> dict:
    """评估单步动作：在沙盒里执行，看测试通过数变化。"""
    action = parse_action(completion)
    if action is None:
        return {"reward": -0.6, "detail": {"no_json": -0.6}, "delta": 0, "passed": 0, "total": 0}

    with tempfile.TemporaryDirectory(prefix="sb-") as tmp:
        ws = Path(tmp) / "ws"
        shutil.copytree(base_ws, ws)
        before, total = pytest_counts(ws)
        obs = exec_action(ws, action)
        after, _ = pytest_counts(ws)

    delta = after - before
    reward = 0.0
    detail: dict[str, float] = {}

    # 1. 测试通过数增量（核心信号）
    if total > 0:
        norm = delta / total
        reward += norm * 0.8
        detail["test_delta"] = round(norm * 0.8, 3)

    # 2. 回归惩罚
    if delta < 0:
        reward -= 0.3
        detail["regression"] = -0.3

    # 3. 动作合法
    if action.get("name") in VALID_TOOLS:
        reward += 0.15
        detail["valid_tool"] = 0.15

    # 4. 多动作 / 对话泄漏惩罚
    n_obj = len(_split_json(completion))
    if n_obj > 1:
        reward -= 0.4
        detail["multi_action"] = -0.4
    if re.search(r"\b(assistant|user)\b", completion or "") or "tool executed" in (completion or ""):
        reward -= 0.4
        detail["conversation_leak"] = -0.4

    # 5. 无效动作轻罚
    if action.get("name") in ("read", "ls", "find", "grep") and delta == 0:
        reward -= 0.02
        detail["explore_no_gain"] = -0.02

    return {
        "reward": max(-1.0, min(1.0, round(reward, 4))),
        "detail": detail,
        "delta": delta,
        "passed": after,
        "total": total,
        "action": action.get("name"),
    }


if __name__ == "__main__":
    tasks = json.load(open("/root/autodl-tmp/datasets/boundary_tasks.json", encoding="utf-8"))
    print(f"任务数: {len(tasks)}")
    t = tasks[0]
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        materialize(t, ws)
        p0, tot = pytest_counts(ws)
        print(f"{t['task_id']} 初始: {p0}/{tot} 通过")
        samples = [
            '{"name": "read", "arguments": {"path": "tests/test_job.py"}}',
            '{"name": "done", "arguments": {}}',
            "I will read the file.",
        ]
        for s in samples:
            r = step_reward(t, s, ws)
            print(f"  {r['reward']:+.2f}  {r['detail']}  {s[:50]!r}")
