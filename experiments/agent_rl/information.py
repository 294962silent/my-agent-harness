"""信息可得性实验：测试可见 vs 盲改。

动机：前面的实验（939 次执行）显示，只要**测试文件可见且题面点名 bug**，
模型就能 100% 完成；但盲改（隐藏测试 + 题面不说明）时 3/3 全部失败。

这说明模型的能力强依赖**信息输入**，而非代码理解本身——这是一个有区分度的维度。

四种条件（信息量递减）：
1. `full`：测试可见 + 题面描述 bug（baseline）
2. `hidden`：测试隐藏 + 题面不描述 bug，但允许读 `.hidden/` 测试
3. `blind`：测试完全不可见 + 题面不描述 bug（只有"有 bug"这一句）
4. `no_hint`：测试可见 + 题面不描述 bug（只隐藏文字提示）

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.information --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.runner import make_model_client, run_task_once  # noqa: E402
from experiments.agent_rl.task import Task, generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"

BLIND_PROMPT = "这个模块存在 bug，测试会失败。请自行判断问题所在并修复。不要修改测试文件。"
NO_HINT_PROMPT = "请让 `python -m pytest -q` 全部通过。不要修改测试文件。"


def make_variant(task: Task, condition: str) -> Task:
    """根据信息条件构造任务变体。"""
    tests = {k: v for k, v in task.files.items() if k.startswith("test_") or "/test_" in k}
    others = {k: v for k, v in task.files.items() if k not in tests}

    if condition == "full":
        return Task(
            task_id=f"{task.task_id}@full",
            template=task.template,
            difficulty=task.difficulty,
            prompt=task.prompt,
            files=dict(task.files),
            check_cmd=task.check_cmd,
            timeout_s=task.timeout_s,
            meta={**task.meta, "condition": "full"},
        )

    if condition == "no_hint":
        return Task(
            task_id=f"{task.task_id}@nohint",
            template=task.template,
            difficulty=task.difficulty,
            prompt=NO_HINT_PROMPT,
            files=dict(task.files),
            check_cmd=task.check_cmd,
            timeout_s=task.timeout_s,
            meta={**task.meta, "condition": "no_hint"},
        )

    if condition == "hidden":
        files = dict(others)
        for name, content in tests.items():
            files[f".hidden/{name}"] = content
        return Task(
            task_id=f"{task.task_id}@hidden",
            template=task.template,
            difficulty=task.difficulty,
            prompt=BLIND_PROMPT + "（提示：`.hidden/` 下有测试，可以读）",
            files=files,
            check_cmd=task.check_cmd,
            timeout_s=task.timeout_s,
            meta={**task.meta, "condition": "hidden"},
        )

    # blind：测试完全不放到 workspace，判定时再注入
    check_cmd = task.check_cmd
    return Task(
        task_id=f"{task.task_id}@blind",
        template=task.template,
        difficulty=task.difficulty,
        prompt=BLIND_PROMPT,
        files=dict(others),
        check_cmd=check_cmd,
        timeout_s=task.timeout_s,
        meta={**task.meta, "condition": "blind", "hidden_tests": tests},
    )


async def _run_variant(task: Task, semaphore: asyncio.Semaphore, out_path: Path, lock: asyncio.Lock, counter: dict, temperature: float) -> None:
    async with semaphore:
        # blind 条件：判定前把测试注入
        hidden = task.meta.get("hidden_tests") or {}
        if hidden:
            original_run = task.check

            def check_with_tests(workspace: Path):
                for name, content in hidden.items():
                    target = workspace / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                return original_run(workspace)

            task.check = check_with_tests  # type: ignore[method-assign]

        try:
            episode = await run_task_once(task, model_client=make_model_client(temperature=temperature))
            payload = {
                "task_id": task.task_id,
                "base_task": task.task_id.split("@")[0],
                "condition": task.meta.get("condition"),
                "template": task.template,
                "success": episode.success,
                "steps": episode.steps,
                "duration_s": round(episode.duration_s, 2),
                "error": episode.error,
            }
        except Exception as exc:  # noqa: BLE001
            payload = {
                "task_id": task.task_id,
                "base_task": task.task_id.split("@")[0],
                "condition": task.meta.get("condition"),
                "template": task.template,
                "success": False,
                "steps": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        async with lock:
            counter["done"] += 1
            counter["ok"] += int(payload["success"])
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            print(
                f"[{counter['done']}/{counter['total']}] {payload['condition']:8s} {payload['base_task']} "
                f"{'OK  ' if payload['success'] else 'FAIL'}",
                flush=True,
            )


async def main_async(args: argparse.Namespace) -> None:
    base = [t for t in generate_all() if t.template == args.template][: args.limit]
    conditions = [c.strip() for c in args.conditions.split(",")]
    variants = [make_variant(t, c) for t in base for c in conditions]

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"information_{args.template}.jsonl"
    if out_path.exists():
        out_path.unlink()

    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    counter = {"done": 0, "ok": 0, "total": len(variants)}
    print(f"模板={args.template} 任务={len(base)} 条件={conditions} 总执行={len(variants)}")

    await asyncio.gather(
        *(_run_variant(v, semaphore, out_path, lock, counter, args.temperature) for v in variants)
    )
    print(f"\n完成 {counter['done']}，成功 {counter['ok']}")


def summarize(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_cond: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_cond[row["condition"]].append(row)
    print("\n条件 | 样本 | 通过率 | 平均步数")
    print("--- | --- | --- | ---")
    for cond in ["full", "no_hint", "hidden", "blind"]:
        items = by_cond.get(cond)
        if not items:
            continue
        acc = sum(1 for x in items if x["success"]) / len(items)
        steps = sum(x["steps"] for x in items) / len(items)
        print(f"{cond} | {len(items)} | {acc:.1%} | {steps:.1f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default="t1_bugfix")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--conditions", default="full,no_hint,hidden,blind")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()

    out_path = RESULTS / f"information_{args.template}.jsonl"
    if not args.summarize_only:
        asyncio.run(main_async(args))
    if out_path.exists():
        summarize(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
