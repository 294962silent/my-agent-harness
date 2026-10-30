"""预算扫描：找出每个任务的"最小可行预算"，得到有区分度的通过率分布。

动机：预算 2 全失败、预算 4 全成功，说明临界点在 2–4 之间。
扫描 1..8 的预算，就能得到"同一任务在不同预算下成败不同"的中间态，
从而算出真实的 pass^k / pass@k / gap。

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.budget_sweep --limit 10 --samples 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.budget import run_budget_limited  # noqa: E402
from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


async def _one(
    task,
    budget: int,
    sample: int,
    semaphore: asyncio.Semaphore,
    out_path: Path,
    lock: asyncio.Lock,
    counter: dict,
    temperature: float,
) -> None:
    async with semaphore:
        factory = lambda: make_model_client(temperature=temperature)  # noqa: E731
        try:
            episode = await run_budget_limited(task, factory, budget=budget, sample_index=sample)
        except Exception as exc:  # noqa: BLE001
            payload = {
                "task_id": task.task_id,
                "template": task.template,
                "difficulty": task.difficulty,
                "budget": budget,
                "sample_index": sample,
                "success": False,
                "steps": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            payload = {
                "task_id": task.task_id,
                "template": task.template,
                "difficulty": task.difficulty,
                "budget": budget,
                "sample_index": sample,
                "success": episode.success,
                "steps": episode.steps,
                "duration_s": round(episode.duration_s, 2),
                "error": episode.error,
            }
        async with lock:
            counter["done"] += 1
            counter["ok"] += int(payload["success"])
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            print(
                f"[{counter['done']}/{counter['total']}] {task.task_id} budget={budget} "
                f"{'OK  ' if payload['success'] else 'FAIL'} steps={payload['steps']:2d}",
                flush=True,
            )


async def main_async(args: argparse.Namespace) -> None:
    tasks = generate_all()
    if args.limit:
        tasks = tasks[: args.limit]
    budgets = [int(x) for x in args.budgets.split(",")]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"budget_sweep_{args.tag}.jsonl"
    if out_path.exists():
        out_path.unlink()

    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    total = len(tasks) * len(budgets) * args.samples
    counter = {"done": 0, "ok": 0, "total": total}
    print(f"任务={len(tasks)} 预算={budgets} 每格采样={args.samples} 总执行={total}")

    await asyncio.gather(
        *(
            _one(task, budget, sample, semaphore, out_path, lock, counter, args.temperature)
            for task in tasks
            for budget in budgets
            for sample in range(args.samples)
        )
    )
    print(f"\n完成 {counter['done']}，成功 {counter['ok']}")
    print(f"结果: {out_path}")


def summarize(path: Path) -> None:
    """打印预算 × 通过率 的矩阵。"""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_budget: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_budget[row["budget"]].append(row)

    print("\n预算 | 通过率 | 题目平均成功率 | 完全通过的任务 | 部分通过 | 全失败")
    print("--- | --- | --- | --- | --- | ---")
    for budget in sorted(by_budget):
        items = by_budget[budget]
        by_task: dict[str, list[bool]] = defaultdict(list)
        for item in items:
            by_task[item["task_id"]].append(item["success"])
        rates = [sum(v) / len(v) for v in by_task.values()]
        all_pass = sum(1 for r in rates if r == 1.0)
        partial = sum(1 for r in rates if 0 < r < 1)
        none = sum(1 for r in rates if r == 0.0)
        acc = sum(1 for item in items if item["success"]) / len(items)
        avg = sum(rates) / len(rates)
        print(f"{budget} | {acc:.2f} | {avg:.2f} | {all_pass} | {partial} | {none}")

    # 找"最有区分度"的预算：部分通过的任务最多
    best_budget, best_partial = None, -1
    for budget in sorted(by_budget):
        by_task: dict[str, list[bool]] = defaultdict(list)
        for item in by_budget[budget]:
            by_task[item["task_id"]].append(item["success"])
        partial = sum(1 for v in by_task.values() if 0 < sum(v) < len(v))
        if partial > best_partial:
            best_budget, best_partial = budget, partial

    print(f"\n最有区分度的预算: {best_budget}（{best_partial} 个任务出现部分成功）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limits", dest="limit", type=int, default=10)
    parser.add_argument("--budgets", default="1,2,3,4,6")
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--tag", default="main")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()

    out_path = RESULTS / f"budget_sweep_{args.tag}.jsonl"
    if not args.summarize_only:
        asyncio.run(main_async(args))
    if out_path.exists():
        summarize(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
