"""生成最终报告：把各实验的结果汇总成一份可读的 REPORT.md。

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.report
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.metrics import compute, failure_modes  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def _load(name: str) -> list[dict[str, Any]]:
    path = RESULTS / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def section_overview() -> list[str]:
    return [
        "# Agent RL 实验报告",
        "",
        "> 模型：`deepseek-v4.1-flash`（网关唯一可用模型）· 沙盒：Docker（`my-agent-rl:py311`）",
        "> 任务：100 个可验证编码任务 + 5 个大型项目任务",
        "",
        "## 一、这套实验回答什么问题",
        "",
        "1. 用**可验证奖励**（纯代码判定，无 LLM 打分）构造 agent 轨迹数据；",
        "2. 用 **pass^k（稳定性）/ pass@k（采样上限）/ gap（训练空间）** 衡量同一个模型在不同策略下的表现；",
        "3. 对比 **7 种策略**（单次、预算受限 ×3、best-of-3、反思重试、先规划）的成功率与成本。",
        "",
    ]


def section_tasks() -> list[str]:
    lines = [
        "## 二、任务集",
        "",
        "| 模板 | 数量 | 判定方式 |",
        "| --- | --- | --- |",
        "| T1 bug 修复 | 20（10 easy + 10 hard） | pytest 退出码 |",
        "| T2 功能补全 | 20 | pytest 退出码 |",
        "| T3 多文件重构 | 20 | pytest + grep 无旧符号 |",
        "| T4 数据变换 | 20 | 输出文件逐字节比对 |",
        "| T5 CLI 工具 | 20 | shell 断言（输出 + 退出码） |",
        "| T6 大型项目 | 5 | pytest（多文件项目） |",
        "| T7 多缺陷 | 10（3–5 个独立 bug/题） | pytest（必须全部修复） |",
        "| **合计** | **115** | **全部纯代码判定** |",
        "",
        "难度倍增（可选）：隐藏测试 / 干扰文件 / 噪声代码，三级叠加，已验证加固后仍可解。",
        "",
    ]
    return lines


def section_calibration() -> list[str]:
    cal1 = _load("episodes_calibrate.jsonl")
    cal3 = _load("episodes_calibrate_l3.jsonl")
    if not cal1 and not cal3:
        return []
    lines = ["## 三、难度校准", "", "| 版本 | 样本 | 通过率 | 中位耗时 | 中位步数 |", "| --- | --- | --- | --- | --- |"]
    for name, items in (("原始任务", cal1), ("加固 level 3", cal3)):
        if not items:
            continue
        acc = sum(1 for e in items if e.get("success")) / len(items)
        lines.append(
            f"| {name} | {len(items)} | {acc:.1%} | "
            f"{median(e.get('duration_s', 0) for e in items):.1f}s | "
            f"{median(e.get('steps', 0) for e in items):.0f} |"
        )
    lines.append("")
    lines.append("**结论**：加固（隐藏测试 + 干扰 + 噪声）没有降低通过率——模型是「读代码」能力强，不是「找文件」能力弱。")
    lines.append("")
    return lines


def section_k5() -> list[str]:
    items = _load("episodes_main_k5.jsonl")
    if not items:
        return []
    stats, summary = compute(items, k=5)
    modes = failure_modes(items)
    lines = [
        "## 四、主实验：k=5 采样的一致性",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 轨迹条数 | {len(items)} |",
        f"| 任务数 | {summary['tasks']} |",
        f"| 单次准确率 | {summary['accuracy']:.3f} |",
        f"| **pass^5（稳定性）** | **{summary['pass_hat_k']:.3f}** |",
        f"| **pass@5（采样上限）** | **{summary['pass_at_k']:.3f}** |",
        f"| **gap（训练空间）** | **{summary['gap']:.3f}** |",
        f"| 不稳定任务（部分成功） | {len(summary['unstable_tasks'])} |",
        "",
        "### 按模板",
        "",
        "| 模板 | 题数 | 准确率 | pass^k | pass@k | gap |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, data in summary["by_template"].items():
        lines.append(
            f"| {name} | {data['tasks']} | {data['accuracy']:.3f} | {data['pass_hat_k']:.3f} | "
            f"{data['pass_at_k']:.3f} | {data['gap']:.3f} |"
        )
    lines.append("")
    if modes:
        lines.append(f"失败归因：{modes}")
        lines.append("")
    return lines


def section_t7() -> list[str]:
    items = _load("episodes_t7.jsonl")
    if not items:
        return []
    items = [x for x in items if x.get("template") == "t7_multi_bug"]
    if not items:
        return []
    from collections import defaultdict as _dd

    by_task = _dd(list)
    for item in items:
        by_task[item["task_id"]].append(bool(item.get("success")))
    acc = sum(1 for x in items if x.get("success")) / len(items)
    lines = [
        "## 五、多缺陷任务（制造部分成功）",
        "",
        "设计动机：单点 bug 一次就能改对，无区分度。把 3–5 个**互不相关**的 bug 放进同一模块，"
        "只修一个其余测试仍失败——理论上会产生部分成功。",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 任务数 | {len(by_task)} |",
        f"| 采样次数 | {len(items)}（每题 3 次） |",
        f"| 通过率 | {acc:.1%} |",
        f"| 出现部分成功的任务 | {sum(1 for v in by_task.values() if 0 < sum(v) < len(v))} |",
        "",
    ]
    return lines


def section_strategies() -> list[str]:
    items = _load("strategy_full.jsonl") or _load("strategy_pilot.jsonl")
    if not items:
        return []
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_strategy[item["strategy"]].append(item)

    lines = [
        "## 六、策略对比",
        "",
        "| 策略 | 样本 | 通过率 | 平均耗时 | 平均步数 | 相对 single 成本 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    baseline_cost = None
    if "single" in by_strategy:
        baseline_cost = mean(x.get("duration_s", 0) for x in by_strategy["single"])

    order = ["single", "budget_2", "budget_4", "budget_8", "plan_first", "reflexion", "best_of_3"]
    for strategy in order:
        group = by_strategy.get(strategy)
        if not group:
            continue
        acc = sum(1 for x in group if x.get("success")) / len(group)
        avg_dur = mean(x.get("duration_s", 0) for x in group)
        avg_steps = mean(x.get("steps_total", 0) for x in group)
        ratio = f"{avg_dur / baseline_cost:.2f}×" if baseline_cost else "-"
        lines.append(
            f"| `{strategy}` | {len(group)} | {acc:.1%} | {avg_dur:.1f}s | {avg_steps:.1f} | {ratio} |"
        )
    lines.append("")
    return lines


def section_budget_sweep() -> list[str]:
    items = _load("budget_sweep_main.jsonl")
    if not items:
        return []
    by_budget: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_budget[item["budget"]].append(item)

    lines = [
        "## 七、预算扫描：找难度临界点",
        "",
        "| 预算（模型调用上限） | 通过率 | 完全通过任务 | 部分通过 | 全失败 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for budget in sorted(by_budget):
        group = by_budget[budget]
        by_task: dict[str, list[bool]] = defaultdict(list)
        for item in group:
            by_task[item["task_id"]].append(bool(item["success"]))
        rates = [sum(v) / len(v) for v in by_task.values()]
        acc = sum(1 for x in group if x.get("success")) / len(group)
        lines.append(
            f"| {budget} | {acc:.1%} | {sum(1 for r in rates if r == 1.0)} | "
            f"{sum(1 for r in rates if 0 < r < 1)} | {sum(1 for r in rates if r == 0.0)} |"
        )
    lines.append("")
    lines.append(
        "**关键观察**：通过率随预算呈**阶跃**而非渐变——模型用 3–4 次调用就能完成任务，"
        "预算减到 2 时直接归零。这说明该模型在此任务集上的能力远高于所需。"
    )
    lines.append("")
    return lines


def section_pairs() -> list[str]:
    path = RESULTS / "dataset_stats.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        "## 八、训练数据产出",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 轨迹条数 | {data['episodes']} |",
        f"| 覆盖任务 | {data['tasks']} |",
        f"| 偏好对 | {data['pairs']} |",
        f"| 偏好对构成 | {data['pairs_by_rule']} |",
        f"| 有正负样本变化的任务 | {data['tasks_with_variation']} |",
        f"| 总步数 | {data['total_steps']} |",
        f"| token（入/出） | {data['total_tokens_in']}/{data['total_tokens_out']} |",
        "",
        "导出格式：`trajectories_verl.jsonl`（prompt/response/reward，可直接喂 verl、OpenRLHF）。",
        "",
    ]


def section_findings() -> list[str]:
    total = 0
    for name in (
        "episodes_smoke.jsonl",
        "episodes_calibrate.jsonl",
        "episodes_calibrate_l3.jsonl",
        "episodes_main_k5.jsonl",
        "episodes_t7.jsonl",
        "strategy_pilot.jsonl",
        "strategy_full.jsonl",
        "budget_sweep_main.jsonl",
    ):
        total += len(_load(name))

    return [
        "## 九、结论",
        "",
        f"**累计 {total} 次真实沙盒执行**（115 个任务，DeepSeek V4.1 Flash，Docker 隔离）。",
        "",
        "### 1. 模型在任务集上饱和",
        "",
        "- 100 题单次通过率 **100%**；k=5 采样下 **pass^5 = pass@5 = 1.0，gap = 0**；",
        "- 加固（隐藏测试 + 干扰文件 + 噪声代码）**没有降低**通过率（100% → 100%，中位耗时 25.9s → 45.0s）；",
        "- 多缺陷任务（一题 3–5 个独立 bug）**仍然 100%** 通过；",
        "- 大型项目任务（8 文件跨模块）**也全部通过**。",
        "",
        "### 2. 策略差异只体现在成本",
        "",
        "| 策略 | 通过率 | 相对成本 | 判断 |",
        "| --- | --- | --- | --- |",
        "| `single` | 100% | 1.00× | 基准 |",
        "| `budget_4` / `budget_8` | 100% | 0.84× / 1.00× | **性价比最高** |",
        "| `plan_first` | 100% | 1.14× | 同等成功率，更贵 |",
        "| `reflexion` | 100% | 1.10× | 从未触发重试（一次就过） |",
        "| `best_of_3` | 100% | **2.46×** | 完全浪费 |",
        "",
        "### 3. 预算呈阶跃，没有中间态",
        "",
        "模型用 **3–4 次调用**就能完成任务；预算降到 2 时通过率直接归零。"
        "不存在「预算 3.5 时一半成功」的连续区——说明能力远高于需求。",
        "",
        "### 4. 对 Agent RL 的意义",
        "",
        "**数据饱和是 RL 训练的第一障碍。** 当前任务集上 `gap = 0`，意味着：",
        "",
        "- 无法构造**成功 vs 失败**偏好对（没有失败样本）；",
        "- `rejection sampling` 无效（采样 5 次全对，无需拒绝）；",
        "- 只能退化为「步数更少」这类**弱偏好信号**（本次生成 100 对，全部来自该规则）。",
        "",
        "要产出可训练数据，任务必须落在模型能力边界之外：更长的上下文、更隐式的需求、"
        "或必须真实运行调试才能发现的问题——而不是靠隐藏测试或增加 bug 数量。",
        "",
        "### 5. 这套基建的价值",
        "",
        "换更强的模型或更难的任务集，这套流程可**立即复用**：任务生成 → 沙盒执行 → 轨迹采集 → "
        "可验证奖励 → 偏好对导出 → 策略对比，全部一条命令。",
        "",
        "## 十、复现",
        "",
        "```bash",
        "# 1. 构建沙盒镜像（需 Docker）",
        "docker build -t my-agent-rl:py311 experiments/agent_rl/",
        "",
        "# 2. 验证任务可解性（初始必失败 + 参考解法必通过）",
        "uv run python -m experiments.agent_rl.verify_tasks",
        "",
        "# 3. 单任务试跑",
        "uv run python -m experiments.agent_rl.try_one --task t4-001",
        "",
        "# 4. 批量采样（k=5，100 题）",
        "uv run python -m experiments.agent_rl.batch --stage main --samples 5 --concurrency 5",
        "",
        "# 5. 策略对比（7 种）",
        "uv run python -m experiments.agent_rl.compare --limit 10 \\",
        "    --strategies single,budget_2,budget_4,budget_8,best_of_3,reflexion,plan_first",
        "",
        "# 6. 预算扫描",
        "uv run python -m experiments.agent_rl.budget_sweep --limits 10 --budgets 1,2,3,4,6",
        "",
        "# 7. 指标与报告",
        "uv run python -m experiments.agent_rl.pairs",
        "uv run python -m experiments.agent_rl.report",
        "```",
        "",
    ]


def main() -> int:
    parts = [
        section_overview(),
        section_tasks(),
        section_calibration(),
        section_k5(),
        section_t7(),
        section_strategies(),
        section_budget_sweep(),
        section_pairs(),
        section_findings(),
    ]
    text = "\n".join("\n".join(p) for p in parts if p)
    out = RESULTS / "REPORT.md"
    out.write_text(text, encoding="utf-8")
    print(f"报告已生成: {out}")
    print(f"字符数: {len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
