"""D1 冒烟：验证沙盒能在容器里跑通一次真实任务。

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.smoke
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.sandbox import Sandbox, docker_available  # noqa: E402


def main() -> int:
    print(f"docker 可用: {docker_available()}")

    with tempfile.TemporaryDirectory(prefix="arl-smoke-") as tmp:
        ws = Path(tmp)
        (ws / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
        )
        (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        with Sandbox(ws, task_id="smoke", timeout_s=120) as box:
            print(f"容器: {box.container}")

            # 1) 容器信息
            r = box.run(["python", "--version"])
            print(f"[1] python --version -> {r.stdout.strip()} (ok={r.ok})")

            # 2) 跑失败的单测（此时代码是错的）
            r = box.run(["python", "-m", "pytest", "-q"])
            print(f"[2] 改前 pytest -> exit={r.exit_code} ok={r.ok} 耗时={r.duration_s:.1f}s")
            assert not r.ok, "改前应当是失败的"

            # 3) 用 edit 的方式改代码（模拟 agent 修复）
            box.write("calc.py", "def add(a, b):\n    return a + b\n")

            # 4) 再跑一次，应当通过
            r = box.run(["python", "-m", "pytest", "-q"])
            print(f"[3] 改后 pytest -> exit={r.exit_code} ok={r.ok} 耗时={r.duration_s:.1f}s")
            print("    输出:", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
            assert r.ok, f"改后应当通过，实际: {r.tail(500)}"

            # 5) 超时保护
            r = box.run(["sleep", "5"])
            print(f"[4] 短命令 -> ok={r.ok}")

        # 超时应当被拦住
        with Sandbox(ws, task_id="timeout", timeout_s=2) as box:
            r = box.run(["sleep", "30"])
            print(f"[5] 超时保护 -> timed_out={r.timed_out} ok={r.ok}")
            assert r.timed_out or not r.ok, "超时应当被拦住"

    print("\nD1 冒烟通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
