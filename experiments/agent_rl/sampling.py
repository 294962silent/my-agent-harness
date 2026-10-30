"""在指定信息条件下做 k 次采样，计算 pass^k / pass@k / gap。

这是把「信息可得性」与「采样」「一致性指标」接起来的关键实验：
- 如果 blind 条件下 pass@k 显著高于 pass^k，说明**采样能救回盲改失败**——
  这正是 RL 训练的空间（gap > 0）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.information import make_variant  # noqa: E402
from experiments.agent_rl.metrics import pass_at_k, pass_hat_k  # noqa: E402
from experiments.agent_rl.runner import make_model_client, run_task_once  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


async def _one(task, sample: int, sem: asyncio.Semaphore, out: Path, lock: asyncio.Lock, counter: dict, temperature: float) -> None:
    async with sem:
        hidden = task.meta.get("hidden_tests") or {}
        if hidden:
            original = task.check

            def patched(workspace: Path):
                for name, content in hidden.items():
                    target = workspace / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                return original(workspace)

            task.check = patched  # type: ignore[method-assign]
        try:
            episode = await run_task_once(task, model_client=make_model_client(temperature=temperature), sample_index=sample)
            success, steps, error = episode.success, episode.steps, episode.error
        except Exception as exc:  # noqa: BLE001
            success, steps, error = False, 0, f"{type(exc).__name__}: {exc}"
        async with lock:
            counter["done"] += 1
            counter["ok"] += int(success)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "task_id": task.task_id,
                    "condition": task.meta.get("condition"),
                    "sample_index": sample,
                    "success": success,
                    "steps": steps,
                    "error": error,
                }, ensure_ascii=False) + "\n")
            print(f"[{counter['done']}/{counter['total']}] {task.task_id} #{sample} {'OK' if success else 'FAIL'} steps={steps}", flush=True)


async def main_async(args: argparse.Namespace) -> None:
    base = [t for t in generate_all() if t.template == args.template][: args.limit]
    variants = [make_variant(t, args.condition) for t in base]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"sampling_{args.condition}_{args.template}.jsonl"
    if out.exists():
        out.unlink()

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    total = len(variants) * args.samples
    counter = {"done": 0, "ok": 0, "total": total}
    print(f"条件={args.condition} 模板={args.template} 任务={len(variants)} 采样={args.samples}")

    await asyncio.gather(*(
        _one(v, i, sem, out, lock, counter, args.temperature)
        for v in variants for i in range(args.samples)
    ))

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_task: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row["success"])

    k = args.samples
    ph, pa = [], []
    for successes in by_task.values():
        n = len(successes)
        c = sum(successes)
        ph.append(pass_hat_k(n, c, min(k, n)))
        pa.append(pass_at_k(n, c, min(k, n)))

    acc = sum(1 for r in rows if r["success"]) / len(rows)
    print(f"\n条件 {args.condition}: 单次准确率 {acc:.3f}")
    print(f"  pass^{k} = {sum(ph)/len(ph):.3f}")
    print(f"  pass@{k} = {sum(pa)/len(pa):.3f}")
    print(f"  gap     = {(sum(pa)-sum(ph))/len(pa):.3f}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--template", default="t1_bugfix")
    p.add_argument("--condition", default="blind")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--temperature", type=float, default=0.9)
    asyncio.run(main_async(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
