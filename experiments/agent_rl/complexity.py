"""量化 T8 长程任务的「工作量」：不看模型，只看任务的固有复杂度。

指标：
- 需要实现的函数数
- 需要的测试断言数
- 文件数 / 代码行数
- 最小改动量（参考解法的行数）

用途：为「预期步数」提供客观依据，而不是拍脑袋。
"""

from __future__ import annotations

import sys

sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness")
sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness/src")

from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401
from experiments.agent_rl.tasks.t8_solutions import SOLUTIONS  # noqa: E402


def analyze(task) -> dict:
    """分析一个任务的固有复杂度。"""
    test_files = {n: c for n, c in task.files.items() if n.startswith("tests/")}
    impl_files = {n: c for n, c in task.files.items() if n.endswith(".py") and not n.startswith("tests/")}
    solutions = SOLUTIONS.get(task.task_id, {})

    # 统计 TODO
    not_impl = sum(c.count("NotImplementedError") for c in impl_files.values())
    # 统计测试函数
    test_funcs = sum(c.count("def test_") for c in test_files.values())
    # 统计断言
    asserts = sum(c.count("assert ") for c in test_files.values())
    # 参考解法行数
    sol_lines = sum(len(c.strip().splitlines()) for c in solutions.values())
    # 初始代码行数
    init_lines = sum(len(c.strip().splitlines()) for c in impl_files.values())

    return {
        "task_id": task.task_id,
        "stages": task.meta.get("num_stages", 0),
        "impl_files": len(impl_files),
        "test_files": len(test_files),
        "todo_functions": not_impl,
        "test_functions": test_funcs,
        "assertions": asserts,
        "init_lines": init_lines,
        "solution_lines": sol_lines,
        # 粗估：每个待实现函数需要 2-3 步（读测试、查上下文、写代码、跑测试）
        "est_steps_low": not_impl * 2 + len(test_files),
        "est_steps_high": not_impl * 3 + len(test_files) * 2,
    }


def main() -> int:
    tasks = [t for t in generate_all() if t.template == "t8_long_horizon"]
    print("=== T8 长程任务复杂度分析 ===\n")
    rows = [analyze(t) for t in tasks]

    header = f"{'任务':10s} {'阶段':>4s} {'实现文件':>8s} {'测试文件':>8s} {'待实现函数':>10s} {'测试函数':>8s} {'断言':>6s} {'解法行数':>8s} {'预估步数':>10s}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['task_id']:10s} {r['stages']:>4d} {r['impl_files']:>8d} {r['test_files']:>8d} "
            f"{r['todo_functions']:>10d} {r['test_functions']:>8d} {r['assertions']:>6d} "
            f"{r['solution_lines']:>8d} {r['est_steps_low']:>4d}-{r['est_steps_high']:>4d}"
        )

    total_todo = sum(r["todo_functions"] for r in rows)
    total_sol = sum(r["solution_lines"] for r in rows)
    print(f"\n合计：{total_todo} 个待实现函数，{total_sol} 行参考解法")

    print("\n=== 对比现有任务 ===")
    others = [t for t in generate_all() if t.template != "t8_long_horizon"]
    avg_files = sum(len(t.files) for t in others) / len(others)
    print(f"  T1-T7 平均文件数: {avg_files:.1f}")
    print(f"  T8 平均文件数:    {sum(len(t.files) for t in tasks) / len(tasks):.1f}")
    print("  T1-T7 平均实测步数: 10.9")
    print(f"  T8 预估步数:      {sum(r['est_steps_low'] for r in rows)//len(rows)}-{sum(r['est_steps_high'] for r in rows)//len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
