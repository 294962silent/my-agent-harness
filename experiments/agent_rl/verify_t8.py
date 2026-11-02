"""验证长程任务：初始必失败 + 参考解法必通过。

用 host backend + uv 环境运行（Docker 不可用时）。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness")
sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness/src")

from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401
from experiments.agent_rl.tasks.t8_solutions import SOLUTIONS  # noqa: E402
from experiments.agent_rl.tasks.t8b_solutions import T8B_SOLUTIONS  # noqa: E402
from experiments.agent_rl.tasks.t8c_solutions import T8C_SOLUTIONS  # noqa: E402
from experiments.agent_rl.tasks.t8d_solutions import T8D_SOLUTIONS  # noqa: E402

ALL_SOLUTIONS = {**SOLUTIONS, **T8B_SOLUTIONS, **T8C_SOLUTIONS, **T8D_SOLUTIONS}


def run_pytest(workspace: Path) -> tuple[int, str]:
    """用 uv run 跑 pytest（保证能找到 pytest）。"""
    proc = subprocess.run(
        ["uv", "run", "python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=600,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    tasks = [t for t in generate_all() if "long_horizon" in t.template]
    ok_count = 0
    for task in tasks:
        print(f"\n=== {task.task_id} {task.meta['num_stages']} 阶段 ===")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            task.materialize(ws)

            code1, out1 = run_pytest(ws)
            print(f"  初始状态: exit={code1} {'✓失败(正确)' if code1 != 0 else '✗竟然通过'}")

            # 应用参考解法
            for rel, content in ALL_SOLUTIONS[task.task_id].items():
                target = ws / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            code2, out2 = run_pytest(ws)
            print(f"  参考解法: exit={code2} {'✓通过' if code2 == 0 else '✗未通过'}")
            if code2 != 0:
                fails = [line for line in out2.splitlines() if "FAILED" in line or "Error" in line]
                for line in fails[:5]:
                    print(f"      {line[:100]}")

            if code1 != 0 and code2 == 0:
                ok_count += 1

    print(f"\n验证通过: {ok_count}/{len(tasks)}")
    return 0 if ok_count == len(tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
