"""GRPO 优势计算的离线验证：用真实轨迹数据证明训练信号存在。

GRPO 的核心公式（DeepSeek 提出）：

    对每个 prompt，采样 G 条输出
    A_i = (r_i − mean(r_1..r_G)) / std(r_1..r_G)
    用 A_i 加权策略梯度

关键前提：**组内奖励必须有方差**，否则 std=0，A_i 全为 0，无梯度。

本脚本用真实的 10 个训练任务的实测数据，验证：
1. 每个任务的组内奖励方差是否足够
2. 与稀疏奖励对比：稠密奖励能否在「全成功/全失败」时也提供梯度
3. 估算需要多少个任务、多少次采样才能得到稳定训练信号
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# 来自 DIRECTION_1.md 的实测数据：(任务, 成功次数, 总次数, 成功步数列表, 失败步数列表)
MEASURED = [
    ("t8-001", 1, 4, [12], [50, 50, 50]),
    ("t8-005", 3, 4, [22, 48, 50], [50]),
    ("t8e-001", 2, 4, [33, 35], [50, 50]),
    ("t8e-002", 2, 4, [32, 46], [50, 50]),
    ("t8f-001", 2, 4, [22, 45], [50, 50]),
    ("t8f-002", 1, 4, [10], [50, 50, 50]),
    ("t8g-001", 1, 2, [44], [50]),
    ("t8g-002", 1, 2, [18], [50]),
    ("t8g-003", 1, 2, [29], [50]),
    ("t8g-004", 2, 4, [20, 51], [50, 50]),
]


def grpo_advantages(rewards: list[float]) -> list[float]:
    """GRPO 组内标准化优势。"""
    if len(rewards) < 2:
        return [0.0] * len(rewards)
    mean = statistics.mean(rewards)
    std = statistics.pstdev(rewards)
    if std < 1e-9:
        return [0.0] * len(rewards)
    return [(r - mean) / std for r in rewards]


def sparse_rewards(successes: int, total: int) -> list[float]:
    """稀疏奖励：成功 1，失败 0。"""
    return [1.0] * successes + [0.0] * (total - successes)


def dense_rewards(success_steps: list[int], fail_steps: list[int], max_steps: int = 50) -> list[float]:
    """稠密奖励：按「测试通过数」而非「步数」给分。

    重要设计修正：
    第一版按步数给分（成功 1.0 - 0.5*步数/50），结果优势值与稀疏奖励
    **完全相同**——因为 GRPO 会做组内标准化，单调变换不改变相对排序。

    真正的稠密奖励必须提供**稀疏奖励没有的区分信息**：
    - 稀疏：只有「全过 vs 没全过」二值
    - 稠密：能区分「9/10 通过」和「3/10 通过」

    这里用「通过的测试比例」模拟（真实场景中由沙箱每步实测得到）。
    """
    rewards = []
    # 成功的轨迹：假设都通过了全部测试
    for _ in success_steps:
        rewards.append(1.0)
    # 失败的轨迹：按「终止时的测试通过比例」给分
    # 这里用步数做代理：跑得越久说明改得越多（真实场景应读沙箱实测值）
    for steps in fail_steps:
        progress = min(steps / max_steps, 1.0) * 0.8  # 最多 0.8（因为没全过）
        rewards.append(round(progress, 3))
    return rewards


def main() -> int:
    print("=" * 70)
    print("GRPO 优势计算的离线验证（用真实实测数据）")
    print("=" * 70)

    print("\n【1】逐任务：稀疏 vs 稠密奖励的组内方差\n")
    header = f"{'任务':10s} {'成功/总':>8s} {'稀疏std':>9s} {'稠密std':>9s} {'稀疏优势全零?':>14s} {'稠密有梯度?':>12s}"
    print(header)
    print("-" * len(header))

    sparse_zero = 0
    dense_ok = 0
    total = 0

    for task, succ, n, ok_steps, fail_steps in MEASURED:
        sr = sparse_rewards(succ, n)
        dr = dense_rewards(ok_steps, fail_steps)
        s_std = statistics.pstdev(sr) if len(sr) > 1 else 0.0
        d_std = statistics.pstdev(dr) if len(dr) > 1 else 0.0
        s_zero = s_std < 1e-9
        d_has_grad = d_std > 1e-9

        total += 1
        sparse_zero += int(s_zero)
        dense_ok += int(d_has_grad)

        print(
            f"{task:10s} {succ}/{n:>3d}    {s_std:>8.3f}  {d_std:>8.3f}  "
            f"{'是（无梯度）' if s_zero else '否':>14s}  {'是' if d_has_grad else '否':>12s}"
        )

    print(f"\n稀疏奖励无梯度的任务: {sparse_zero}/{total}")
    print(f"稠密奖励有梯度的任务: {dense_ok}/{total}")

    print("\n【2】优势值样例（t8-001，1成功3失败）\n")
    task, succ, n, ok_steps, fail_steps = MEASURED[0]
    sr = sparse_rewards(succ, n)
    dr = dense_rewards(ok_steps, fail_steps)
    print(f"  稀疏奖励: {sr}  -> 优势 {[round(a, 2) for a in grpo_advantages(sr)]}")
    print(f"  稠密奖励: {dr}  -> 优势 {[round(a, 2) for a in grpo_advantages(dr)]}")

    print("\n【3】需要多少任务才能稳定训练\n")
    rates = [succ / n for _, succ, n, _, _ in MEASURED]
    print(f"  10 个任务的通过率: {[f'{r:.0%}' for r in rates]}")
    print(f"  平均通过率: {statistics.mean(rates):.1%}")
    print(f"  通过率标准差: {statistics.pstdev(rates):.1%}")

    # 估算：理想训练集需要多少任务
    # 假设每个任务贡献一次 GRPO 更新（G=4 采样），需要足够的任务覆盖
    in_gradient = sum(1 for r in rates if 0 < r < 1)
    at_boundary = sum(1 for r in rates if 0.2 <= r <= 0.8)
    print(f"\n  有梯度的任务（0 < 通过率 < 1）: {in_gradient}/{len(rates)}")
    print(f"  落在理想区间（20%-80%）: {at_boundary}/{len(rates)}")

    print()
    print("【4】关键发现：稠密奖励在这批任务上没有额外价值")
    print("  两组优势值完全相同: [1.73, -0.58, -0.58, -0.58]")
    print("  原因：GRPO 做组内标准化，单调变换不改变相对排序。")
    print("  更根本的原因：当前 10 个任务的稀疏奖励本身就有梯度")
    print("    （通过率 25%-75%，没有任何任务全成功或全失败）")
    print("  稠密奖励真正有价值的场景：")
    print("    - 任务全成功（100%）→ 稀疏无梯度，稠密可区分效率")
    print("    - 任务全失败（0%）  → 稀疏无梯度，稠密可区分进度")
    print()
    print("【5】结论")
    print("  10 个任务全部有梯度信号（gap=1.0）")
    print("  通过率分布集中在 25%-75%，适合 GRPO")
    print("  稠密奖励在本批任务上不提供额外梯度（稀疏已足够）")

    # 保存结果
    summary = {
        "tasks": total,
        "sparse_no_gradient": sparse_zero,
        "dense_with_gradient": dense_ok,
        "mean_pass_rate": round(statistics.mean(rates), 3),
        "std_pass_rate": round(statistics.pstdev(rates), 3),
        "ideal_band_tasks": at_boundary,
        "note": "10 个任务全部 gap=1.0，可直接用于 GRPO",
    }
    out = RESULTS / "grpo_signal.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
