"""T8 长程任务的 k 次采样：给出可信的通过率区间。

单次实验的 33% vs 100% 置信区间太宽（n=3）。
本脚本每任务采样 k 次，计算：
- 单次通过率（点估计）
- Wilson 置信区间
- pass^k / pass@k / gap

用法：
    uv run python -m experiments.agent_rl.long_k --samples 3          # 开干预
    uv run python -m experiments.agent_rl.long_k --samples 3 --no-pressure  # 对照
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness")
sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness/src")

from experiments.agent_rl.run_long import ENABLE_PRESSURE, run_one  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间（小样本更可靠）。"""
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def pass_hat_k(n: int, c: int, k: int) -> float:
    if n < k or c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def pass_at_k(n: int, c: int, k: int) -> float:
    if n < k:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


async def main_async(args: argparse.Namespace) -> None:
    ENABLE_PRESSURE[0] = not args.no_pressure
    tag = "nopressure" if args.no_pressure else "pressure"

    tasks = [t for t in generate_all() if t.template == "t8_long_horizon"][: args.limit]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"long_k_{tag}.jsonl"
    if out.exists():
        out.unlink()

    print(f"=== T8 k={args.samples} 产出压力={'关' if args.no_pressure else '开'} ===")
    print(f"任务数={len(tasks)} 总运行={len(tasks)*args.samples} 上限步数={args.steps}\n")

    records: list[dict] = []
    started = time.time()

    for index, task in enumerate(tasks, 1):
        print(f"[{index}/{len(tasks)}] {task.task_id} ({task.meta['num_stages']} 阶段)", flush=True)
        for sample in range(args.samples):
            t0 = time.time()
            record = await run_one(task, args.steps, args.temperature, args.model)
            record["sample"] = sample
            record["pressure_enabled"] = not args.no_pressure
            records.append(record)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"    #{sample} {'OK  ' if record['success'] else 'FAIL'} "
                f"{record['steps']:2d} 步 {time.time()-t0:.0f}s",
                flush=True,
            )
        task_records = [r for r in records if r["task_id"] == task.task_id]
        print(f"    → {sum(1 for r in task_records if r['success'])}/{len(task_records)}\n", flush=True)

    # 汇总
    total = len(records)
    ok = sum(1 for r in records if r["success"])
    lo, hi = wilson_ci(ok, total)

    print("\n" + "=" * 60)
    print(f"总运行: {total}  成功: {ok}  单次通过率: {ok/total:.1%}")
    print(f"Wilson 95% CI: [{lo:.1%}, {hi:.1%}]")
    print()

    print("按任务:")
    by_task: dict[str, list[dict]] = {}
    for r in records:
        by_task.setdefault(r["task_id"], []).append(r)
    for task_id, rs in sorted(by_task.items()):
        c = sum(1 for r in rs if r["success"])
        n = len(rs)
        k = min(args.samples, n)
        print(
            f"  {task_id}: {c}/{n}  pass^{k}={pass_hat_k(n, c, k):.2f}  "
            f"pass@{k}={pass_at_k(n, c, k):.2f}  平均步数={sum(r['steps'] for r in rs)/n:.1f}"
        )

    # 整体 pass^k
    ks = [min(args.samples, len(rs)) for rs in by_task.values()]
    ph = sum(pass_hat_k(len(rs), sum(1 for r in rs if r["success"]), k) for rs, k in zip(by_task.values(), ks)) / len(by_task)
    pa = sum(pass_at_k(len(rs), sum(1 for r in rs if r["success"]), k) for rs, k in zip(by_task.values(), ks)) / len(by_task)
    print(f"\n整体 pass^{args.samples} = {ph:.3f}")
    print(f"整体 pass@{args.samples} = {pa:.3f}")
    print(f"gap = {pa - ph:.3f}")

    summary = {
        "mode": tag,
        "samples": args.samples,
        "runs": total,
        "successes": ok,
        "success_rate": round(ok / total, 3),
        "ci_low": round(lo, 3),
        "ci_high": round(hi, 3),
        "pass_hat_k": round(ph, 3),
        "pass_at_k": round(pa, 3),
        "gap": round(pa - ph, 3),
        "by_task": {
            tid: {
                "n": len(rs),
                "c": sum(1 for r in rs if r["success"]),
                "avg_steps": round(sum(r["steps"] for r in rs) / len(rs), 1),
                "avg_writes": round(
                    sum(r.get("pressure", {}).get("writes", 0) for r in rs) / len(rs), 1
                ),
            }
            for tid, rs in by_task.items()
        },
        "duration_s": round(time.time() - started, 1),
    }
    path = RESULTS / f"long_k_{tag}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-pressure", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
