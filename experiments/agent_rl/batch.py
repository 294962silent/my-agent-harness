"""批量执行器：并发跑任务，支持断点续跑与结果落盘。

跑法（仓库根目录）：
    # 难度校准：每题 1 次
    uv run python -m experiments.agent_rl.batch --stage calibrate --samples 1 --concurrency 4

    # 主实验：每题 k 次
    uv run python -m experiments.agent_rl.batch --stage main --samples 5 --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.runner import Episode, make_model_client, run_task_once  # noqa: E402
from experiments.agent_rl.task import Task, generate_all, load_tasks  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


@dataclass
class BatchConfig:
    stage: str
    samples: int
    concurrency: int
    model: str | None
    temperature: float
    tasks_file: Path | None = None
    out_file: Path | None = None


def _completed_keys(path: Path) -> set[tuple[str, int]]:
    """断点续跑：已完成的 (task_id, sample_index)。"""
    done: set[tuple[str, int]] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((data["task_id"], data.get("sample_index", 0)))
    return done


async def _run_one(
    task: Task,
    sample_index: int,
    config: BatchConfig,
    semaphore: asyncio.Semaphore,
    out_path: Path,
    lock: asyncio.Lock,
    counter: dict[str, int],
) -> None:
    async with semaphore:
        client = make_model_client(model=config.model, temperature=config.temperature)
        try:
            episode = await run_task_once(task, model_client=client, sample_index=sample_index)
        except Exception as exc:  # noqa: BLE001
            episode = Episode(
                task_id=task.task_id,
                template=task.template,
                difficulty=task.difficulty,
                sample_index=sample_index,
                model=config.model or "deepseek-v4.1-flash",
                success=False,
                reward=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
        async with lock:
            counter["done"] += 1
            counter["ok"] += int(episode.success)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")
            elapsed = time.time() - counter["start"]
            print(
                f"[{counter['done']}/{counter['total']}] {task.task_id}#{sample_index} "
                f"{'OK  ' if episode.success else 'FAIL'} "
                f"{episode.duration_s:5.1f}s steps={episode.steps:2d} "
                f"| 累计通过 {counter['ok']}/{counter['done']} "
                f"| 已用 {elapsed/60:.1f}min",
                flush=True,
            )


async def run_batch(tasks: list[Task], config: BatchConfig) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = config.out_file or RESULTS / f"episodes_{config.stage}.jsonl"
    if out_path.exists() and config.stage == "calibrate":
        backup = out_path.with_suffix(f".{int(time.time())}.bak")
        out_path.rename(backup)
        print(f"旧结果已备份: {backup.name}")

    done = _completed_keys(out_path)
    pending = [(task, i) for task in tasks for i in range(config.samples) if (task.task_id, i) not in done]
    print(f"阶段={config.stage} 任务={len(tasks)} 采样={config.samples} 待跑={len(pending)} 并发={config.concurrency}")
    if not pending:
        print("没有待跑的任务")
        return out_path

    semaphore = asyncio.Semaphore(config.concurrency)
    lock = asyncio.Lock()
    counter = {"done": 0, "ok": 0, "total": len(pending), "start": time.time()}

    await asyncio.gather(
        *(_run_one(task, index, config, semaphore, out_path, lock, counter) for task, index in pending)
    )
    print(f"\n完成 {counter['done']} 条，通过 {counter['ok']}，耗时 {(time.time()-counter['start'])/60:.1f} 分钟")
    print(f"结果: {out_path}")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="calibrate")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--tasks-file", default=None, help="只跑该文件里的任务（主实验用校准后的子集）")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--harden", type=int, default=0, help="难度倍增等级 0-3")
    args = parser.parse_args()

    if args.tasks_file:
        tasks = load_tasks(Path(args.tasks_file))
    else:
        tasks = generate_all()
    if args.limit:
        tasks = tasks[: args.limit]
    if args.harden:
        from experiments.agent_rl.hardening import harden_all

        tasks = harden_all(tasks, level=args.harden)
        print(f"已加固: level={args.harden}")

    config = BatchConfig(
        stage=args.stage,
        samples=args.samples,
        concurrency=args.concurrency,
        model=args.model,
        temperature=args.temperature,
        out_file=Path(args.out) if args.out else None,
    )
    asyncio.run(run_batch(tasks, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
