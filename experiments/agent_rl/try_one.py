"""单任务试跑：验证 agent 能在沙盒里完成任务并产出轨迹。

跑法（仓库根目录）：
    uv run python -m experiments.agent_rl.try_one --task t4-001
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.runner import make_model_client, run_task_once  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="t4-001")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    tasks = {t.task_id: t for t in generate_all()}
    if args.task not in tasks:
        print(f"没有该任务: {args.task}；可选示例: {list(tasks)[:5]}")
        return 1
    task = tasks[args.task]

    print(f"任务: {task.task_id} [{task.template}/{task.difficulty}]")
    print(f"题面: {task.prompt[:120]}...")
    print(f"文件: {list(task.files)}")

    client = make_model_client(model=args.model, temperature=0.2)
    episode = await run_task_once(task, model_client=client)

    print("\n==== 结果 ====")
    print(f"成功: {episode.success}  reward={episode.reward}")
    print(f"步数: {episode.steps}  耗时: {episode.duration_s:.1f}s")
    print(f"token: in={episode.tokens_in} out={episode.tokens_out}")
    if episode.error:
        print(f"错误: {episode.error}")
    print(f"判定输出: {episode.check_tail[:300]}")
    print("\n工具调用序列:")
    for call in episode.tool_calls[:20]:
        if call["phase"] == "start":
            args_repr = str(call.get("args", ""))[:90]
            print(f"  -> {call['tool']} {args_repr}")

    return 0 if episode.success else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
