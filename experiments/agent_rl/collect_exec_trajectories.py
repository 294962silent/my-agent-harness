"""用 DeepSeek 在边界任务上采集成功轨迹，带真实工具反馈。

关键：与旧的 sft_train.jsonl 不同，这里每条 observation 都是**真实执行结果**
（ls 的真实文件列表、read 的真实文件内容、pytest 的真实输出），
而不是占位符 "(tool executed)"。

旧的 SFT 数据 6703 条 observation 只有 1 种内容，模型学不到「反馈 → 决策」，
所以在多步任务上只会陷入 ls 死循环。

用法（需要本机 DeepSeek 凭据）：
    uv run python -m experiments.agent_rl.collect_exec_trajectories --rounds 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")

from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

OUT = Path(__file__).resolve().parent / "results" / "exec_trajectories.jsonl"

SYSTEM = """You are fixing code in a Python repository. Output exactly ONE JSON action per turn, and nothing else.

Available actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL FILE CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"COMMAND"}}
{"name":"ls","arguments":{"path":"DIR"}}

Rules:
- Output ONLY the JSON object. No explanation. No markdown fences.
- One action per turn.
- Do NOT repeat an action you already took in this episode.
"""


def parse_action(text: str) -> dict | None:
    from my_agent_llm import Message  # noqa: F401

    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    depth = 0
    start = -1
    in_str = escape = False
    for idx, ch in enumerate(cleaned):
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
                try:
                    data = json.loads(cleaned[start : idx + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(data, dict):
                    args = data.get("arguments")
                    if not isinstance(args, dict):
                        args = {k: v for k, v in data.items() if k not in ("name", "type")}
                    name = data.get("name") or data.get("type")
                    if name:
                        return {"name": name, "arguments": args}
                start = -1
    return None


def exec_action(ws: Path, action: dict) -> str:
    """执行动作，返回**真实**输出。"""
    name = action.get("name")
    args = action.get("arguments") or {}
    try:
        if name == "read":
            p = ws / str(args.get("path", ""))
            if not p.exists():
                return f"ERROR: file not found: {args.get('path')}"
            return p.read_text(encoding="utf-8", errors="replace")[:2000]
        if name == "ls":
            p = ws / str(args.get("path", "."))
            if not p.exists():
                return f"ERROR: dir not found: {args.get('path')}"
            items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
            return " ".join(items) if items else "(empty)"
        if name == "find":
            pattern = str(args.get("pattern", "*"))
            hits = sorted(str(x.relative_to(ws)) for x in ws.rglob(pattern.lstrip("*").lstrip("/") or "*"))
            return "\n".join(hits[:50]) if hits else "(no match)"
        if name in ("write", "edit"):
            p = ws / str(args.get("path", ""))
            if name == "write":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(args.get("content", "")), encoding="utf-8")
                return f"wrote {args.get('path')} ({len(str(args.get('content','')))} chars)"
            content = p.read_text(encoding="utf-8") if p.exists() else ""
            edits = args.get("edits") or []
            if not edits and args.get("oldText"):
                edits = [{"oldText": args["oldText"], "newText": args.get("newText", "")}]
            applied = 0
            for e in edits:
                old, new = str(e.get("oldText", "")), str(e.get("newText", ""))
                if old and old in content:
                    content = content.replace(old, new, 1)
                    applied += 1
            if applied == 0:
                return "ERROR: oldText not found in file"
            p.write_text(content, encoding="utf-8")
            return f"edited {args.get('path')} ({applied} edits applied)"
        if name == "bash":
            proc = subprocess.run(
                str(args.get("command", "true")), shell=True, cwd=ws,
                capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace",
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return out[:1500] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timeout"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    return f"ERROR: unknown action {name}"


def run_tests(ws: Path) -> tuple[bool, str]:
    for cache in ws.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    env = {
        "PYTHONPATH": str(ws),
        "PYTHONIOENCODING": "utf-8",
        "PATH": "D:\\soft\\Git\\bin;C:\\Windows\\System32;C:\\Windows",
        "SYSTEMROOT": "C:\\Windows",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
             "--rootdir", str(ws), "tests"],
            cwd=ws, capture_output=True, text=True, timeout=200,
            encoding="utf-8", errors="replace", env=env,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


async def run_task(client, task, max_steps: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="traj-") as tmp:
        ws = Path(tmp) / "ws"
        task.materialize(ws)
        ok0, _ = run_tests(ws)

        files = sorted(k for k in task.files if not k.startswith("tests/"))
        tests = sorted(k for k in task.files if k.startswith("tests/"))
        header = (
            SYSTEM + f"\n=== TASK ===\n{task.prompt}\n\n"
            "=== SOURCE FILES ===\n" + "\n".join(f"  {f}" for f in files) + "\n\n"
            "=== TEST FILES ===\n" + "\n".join(f"  {t}" for t in tests) + "\n\n"
        )

        from my_agent_llm import Message

        messages: list[dict] = []
        seen: set[str] = set()
        tools: list[str] = []
        last_out = ""

        for _ in range(max_steps):
            done = [x for x in seen if '"write"' in x or '"edit"' in x]
            # 从测试输出解析剩余未实现的模块数
            remaining = len([f for f in files if f.endswith(".py") and not f.endswith("__init__.py")])
            written = set()
            for sig in seen:
                for f in files:
                    if f in sig:
                        written.add(f)
            remaining = max(0, remaining - len(written))
            progress = (
                f"\n=== PROGRESS ===\n"
                f"Actions taken: {len(tools)} | writes/edits: {len(done)}\n"
                f"Source files: {len(files)} | files you have touched: {len(written)}\n"
            )
            if remaining > 0:
                progress += (
                    f"WARNING: approximately {remaining} source file(s) still unimplemented. "
                    "Tests are failing with NotImplementedError. "
                    "Use `write` to replace each stub with a working implementation.\n"
                )
            convo = header + progress + "\n".join(messages[-6:]) + "\n=== YOUR TURN ===\n"
            try:
                resp = await client.achat([Message(role="user", content=convo)])
                text = resp.content or ""
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"llm: {exc}", "tools": tools,
                        "messages": messages, "output": last_out}

            action = parse_action(text)
            if action is None:
                messages.append('assistant: [invalid] output must be a JSON action')
                continue

            sig = json.dumps(action, sort_keys=True, ensure_ascii=False)
            if sig in seen:
                messages.append(f"assistant: {sig}")
                messages.append("[observation] You already did this. Choose a DIFFERENT action.")
                continue
            seen.add(sig)

            name = action.get("name", "?")
            tools.append(name)
            obs = exec_action(ws, action)
            messages.append(f"assistant: {sig}")
            messages.append(f"[observation] {obs}")

            ok, out = run_tests(ws)
            last_out = out
            if ok:
                return {"success": True, "tools": tools, "messages": messages,
                        "output": out, "steps": len(tools)}

            tail = out.strip().splitlines()[-1][:120] if out.strip() else ""
            messages.append(f"[tests] {tail}")

        return {"success": False, "tools": tools, "messages": messages,
                "output": last_out, "steps": len(tools)}


async def main_async(args) -> int:
    boundary = ["t8-001", "t8-005", "t8e-001", "t8e-002", "t8f-001",
                "t8f-002", "t8g-001", "t8g-002", "t8g-003", "t8g-004"]
    all_tasks = {t.task_id: t for t in generate_all()}
    tasks = [all_tasks[tid] for tid in boundary if tid in all_tasks]

    client = make_model_client(model=args.model, temperature=args.temperature)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    print(f"=== 采集轨迹：{len(tasks)} 个任务 × {args.rounds} 轮 ===\n", flush=True)
    ok_total = 0
    run_total = 0
    for tid, task in zip(boundary, tasks):
        succ = 0
        for r in range(args.rounds):
            t0 = time.time()
            rec = await run_task(client, task, args.steps)
            rec["task_id"] = tid
            rec["round"] = r
            rec["duration_s"] = round(time.time() - t0, 1)
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            run_total += 1
            if rec["success"]:
                succ += 1
                ok_total += 1
            print(
                f"  {tid} #{r} {'OK  ' if rec['success'] else 'FAIL'} "
                f"{rec.get('steps', 0)} 步 {rec['duration_s']:.0f}s",
                flush=True,
            )
        print(f"  → {tid}: {succ}/{args.rounds}\n", flush=True)

    print(f"总计: {ok_total}/{run_total} 成功，写入 {OUT}", flush=True)

    # 检查 observation 多样性
    obs = []
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        for m in rec.get("messages", []):
            if str(m).startswith("[observation]"):
                obs.append(str(m))
    print(f"observation 总数: {len(obs)}, 唯一内容: {len(set(obs))}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--steps", type=int, default=40)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--model", default=None)
    asyncio.run(main_async(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
