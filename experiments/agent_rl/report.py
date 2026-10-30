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
        "| **合计** | **105** | **全部纯代码判定** |",
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


def section_strategies() -> list[str]:
    items = _load("strategy_full.jsonl") or _load("strategy_pilot.jsonl")
    if not items:
        return []
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_strategy[item["strategy"]].append(item)

    lines = [
        "## 五、策略对比",
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
        "## 六、预算扫描：找难度临界点",
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
        "## 七、训练数据产出",
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
    return [
        "## 八、结论",
        "",
        "1. **评测基建可用**：105 个任务、Docker 沙盒、纯代码判定，真实执行 500+ 次。",
        "2. **该模型在任务集上饱和**：k=5 采样下 pass^5 = pass@5 = 1.0，gap = 0，没有不稳定任务。",
        "3. **策略差异只体现在成本**：`best_of_3` 通过率与 `single` 相同，但耗时是 2.6 倍；"
        "`plan_first`、`reflexion` 同理。",
        "4. **预算呈阶跃**：模型 3–4 次调用即可完成，预算降到 2 直接归零，没有中间态。",
        "",
        "### 这些结论对我们意味着什么",
        "",
        "- **没有区分度的数据产生不了偏好信号**。当前偏好对只能靠「步数更少」这类弱信号构造，"
        "无法支撑 DPO/GRPO 训练。",
        "- **要产出可用训练数据，任务必须落在模型能力边界之外**：需要更长的上下文、"
        "更隐式的需求、或需要真实运行调试才能发现的问题。",
        "- **评测框架本身是资产**：换更强的模型或更难的任务集，这套流程可立即复用。",
        "",
        "## 九、复现",
        "",
        "```bash",
        "# 1. 构建沙盒镜像",
        "docker build -t my-agent-rl:py311 experiments/agent_rl/",
        "",
        "# 2. 生成任务并验证",
        "uv run python -m experiments.agent_rl.verify_tasks",
        "",
        "# 3. 单任务试跑",
        "uv run python -m experiments.agent_rl.try_one --task t4-001",
        "",
        "# 4. 批量采样（k=5）",
        "uv run python -m experiments.agent_rl.batch --stage main --samples 5 --concurrency 5",
        "",
        "# 5. 策略对比",
        "uv run python -m experiments.agent_rl.compare --limit 10 --strategies single,budget_4,best_of_3",
        "",
        "# 6. 预算扫描",
        "uv run python -m experiments.agent_rl.budget_sweep --limits 10 --budgets 1,2,3,4,6",
        "",
        "# 7. 生成偏好对与报告",
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
