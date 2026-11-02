"""过程指标：长程 Harness 工程的核心评测维度。

现有的 pass^k / pass@k 只衡量「最终成没成」，但长程任务需要看**过程**：

| 指标 | 定义 | 为什么重要 |
| --- | --- | --- |
| **早停率** | 未用尽最大步数就结束（但未完成）的比例 | 模型「放弃」的频率 |
| **循环次数** | 连续重复动作序列的最长长度 | 卡死后能否自救 |
| **恢复成功率** | 出错后多少步内恢复正常轨迹 | 纠错能力 |
| **时间视界** | 能保持有效行动的最长步数 | 长程稳定性上限 |
| **稠密奖励** | 每步的中间进度分，而非只有 0/1 | 训练信号密度 |

稠密奖励的构造（不引入 LLM 打分，保持可验证）：
- **测试通过率增量**：每步跑一次测试，通过数从 a 涨到 b，得分 +(b-a)
- **文件接近度**：改动的文件是否属于「应该改的文件」集合
- **无回归惩罚**：原来通过的测试变成失败，扣分
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"


@dataclass
class StepMetric:
    """单步的稠密奖励与状态。"""

    step: int
    tool: str
    reward: float
    tests_passed: int
    tests_failed: int
    is_error: bool
    is_repeat: bool
    note: str = ""


@dataclass
class ProcessMetrics:
    """一条轨迹的过程指标集合。"""

    task_id: str
    success: bool
    steps_used: int
    max_steps: int
    early_stop: bool
    longest_loop: int
    recovery_rate: float
    horizon: int
    dense_reward: float
    sparse_reward: float
    step_metrics: list[StepMetric] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "steps_used": self.steps_used,
            "max_steps": self.max_steps,
            "early_stop": self.early_stop,
            "longest_loop": self.longest_loop,
            "recovery_rate": round(self.recovery_rate, 3),
            "horizon": self.horizon,
            "dense_reward": round(self.dense_reward, 3),
            "sparse_reward": self.sparse_reward,
            "reward_ratio": round(self.dense_reward / max(self.sparse_reward, 1e-6), 3),
            "tool_counts": self.tool_counts,
            "steps": [
                {
                    "step": s.step,
                    "tool": s.tool,
                    "reward": round(s.reward, 3),
                    "passed": s.tests_passed,
                    "failed": s.tests_failed,
                    "error": s.is_error,
                    "repeat": s.is_repeat,
                }
                for s in self.step_metrics
            ],
        }


_PYTEST_RE = re.compile(r"(\d+) failed.*?(\d+) passed|(\d+) passed(?:, (\d+) failed)?|(\d+) error")


def parse_pytest_counts(output: str) -> tuple[int, int]:
    """从 pytest 输出里解析 (通过数, 失败数)。"""
    if not output:
        return 0, 0
    passed = failed = 0
    for match in re.finditer(r"(\d+) passed", output):
        passed = max(passed, int(match.group(1)))
    for match in re.finditer(r"(\d+) (?:failed|error)", output):
        failed = max(failed, int(match.group(1)))
    return passed, failed


def detect_loop(tools: list[str], min_len: int = 2) -> int:
    """检测最长的重复动作序列长度（循环次数）。

    例：['read','bash','read','bash','read','bash'] → 循环模式长度 2，重复 3 次
    """
    if not tools:
        return 0
    best = 0
    max_period = min(6, len(tools) // 2)
    for period in range(1, max_period + 1):
        repeats = 0
        for start in range(len(tools) - period):
            if tools[start : start + period] == tools[start + period : start + 2 * period]:
                repeats += 1
            else:
                if repeats:
                    best = max(best, repeats + 1)
                repeats = 0
        if repeats:
            best = max(best, repeats + 1)
    return best


def compute_process_metrics(
    episode: dict[str, Any],
    test_snapshots: list[tuple[int, int]] | None = None,
    max_steps: int = 20,
) -> ProcessMetrics:
    """从一条 episode 计算过程指标。

    test_snapshots: 每步的 (passed, failed)，若没有则用工具调用推断。
    """
    task_id = episode.get("task_id", "?")
    success = bool(episode.get("success"))
    calls = [c for c in episode.get("tool_calls", episode.get("trajectory", [])) if isinstance(c, dict)]
    tools = [c.get("tool") or c.get("name") or "?" for c in calls if c.get("phase", "start") == "start"]
    steps_used = len(tools)
    sparse = 1.0 if success else 0.0

    # 早停：步数明显少于上限却没成功
    early_stop = (not success) and steps_used < max_steps * 0.5

    # 循环检测
    longest_loop = detect_loop(tools)

    # 稠密奖励：基于测试通过数增量
    dense = 0.0
    step_metrics: list[StepMetric] = []
    prev_passed = 0
    prev_failed = 0
    if test_snapshots:
        for index, (passed, failed) in enumerate(test_snapshots):
            gain = max(0, passed - prev_passed)
            regress = max(0, failed - prev_failed) if prev_failed else 0
            step_reward = gain * 0.5 - regress * 0.3
            dense += step_reward
            step_metrics.append(
                StepMetric(
                    step=index,
                    tool=tools[index] if index < len(tools) else "?",
                    reward=step_reward,
                    tests_passed=passed,
                    tests_failed=failed,
                    is_error=failed > 0,
                    is_repeat=False,
                )
            )
            prev_passed, prev_failed = passed, failed
    else:
        # 没有测试快照时：用「改文件」给基础分，「跑测试」给探索分
        for index, tool in enumerate(tools):
            reward = 0.1 if tool in ("edit", "write") else 0.02
            if tool in ("edit", "write"):
                dense += reward
            step_metrics.append(
                StepMetric(
                    step=index,
                    tool=tool,
                    reward=reward,
                    tests_passed=0,
                    tests_failed=0,
                    is_error=False,
                    is_repeat=index > 0 and index < len(tools) - 1 and tools[index - 1] == tool,
                )
            )
        if success:
            dense += 1.0  # 完成给终局奖励

    # 恢复率：出错后 3 步内没再出错的占比
    errors = [i for i, s in enumerate(step_metrics) if s.is_error]
    recovered = sum(1 for i in errors if i + 3 < len(step_metrics) and not step_metrics[i + 3].is_error)
    recovery_rate = recovered / len(errors) if errors else 1.0

    # 时间视界：连续有效（非重复、非错误）步数的最长长度
    horizon = 0
    current = 0
    for s in step_metrics:
        if not s.is_error and not s.is_repeat:
            current += 1
            horizon = max(horizon, current)
        else:
            current = 0

    return ProcessMetrics(
        task_id=task_id,
        success=success,
        steps_used=steps_used,
        max_steps=max_steps,
        early_stop=early_stop,
        longest_loop=longest_loop,
        recovery_rate=recovery_rate,
        horizon=horizon,
        dense_reward=dense,
        sparse_reward=sparse,
        step_metrics=step_metrics,
        tool_counts=dict(Counter(tools)),
    )


def summarize(metrics: list[ProcessMetrics]) -> dict[str, Any]:
    """汇总一批轨迹的过程指标。"""
    if not metrics:
        return {}
    n = len(metrics)
    return {
        "episodes": n,
        "success_rate": round(sum(1 for m in metrics if m.success) / n, 3),
        "avg_steps": round(sum(m.steps_used for m in metrics) / n, 1),
        "early_stop_rate": round(sum(1 for m in metrics if m.early_stop) / n, 3),
        "avg_longest_loop": round(sum(m.longest_loop for m in metrics) / n, 2),
        "loop_rate": round(sum(1 for m in metrics if m.longest_loop >= 3) / n, 3),
        "avg_recovery_rate": round(sum(m.recovery_rate for m in metrics) / n, 3),
        "avg_horizon": round(sum(m.horizon for m in metrics) / n, 1),
        "avg_dense_reward": round(sum(m.dense_reward for m in metrics) / n, 3),
        "dense_sparse_ratio": round(
            sum(m.dense_reward for m in metrics) / max(sum(m.sparse_reward for m in metrics), 1e-6), 1
        ),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inputs",
        default="episodes_main_k5.jsonl,episodes_t7.jsonl",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--out", default=str(RESULTS / "process_metrics.json"))
    args = parser.parse_args()

    all_metrics: list[ProcessMetrics] = []
    for name in args.inputs.split(","):
        path = RESULTS / name.strip()
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                episode = json.loads(line)
                all_metrics.append(compute_process_metrics(episode, max_steps=args.max_steps))

    summary = summarize(all_metrics)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
