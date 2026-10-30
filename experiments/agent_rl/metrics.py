"""指标：pass^k（稳定性）、pass@k（采样上限）、gap（训练空间）。

定义（写进报告的口径）：
- **pass^k**（τ-bench 口径，可靠性）：同一任务采样 k 次**全部成功**的概率。
  对每题用无偏估计 `C(c,k)/C(n,k)`（c 为成功次数，n 为采样次数），再对题目取均值。
- **pass@k**（Codex 口径，能力上限）：同一任务 k 次里**至少一次成功**的概率，
  无偏估计 `1 - C(n-c,k)/C(n,k)`。
- **gap = pass@k − pass^k**：reward 选得准时的提升空间。

n == k 时不取交集，pass^k 退化为「全成功计 1、否则计 0」的经验值；pass@k 退化为「至少一次成功」。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from math import comb
from pathlib import Path
from typing import Any


def _comb(n: int, k: int) -> int:
    if k > n:
        return 0
    return comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k 的无偏估计：n 次采样中成功 c 次，k 次全成功的概率。"""
    if n < k:
        raise ValueError(f"采样次数 n={n} 小于 k={k}")
    if c < k:
        return 0.0
    return _comb(c, k) / _comb(n, k)


def pass_at_k(n: int, c: int, k: int) -> float:
    """pass@k 的无偏估计：n 次采样中成功 c 次，k 次里至少一次成功的概率。"""
    if n < k:
        raise ValueError(f"采样次数 n={n} 小于 k={k}")
    if n - c < k:
        return 1.0
    return 1.0 - _comb(n - c, k) / _comb(n, k)


@dataclass
class TaskStat:
    task_id: str
    template: str
    difficulty: str
    n: int
    successes: int
    pass_hat_k: float = 0.0
    pass_at_k: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def gap(self) -> float:
        return self.pass_at_k - self.pass_hat_k


def load_episodes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def compute(episodes: list[dict[str, Any]], k: int) -> tuple[list[TaskStat], dict[str, Any]]:
    """按任务聚合，返回每题统计与总体汇总。"""
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        by_task[episode["task_id"]].append(episode)

    stats: list[TaskStat] = []
    for task_id, items in by_task.items():
        n = len(items)
        c = sum(1 for item in items if item.get("success"))
        effective_k = min(k, n)
        stat = TaskStat(
            task_id=task_id,
            template=items[0]["template"],
            difficulty=items[0]["difficulty"],
            n=n,
            successes=c,
            pass_hat_k=pass_hat_k(n, c, effective_k),
            pass_at_k=pass_at_k(n, c, effective_k),
        )
        stats.append(stat)

    stats.sort(key=lambda s: s.task_id)

    summary = {
        "k": k,
        "tasks": len(stats),
        "samples_per_task": sorted({s.n for s in stats}),
        "accuracy": _mean(s.accuracy for s in stats),
        "pass_hat_k": _mean(s.pass_hat_k for s in stats),
        "pass_at_k": _mean(s.pass_at_k for s in stats),
        "gap": _mean(s.gap for s in stats),
        "by_template": _group(stats, lambda s: s.template),
        "by_difficulty": _group(stats, lambda s: s.difficulty),
        "unstable_tasks": [
            {"task_id": s.task_id, "successes": s.successes, "n": s.n, "pass_hat_k": round(s.pass_hat_k, 3)}
            for s in stats
            if 0 < s.successes < s.n
        ],
    }
    return stats, summary


def _mean(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _group(stats: list[TaskStat], key) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[TaskStat]] = defaultdict(list)
    for stat in stats:
        buckets[key(stat)].append(stat)
    return {
        name: {
            "tasks": len(items),
            "accuracy": round(_mean(s.accuracy for s in items), 4),
            "pass_hat_k": round(_mean(s.pass_hat_k for s in items), 4),
            "pass_at_k": round(_mean(s.pass_at_k for s in items), 4),
            "gap": round(_mean(s.gap for s in items), 4),
        }
        for name, items in sorted(buckets.items())
    }


def failure_modes(episodes: list[dict[str, Any]]) -> dict[str, int]:
    """粗粒度失败归因：从最后的工具调用与错误信息推断。"""
    modes: dict[str, int] = defaultdict(int)
    for episode in episodes:
        if episode.get("success"):
            continue
        if episode.get("error"):
            modes["agent_error:" + str(episode["error"]).split(":")[0]] += 1
            continue
        tail = (episode.get("check_tail") or "").lower()
        calls = episode.get("tool_calls") or []
        if not calls:
            modes["no_tool_call"] += 1
        elif "timed out" in tail or "timeout" in tail:
            modes["timeout"] += 1
        elif "modulenotfound" in tail or "importerror" in tail:
            modes["broken_import"] += 1
        elif "assert" in tail or "failed" in tail:
            modes["wrong_answer"] += 1
        elif "syntaxerror" in tail:
            modes["syntax_error"] += 1
        else:
            modes["other"] += 1
    return dict(modes)


def format_report(summary: dict[str, Any], stats: list[TaskStat], modes: dict[str, int]) -> str:
    lines = [
        f"# 结果（k={summary['k']}，每任务 {summary['samples_per_task']} 次采样）",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 任务数 | {summary['tasks']} |",
        f"| 单次准确率 | {summary['accuracy']:.3f} |",
        f"| **pass^{summary['k']}**（稳定性） | **{summary['pass_hat_k']:.3f}** |",
        f"| **pass@{summary['k']}**（采样上限） | **{summary['pass_at_k']:.3f}** |",
        f"| **gap**（训练空间） | **{summary['gap']:.3f}** |",
        "",
        "## 按模板",
        "",
        "| 模板 | 题数 | 准确率 | pass^k | pass@k | gap |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, data in summary["by_template"].items():
        lines.append(
            f"| {name} | {data['tasks']} | {data['accuracy']:.3f} | {data['pass_hat_k']:.3f} | "
            f"{data['pass_at_k']:.3f} | {data['gap']:.3f} |"
        )
    lines += ["", "## 按难度", "", "| 难度 | 题数 | 准确率 | pass^k | pass@k | gap |", "| --- | --- | --- | --- | --- | --- |"]
    for name, data in summary["by_difficulty"].items():
        lines.append(
            f"| {name} | {data['tasks']} | {data['accuracy']:.3f} | {data['pass_hat_k']:.3f} | "
            f"{data['pass_at_k']:.3f} | {data['gap']:.3f} |"
        )
    if modes:
        lines += ["", "## 失败归因", "", "| 类型 | 次数 |", "| --- | --- |"]
        for name, count in sorted(modes.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {count} |")
    unstable = summary["unstable_tasks"]
    if unstable:
        lines += ["", f"## 不稳定任务（{len(unstable)} 个，部分成功）", ""]
        for item in unstable[:15]:
            lines.append(f"- `{item['task_id']}` {item['successes']}/{item['n']} → pass^k={item['pass_hat_k']}")
    return "\n".join(lines) + "\n"
