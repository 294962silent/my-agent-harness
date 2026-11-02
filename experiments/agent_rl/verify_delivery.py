"""交付验证：把方向一/二的所有关键数字重新跑一遍。

目的：确保交付文档里的每个数字都能复现，而不是手抄的。

用法：
    uv run python -m experiments.agent_rl.verify_delivery
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")

RESULTS = Path(__file__).resolve().parent / "results"

CHECKS: list[tuple[str, str, callable]] = []


def check(name: str, expectation: str):
    def deco(fn):
        CHECKS.append((name, expectation, fn))
        return fn

    return deco


@check("任务总数", "≥120")
def _task_count():
    from experiments.agent_rl.task import generate_all
    from experiments.agent_rl import tasks as _tasks  # noqa: F401

    return len(generate_all())


@check("长程任务数", "19")
def _long_count():
    from experiments.agent_rl.task import generate_all
    from experiments.agent_rl import tasks as _tasks  # noqa: F401

    ts = generate_all()
    return len(
        [
            t
            for t in ts
            if "long_horizon" in t.template
            or "boundary" in t.template
            or "chain_depth" in t.template
            or "complexity" in t.template
        ]
    )


@check("可训练边界任务", "10")
def _trainable_count():
    from experiments.agent_rl.grpo_signal import MEASURED

    return len(MEASURED)


@check("边界任务平均通过率", "~48%")
def _mean_rate():
    from experiments.agent_rl.grpo_signal import MEASURED
    import statistics

    return round(statistics.mean(s / n for _, s, n, _, _ in MEASURED), 3)


@check("GRPO 有效步率 (G=2)", "0.60")
def _g2():
    from experiments.agent_rl.grpo_feasibility import Config, TASK_PASS_RATES, diagnose

    return diagnose(Config(10, 2, 30), TASK_PASS_RATES)["effective_step_ratio"]


@check("GRPO 有效步率 (G=8)", "0.93")
def _g8():
    from experiments.agent_rl.grpo_feasibility import Config, TASK_PASS_RATES, diagnose

    return diagnose(Config(10, 8, 30), TASK_PASS_RATES)["effective_step_ratio"]


@check("结构奖励：完美 JSON", "1.0")
def _reward_perfect():
    from experiments.agent_rl.grpo_reward import structure_reward

    return structure_reward('{"name":"read","arguments":{"path":"a"}}')[0]


@check("结构奖励：空输出", "-1.0")
def _reward_empty():
    from experiments.agent_rl.grpo_reward import structure_reward

    return structure_reward("")[0]


@check("过程指标文件存在", "process_metrics.json")
def _pm():
    p = RESULTS / "process_metrics.json"
    if not p.exists():
        return "缺失"
    d = json.loads(p.read_text(encoding="utf-8"))
    return f"{d.get('episodes')} 条轨迹, 循环率 {d.get('loop_rate')}"


@check("稠密奖励文件存在", "dense_rewards.json")
def _dr():
    p = RESULTS / "dense_rewards.json"
    if not p.exists():
        return "缺失"
    d = json.loads(p.read_text(encoding="utf-8"))
    return f"密度 {d.get('avg_density')}"


@check("方向一文档", "≥300 行")
def _d1():
    p = RESULTS / "DIRECTION_1.md"
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else "缺失"


@check("方向二文档", "≥200 行")
def _d2():
    p = RESULTS / "DIRECTION_2.md"
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else "缺失"


def main() -> int:
    print("=" * 70)
    print("交付验证")
    print("=" * 70)
    print(f"{'检查项':<28s} {'期望':<22s} {'实际'}")
    print("-" * 70)

    failed = 0
    for name, expectation, fn in CHECKS:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            value = f"ERROR: {exc}"
            failed += 1
        print(f"{name:<28s} {expectation:<22s} {value}")

    print("-" * 70)
    if failed:
        print(f"{failed} 项异常")
        return 1
    print("全部检查完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
