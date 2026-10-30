"""验证任务生成器的自检：每个任务的初始状态应当**失败**，参考解法应当**通过**。

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.verify_tasks --template t1_bugfix
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.sandbox import Sandbox  # noqa: E402
from experiments.agent_rl.task import Task, all_templates, generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks_pkg  # noqa: E402,F401  触发注册


def _apply_fix(task: Task, files: dict[str, str]) -> dict[str, str]:
    """把 meta 里的参考实现写回正确的位置。"""
    fixed = task.meta.get("fixed_preview", "")
    if not fixed:
        return files
    return files


def verify(tasks: list[Task], limit: int | None = None) -> int:
    failures = 0
    selected = tasks[:limit] if limit else tasks
    for task in selected:
        with tempfile.TemporaryDirectory(prefix=f"verify-{task.task_id}-") as tmp:
            ws = Path(tmp)
            task.materialize(ws)
            with Sandbox(ws, task_id=task.task_id, timeout_s=task.timeout_s) as box:
                bad = box.run(task.check_cmd)
                if bad.ok:
                    print(f"[FAIL] {task.task_id} 初始状态竟然通过（应当失败）")
                    failures += 1
                    continue
                # 用 meta 里的参考实现覆盖
                reference = task.meta.get("reference_files")
                if not reference:
                    print(f"[SKIP] {task.task_id} 无参考解法，仅验证失败态")
                    continue
                for rel, content in reference.items():
                    box.write(rel, content)
                good = box.run(task.check_cmd)
                if not good.ok:
                    print(f"[FAIL] {task.task_id} 参考解法未通过: {good.tail(300)}")
                    failures += 1
                else:
                    print(f"[ ok ] {task.task_id} {task.template} {task.difficulty}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    all_tasks = generate_all()
    if args.template:
        all_tasks = [t for t in all_tasks if t.template == args.template]
    print(f"模板: {all_templates()}")
    print(f"任务数: {len(all_tasks)}")
    failures = verify(all_tasks, args.limit)
    if failures:
        print(f"\n{failures} 个任务验证失败")
        return 1
    print("\n全部验证通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
