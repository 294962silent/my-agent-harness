"""从轨迹生成 DPO 偏好对，并导出训练用数据集。

偏好对的构造规则（可验证奖励驱动）：
1. **成功 vs 失败**：同一任务的成功轨迹（chosen）对失败轨迹（rejected）——最干净的信号。
2. **步数效率**：同一任务两条都成功时，步数少者为 chosen（效率偏好）。
3. **token 效率**：都成功且步数相同时，token 少者为 chosen。

没有失败样本时（当前实验全部成功），退化为 2/3 类——这也说明一个重要结论：
**只有成功样本的数据集无法提供偏好信号**，必须造出模型失败的任务。

输出：
- `pairs.jsonl`：每行 `{prompt, chosen, rejected, task_id, rule, margin}`
- `dataset_stats.json`：规模与覆盖统计
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

RESULTS = Path(__file__).resolve().parent / "results"


def _trajectory_text(episode: dict[str, Any], limit: int = 6000) -> str:
    """把一条轨迹压成可训练的文本：目标 + 关键工具调用 + 最终结论。"""
    parts = []
    for message in episode.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(f"[{role}] {content.strip()}")
        if len("\n".join(parts)) > limit:
            break
    return "\n".join(parts)[:limit]


def build_pairs(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        by_task[episode["task_id"]].append(episode)

    pairs: list[dict[str, Any]] = []
    for task_id, items in by_task.items():
        success = [e for e in items if e.get("success")]
        failure = [e for e in items if not e.get("success")]

        # 规则 1：成功 vs 失败
        for good in success:
            for bad in failure[:1]:
                pairs.append(
                    {
                        "task_id": task_id,
                        "rule": "success_vs_failure",
                        "chosen": _trajectory_text(good),
                        "rejected": _trajectory_text(bad),
                        "margin": 1.0,
                        "chosen_steps": good.get("steps"),
                        "rejected_steps": bad.get("steps"),
                    }
                )

        # 规则 2/3：都成功时偏好更省的那条
        if len(success) >= 2:
            best = min(success, key=lambda e: (e.get("steps", 999), e.get("tokens_out", 999)))
            worst = max(success, key=lambda e: (e.get("steps", 0), e.get("tokens_out", 0)))
            if best is not worst:
                rule = "fewer_steps" if best.get("steps") != worst.get("steps") else "fewer_tokens"
                pairs.append(
                    {
                        "task_id": task_id,
                        "rule": rule,
                        "chosen": _trajectory_text(best),
                        "rejected": _trajectory_text(worst),
                        "margin": 0.3,
                        "chosen_steps": best.get("steps"),
                        "rejected_steps": worst.get("steps"),
                    }
                )
    return pairs


def to_verl_view(episode: dict[str, Any]) -> dict[str, Any]:
    """导出 verl/OpenRLHF 常见字段：prompt / response / reward。"""
    messages = episode.get("messages", [])
    prompt = ""
    response = ""
    for message in messages:
        if message.get("role") == "user" and not prompt:
            prompt = str(message.get("content") or "")
        elif message.get("role") == "assistant":
            response += str(message.get("content") or "") + "\n"
    return {
        "task_id": episode["task_id"],
        "prompt": prompt,
        "response": response.strip()[:8000],
        "reward": episode.get("reward", 0.0),
        "meta": {
            "template": episode.get("template"),
            "difficulty": episode.get("difficulty"),
            "steps": episode.get("steps"),
            "model": episode.get("model"),
        },
    }


def stats(episodes: list[dict[str, Any]], pairs: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        by_task[episode["task_id"]].append(episode)

    rules: dict[str, int] = defaultdict(int)
    for pair in pairs:
        rules[pair["rule"]] += 1

    return {
        "episodes": len(episodes),
        "tasks": len(by_task),
        "success_rate": round(sum(1 for e in episodes if e.get("success")) / max(1, len(episodes)), 4),
        "samples_per_task": sorted({len(v) for v in by_task.values()}),
        "pairs": len(pairs),
        "pairs_by_rule": dict(rules),
        "tasks_with_variation": sum(1 for v in by_task.values() if len({e.get("success") for e in v}) > 1),
        "total_steps": sum(e.get("steps", 0) for e in episodes),
        "total_tokens_in": sum(e.get("tokens_in", 0) for e in episodes),
        "total_tokens_out": sum(e.get("tokens_out", 0) for e in episodes),
    }


def main() -> int:
    files = sys.argv[1:] or [
        str(RESULTS / "episodes_main_k5.jsonl"),
        str(RESULTS / "strategy_pilot.jsonl"),
    ]
    episodes: list[dict[str, Any]] = []
    for path_str in files:
        path = Path(path_str)
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    episodes.append(json.loads(line))

    if not episodes:
        print("没有可用的轨迹")
        return 1

    pairs = build_pairs(episodes)
    RESULTS.mkdir(parents=True, exist_ok=True)

    with (RESULTS / "pairs.jsonl").open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")

    with (RESULTS / "trajectories_verl.jsonl").open("w", encoding="utf-8") as fh:
        for episode in episodes:
            fh.write(json.dumps(to_verl_view(episode), ensure_ascii=False) + "\n")

    summary = stats(episodes, pairs)
    (RESULTS / "dataset_stats.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
