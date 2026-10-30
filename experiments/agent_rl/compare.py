"""策略对比：在同一批任务上跑多种 agent 策略，输出对照表。

跑法（仓库根目录）：
    # 4 种策略 × N 题（默认全部 100 题，可 --limit 抽样）
    uv run python -m experiments.agent_rl.compare --strategies single,best_of_3,reflexion,plan_first --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.strategies import STRATEGIES  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


async def _run(
    strategy: str,
    tasks: list,
    semaphore: asyncio.Semaphore,
    out_path: Path,
    lock: asyncio.Lock,
    counter: dict,
    temperature: float,
    model: str | None,
) -> None:
    runner = STRATEGIES[strategy]
    for task in tasks:
        async with semaphore:
            factory = lambda: make_model_client(model=model, temperature=temperature)  # noqa: E731
            try:
                result = await runner(task, factory)
            except Exception as exc:  # noqa: BLE001
                from experiments.agent_rl.strategies import StrategyResult

                result = StrategyResult(
                    task_id=task.task_id,
                    strategy=strategy,
                    success=False,
                    attempts=0,
                    reward=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            async with lock:
                counter["done"] += 1
                counter["ok"] += int(result.success)
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
                elapsed = time.time() - counter["start"]
                print(
                    f"[{strategy}] {task.task_id} {'OK  ' if result.success else 'FAIL'} "
                    f"attempts={result.attempts} {result.duration_s:6.1f}s "
                    f"| {counter['ok']}/{counter['done']} | {elapsed/60:.1f}min",
                    flush=True,
                )


async def main_async(args: argparse.Namespace) -> None:
    tasks = generate_all()
    if args.tasks_file:
        from experiments.agent_rl.task import load_tasks

        keep = {t.task_id for t in load_tasks(Path(args.tasks_file))}
        tasks = [t for t in tasks if t.task_id in keep]
    if args.limit:
        tasks = tasks[: args.limit]
    if args.harden:
        from experiments.agent_rl.hardening import harden_all

        tasks = harden_all(tasks, level=args.harden)

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"strategy_{args.tag}.jsonl"

    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    total = len(tasks) * len(strategies)
    counter = {"done": 0, "ok": 0, "start": time.time()}
    print(f"策略={strategies} 任务={len(tasks)} 总执行={total} 并发={args.concurrency}")

    await asyncio.gather(
        *(
            _run(
                strategy,
                tasks,
                semaphore,
                out_path,
                lock,
                counter,
                args.temperature,
                args.model,
            )
            for strategy in strategies
        )
    )
    print(f"\n完成 {counter['done']}，成功 {counter['ok']}，耗时 {(time.time()-counter['start'])/60:.1f} 分钟")
    print(f"结果: {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default="single,best_of_3,reflexion,plan_first")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--harden", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--model", default=None)
    parser.add_argument("--tasks-file", default=None)
    parser.add_argument("--tag", default="all")
    args = parser.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
