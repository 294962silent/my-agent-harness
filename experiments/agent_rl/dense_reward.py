"""稠密奖励评测：每步检查中间 artifact，而非只看最终成败。

方向一（长程 Harness 工程 + 稠密奖励评测）的核心组件。

问题：现有评测只有 0/1 的终局判定。在模型 100% 通过时，奖惩信号完全饱和，
既无法区分「一次做对」和「撞了 10 次才做对」，也无法用于训练。

稠密奖励的来源（全部可验证，不用 LLM 打分）：
1. **测试通过率增量**：每步跑一次测试，通过数从 a 涨到 b → +(b-a)/total
2. **回归惩罚**：原本通过的测试变成失败 → -0.3/个
3. **目标文件命中**：改的文件是否在「应该改的文件」集合里 → +0.2
4. **无效动作惩罚**：重复动作、报错的 bash → -0.1
5. **终局奖励**：全过 +1.0

产出：
- 每条轨迹的逐步奖励序列（可用于 GRPO 的过程奖励）
- 密集度指标：非零奖励步数占比
- 与稀疏奖励的相关性：稠密分高的轨迹，最终是否也更可能成功
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"


@dataclass
class DenseStep:
    """单步的稠密奖励分解。"""

    step: int
    tool: str
    test_gain: float = 0.0
    regression: float = 0.0
    target_hit: float = 0.0
    wasted: float = 0.0
    total: float = 0.0
    tests_passed: int = 0
    tests_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "gain": round(self.test_gain, 3),
            "regression": round(self.regression, 3),
            "target_hit": round(self.target_hit, 3),
            "wasted": round(self.wasted, 3),
            "reward": round(self.total, 3),
            "passed": self.tests_passed,
            "total": self.tests_total,
        }


@dataclass
class DenseTrajectory:
    """一条轨迹的稠密奖励。"""

    task_id: str
    success: bool
    steps: list[DenseStep] = field(default_factory=list)
    final_reward: float = 0.0

    @property
    def dense_total(self) -> float:
        return sum(s.total for s in self.steps)

    @property
    def non_zero_steps(self) -> int:
        return sum(1 for s in self.steps if abs(s.total) > 1e-6)

    @property
    def density(self) -> float:
        """非零奖励步数占比：衡量信号密集程度。"""
        return self.non_zero_steps / len(self.steps) if self.steps else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "dense_total": round(self.dense_total, 3),
            "sparse_reward": 1.0 if self.success else 0.0,
            "density": round(self.density, 3),
            "non_zero_steps": self.non_zero_steps,
            "total_steps": len(self.steps),
            "steps": [s.to_dict() for s in self.steps],
        }


def compute_dense_rewards(
    episode: dict[str, Any],
    test_progression: list[tuple[int, int]] | None = None,
    target_files: set[str] | None = None,
    total_tests: int = 10,
) -> DenseTrajectory:
    """计算一条轨迹的稠密奖励。

    test_progression: 每步的 (通过数, 失败数)。若为 None，用工具调用推断。
    target_files: 应该修改的文件集合（来自参考解法或 PR diff）。
    """
    task_id = episode.get("task_id", "?")
    success = bool(episode.get("success"))
    calls = episode.get("tool_calls", episode.get("trajectory", []))
    calls = [c for c in calls if isinstance(c, dict) and c.get("phase", "start") == "start"]
    tools = [c.get("tool") or c.get("name") or "?" for c in calls]
    args = [c.get("args") or c.get("arguments") or {} for c in calls]

    steps: list[DenseStep] = []
    prev_passed = 0

    for index, tool in enumerate(tools):
        step = DenseStep(step=index, tool=tool)
        total = max(total_tests, 1)

        # 1. 测试通过率增量
        if test_progression and index < len(test_progression):
            passed, failed = test_progression[index]
            step.tests_passed = passed
            step.tests_total = total
            gain = max(0, passed - prev_passed)
            step.test_gain = gain / total
            if failed > 0 and prev_passed > 0 and failed > 0:
                step.regression = -0.3 * min(failed, 3) / total
            prev_passed = max(prev_passed, passed)
        else:
            # 无测试快照：按动作的信息价值给分
            #   read/ls/find/grep = 探索（低分）
            #   edit/write = 进展（高分）
            #   bash + pytest = 验证（中分，但重复验证无分）
            if tool in ("edit", "write"):
                step.test_gain = 0.4
            elif tool == "bash":
                cmd = str(args[index].get("command", "")) if index < len(args) else ""
                step.test_gain = 0.1 if "pytest" in cmd else 0.0
            elif tool in ("read", "ls", "find", "grep"):
                step.test_gain = 0.05

        # 2. 目标文件命中
        if target_files and tool in ("edit", "write"):
            path = str(args[index].get("path", "")) if index < len(args) else ""
            if any(path.endswith(t) or t.endswith(path) for t in target_files if path):
                step.target_hit = 0.2

        # 3. 无效动作
        if index > 0 and tools[index - 1] == tool:
            step.wasted = -0.02
        if tool == "bash":
            cmd = str(args[index].get("command", "")) if index < len(args) else ""
            if "pytest" in cmd and index > 2 and tools[index - 1] == "bash":
                step.wasted -= 0.03  # 连续跑测试但不改代码

        step.total = step.test_gain + step.regression + step.target_hit + step.wasted
        steps.append(step)

    trajectory = DenseTrajectory(task_id=task_id, success=success, steps=steps)
    # 终局奖励
    trajectory.final_reward = 1.0 if success else 0.0
    return trajectory


def summarize(trajectories: list[DenseTrajectory]) -> dict[str, Any]:
    """汇总稠密奖励的统计。"""
    if not trajectories:
        return {}
    n = len(trajectories)
    return {
        "trajectories": n,
        "success_rate": round(sum(1 for t in trajectories if t.success) / n, 3),
        "avg_dense_total": round(sum(t.dense_total for t in trajectories) / n, 3),
        "avg_density": round(sum(t.density for t in trajectories) / n, 3),
        "avg_steps": round(sum(len(t.steps) for t in trajectories) / n, 1),
        "avg_nonzero_steps": round(sum(t.non_zero_steps for t in trajectories) / n, 1),
        "dense_sparse_ratio": round(
            sum(t.dense_total for t in trajectories) / max(sum(1 for t in trajectories if t.success), 1), 2
        ),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", default="episodes_main_k5.jsonl,episodes_t7.jsonl")
    parser.add_argument("--out", default=str(RESULTS / "dense_rewards.json"))
    parser.add_argument("--sample-out", default=str(RESULTS / "dense_rewards_sample.jsonl"))
    args = parser.parse_args()

    trajectories: list[DenseTrajectory] = []
    for name in args.inputs.split(","):
        path = RESULTS / name.strip()
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    episode = json.loads(line)
                    trajectories.append(compute_dense_rewards(episode))

    summary = summarize(trajectories)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 保存样例供检查
    with open(args.sample_out, "w", encoding="utf-8") as fh:
        for traj in trajectories[:20]:
            fh.write(json.dumps(traj.to_dict(), ensure_ascii=False) + "\n")

    print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
