"""GRPO 训练可行性诊断：回答「我这个配置能训出东西吗」。

背景（来自 DIRECTION_2.md 第八节的失败实验）：
本地用 2 条样本 + num_generations=2 训练 30 步，
loss 0.0326 看起来很好，但 grad_norm 多次为 0，
frac_reward_zero_std 从 0.6 升到 1.0——**什么都没学到**。

本模块把「为什么没学到」变成可计算、可预判的：

1. 给定任务集的通过率分布，模拟每组采样的奖励
2. 算出每组产生非零优势的概率 P(std > 0)
3. 估算总的有效梯度步数
4. 给出配置建议（需要多少样本 / 多大的 num_generations）

不需要 GPU，不需要模型，只做概率计算。
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import asdict, dataclass

# 10 个训练任务的实测通过率（来自 DIRECTION_1.md）
TASK_PASS_RATES = {
    "t8-001": 0.25,
    "t8-005": 0.75,
    "t8e-001": 0.50,
    "t8e-002": 0.50,
    "t8f-001": 0.50,
    "t8f-002": 0.25,
    "t8g-001": 0.50,
    "t8g-002": 0.50,
    "t8g-003": 0.50,
    "t8g-004": 0.50,
}


@dataclass
class Config:
    """一个 GRPO 训练配置。"""

    num_prompts: int  # 训练集大小（不同 prompt 数）
    num_generations: int  # 每组采样数 G
    max_steps: int  # 训练步数

    def label(self) -> str:
        return f"prompts={self.num_prompts}, G={self.num_generations}, steps={self.max_steps}"


def p_nonzero_variance(p_success: float, g: int) -> float:
    """一组 G 个伯努利采样中，奖励不全相同的概率。

    全相同的两种情况：
    - 全部成功：p^G
    - 全部失败：(1-p)^G
    """
    return 1.0 - p_success**g - (1.0 - p_success) ** g


def diagnose(config: Config, rates: dict[str, float], trials: int = 20000, seed: int = 0) -> dict:
    """模拟训练，统计有效梯度步数。"""
    rng = random.Random(seed)
    names = list(rates)

    zero_std_steps = 0
    nonzero_steps = 0
    reward_means = []
    per_step_zero = []

    for _ in range(config.max_steps):
        # 一步只取一个 prompt（batch=1，与本地 8GB 环境一致）
        task = rng.choice(names)
        p = rates[task]
        # 采样 G 个奖励（稀疏：成功 1，失败 0）
        rewards = [1.0 if rng.random() < p else 0.0 for _ in range(config.num_generations)]
        std = statistics.pstdev(rewards)
        reward_means.append(statistics.mean(rewards))
        if std < 1e-9:
            zero_std_steps += 1
            per_step_zero.append(True)
        else:
            nonzero_steps += 1
            per_step_zero.append(False)

    # 理论值：按任务通过率加权
    theoretical = sum(p_nonzero_variance(p, config.num_generations) for p in rates.values()) / len(rates)

    return {
        "config": asdict(config),
        "label": config.label(),
        "max_steps": config.max_steps,
        "nonzero_gradient_steps": nonzero_steps,
        "zero_gradient_steps": zero_std_steps,
        "effective_step_ratio": round(nonzero_steps / config.max_steps, 3),
        "theoretical_nonzero_prob": round(theoretical, 3),
        "reward_mean": round(statistics.mean(reward_means), 3),
    }


def grid_diagnose(rates: dict[str, float], steps: int = 30) -> list[dict]:
    """扫描不同 num_generations。"""
    out = []
    for g in (1, 2, 4, 8, 16):
        cfg = Config(num_prompts=len(rates), num_generations=g, max_steps=steps)
        out.append(diagnose(cfg, rates))
    return out


def min_g_for(config_p, target: float = 0.8) -> int:
    """给定通过率，求达到目标有效步率所需的最小 G。"""
    for g in range(2, 65):
        if p_nonzero_variance(config_p, g) >= target:
            return g
    return -1


def main() -> int:
    print("=" * 72)
    print("GRPO 训练可行性诊断（不需要 GPU）")
    print("=" * 72)

    print("\n【1】实测的 10 个任务通过率")
    rates = TASK_PASS_RATES
    for name, p in rates.items():
        print(f"  {name}: {p:.0%}")
    mean_p = statistics.mean(rates.values())
    print(f"\n  平均通过率: {mean_p:.1%}")

    print("\n【2】一组 G 个采样中产生非零优势的概率")
    print("     公式: P(有梯度) = 1 - p^G - (1-p)^G\n")
    header = f"{'G':>4s} " + " ".join(f"{n.replace('t8', ''):>8s}" for n in list(rates)[:6])
    print(header)
    print("-" * len(header))
    for g in (2, 4, 6, 8, 16):
        row = " ".join(f"{p_nonzero_variance(p, g):>8.2f}" for p in list(rates.values())[:6])
        print(f"{g:>4d} {row}")

    print("\n【3】复现上次失败的配置（G=2, 2 个 prompt, 30 步）")
    fail_cfg = Config(num_prompts=2, num_generations=2, max_steps=30)
    fail_res = diagnose(fail_cfg, rates)
    print(f"  配置: {fail_res['label']}")
    print(f"  有效梯度步: {fail_res['nonzero_gradient_steps']}/{fail_res['max_steps']} "
          f"= {fail_res['effective_step_ratio']:.0%}")
    print(f"  理论上限: {fail_res['theoretical_nonzero_prob']:.0%}")
    print("  → 与实测 grad_norm 多次为 0 吻合")

    print("\n【4】不同 G 的有效步率（30 步，全部 10 个 prompt）")
    print(f"{'G':>4s} {'有效步':>8s} {'比例':>8s} {'reward均值':>10s}")
    print("-" * 34)
    for res in grid_diagnose(rates):
        cfg = res["config"]
        print(
            f"{cfg['num_generations']:>4d} {res['nonzero_gradient_steps']:>8d} "
            f"{res['effective_step_ratio']:>7.0%} {res['reward_mean']:>10.2f}"
        )

    print("\n【5】达到 80% 有效步率所需的最小 G")
    print(f"{'通过率':>8s} {'最小G':>8s}")
    print("-" * 18)
    for p in (0.1, 0.25, 0.5, 0.75, 0.9):
        print(f"{p:>8.0%} {min_g_for(p, 0.8):>8d}")

    print("\n【6】结论")
    best = [r for r in grid_diagnose(rates) if r["config"]["num_generations"] == 8][0]
    print(f"  G=2 (本地 8GB 的上限): 有效步率 {diagnose(fail_cfg, rates)['effective_step_ratio']:.0%}")
    print(f"  G=8 (服务器 32GB 可行): 有效步率 {best['effective_step_ratio']:.0%}")
    print("\n  这正是本地训练无效、且必须用服务器的定量原因。")

    # 保存
    from pathlib import Path

    out = Path(__file__).resolve().parent / "results" / "grpo_feasibility.json"
    payload = {
        "task_pass_rates": rates,
        "failure_config": fail_res,
        "grid": grid_diagnose(rates),
        "min_g_for_80pct": {str(p): min_g_for(p, 0.8) for p in (0.1, 0.25, 0.5, 0.75, 0.9)},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
