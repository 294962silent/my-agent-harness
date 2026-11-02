"""循环打断器的离线评估：用已有轨迹模拟「如果当时有打断会怎样」。

无法重跑模型（成本高），但可以精确计算：
1. 每个轨迹里，打断会在第几步触发
2. 触发后，原本浪费的步数有多少被省下
3. 有多少轨迹能在打断点之前就改对（用「第一次 edit 的位置」近似）

这是「轻量、可解释」的评估，不夸大结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from experiments.agent_rl.loop_breaker import LoopBreaker

RESULTS = Path(__file__).resolve().parent / "results"


def simulate(episode: dict) -> dict:
    """模拟一条轨迹在打断器下的行为。"""
    calls = [c for c in episode.get("tool_calls", []) if c.get("phase") == "start"]
    tools = [c.get("tool", "?") for c in calls]
    args = [c.get("args") or {} for c in calls]

    if not tools:
        return {}

    breaker = LoopBreaker()
    trigger_step = None
    terminate_step = None
    first_edit = next((i for i, t in enumerate(tools) if t in ("edit", "write")), None)

    for index, tool in enumerate(tools):
        # 签名 = 工具名 + 关键参数（不是全部参数）
        # 语义重复的定义：「同一个工具 + 同一个目标」，而不是字节级完全相同
        raw = args[index] if index < len(args) else {}
        key_arg = raw.get("path") or raw.get("command") or raw.get("pattern") or ""
        signature = f"{tool}::{key_arg}"[:200]
        level, _ = breaker.observe(tool, signature)
        if level == "switch" and trigger_step is None:
            trigger_step = index
        if level == "terminate":
            terminate_step = index
            break

    saved_steps = (len(tools) - terminate_step - 1) if terminate_step is not None else 0

    return {
        "task_id": episode.get("task_id"),
        "success": episode.get("success"),
        "total_steps": len(tools),
        "trigger_step": trigger_step,
        "terminate_step": terminate_step,
        "saved_steps": saved_steps,
        "first_edit": first_edit,
        "edit_before_trigger": (
            first_edit is not None and trigger_step is not None and first_edit <= trigger_step
        ),
    }


def main() -> int:
    episodes = []
    for name in ("episodes_main_k5.jsonl", "episodes_t7.jsonl"):
        path = RESULTS / name
        if path.exists():
            episodes += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    results = [simulate(e) for e in episodes]
    results = [r for r in results if r]

    n = len(results)
    triggered = [r for r in results if r["trigger_step"] is not None]
    terminated = [r for r in results if r["terminate_step"] is not None]
    saved = sum(r["saved_steps"] for r in results)

    print(f"=== 循环打断器离线评估（{n} 条轨迹）===")
    print()
    print(f"触发过提示/换工具的轨迹: {len(triggered)} ({len(triggered)/n:.1%})")
    print(f"被判定终止的轨迹:       {len(terminated)} ({len(terminated)/n:.1%})")
    print(f"总节省步数:             {saved}")
    if terminated:
        print(f"平均节省（终止的轨迹）:  {saved/max(len(terminated),1):.1f} 步")
    if triggered:
        avg_trigger = sum(r["trigger_step"] for r in triggered) / len(triggered)
        print(f"平均触发位置:           第 {avg_trigger:.1f} 步")
    print()

    # 关键问题：打断会不会误伤「本来能成功」的轨迹？
    print("=== 误伤分析 ===")
    success_triggered = [r for r in triggered if r["success"]]
    print(f"成功轨迹中被触发的:      {len(success_triggered)} / {sum(1 for r in results if r['success'])}")
    edit_before = [r for r in success_triggered if r["edit_before_trigger"]]
    print(f"其中「触发前已改文件」:   {len(edit_before)}")

    print()
    print("=== 样例（被终止的轨迹）===")
    for r in terminated[:5]:
        print(
            f"  {r['task_id']}: 共 {r['total_steps']} 步, "
            f"触发于第 {r['trigger_step']} 步, 终止于第 {r['terminate_step']} 步, "
            f"节省 {r['saved_steps']} 步, 首次改文件={r['first_edit']}"
        )

    out = RESULTS / "loop_breaker_eval.json"
    out.write_text(
        json.dumps(
            {
                "trajectories": n,
                "triggered": len(triggered),
                "trigger_rate": round(len(triggered) / n, 3),
                "terminated": len(terminated),
                "terminate_rate": round(len(terminated) / n, 3),
                "total_saved_steps": saved,
                "avg_saved_per_terminated": round(saved / max(len(terminated), 1), 1),
                "success_triggered": len(success_triggered),
                "success_edit_before_trigger": len(edit_before),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
