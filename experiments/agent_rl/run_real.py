"""用真实 PR 任务评测 agent——这是 superagent 融合点 1 的最终产出。

对比：合成任务（115 个，flash 100% 通过） vs 真实任务（21 个，取自真实 PR）

用法（仓库根目录）：
    uv run python -m experiments.agent_rl.run_real --limit 21 --samples 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.real_tasks import load_verified  # noqa: E402
from experiments.agent_rl.runner import make_model_client  # noqa: E402
from experiments.agent_rl.sandbox import Sandbox  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results" / "real"


async def run_one(task, sample_index: int, model: str | None, temperature: float) -> dict:
    """在真实仓库上跑一次 agent。"""
    import tempfile

    started = time.time()
    payload = {
        "task_id": task.task_id,
        "repo": task.repo,
        "sample_index": sample_index,
        "success": False,
        "steps": 0,
        "duration_s": 0.0,
        "error": None,
    }
    with tempfile.TemporaryDirectory(prefix="real-run-") as tmp:
        workspace = Path(tmp) / "repo"
        if not task.materialize(workspace):
            payload["error"] = "materialize_failed"
            return payload

        try:
            from experiments.agent_rl.runner import run_task_once

            # 复用现有 runner：它接受 Task 协议对象（有 prompt/files/check_cmd/materialize）
            class _Adapter:
                """把 RealTask 适配成 runner 需要的接口。"""

                task_id = task.task_id
                template = "real_pr"
                difficulty = "real"
                prompt = task.prompt
                files: dict = {}
                check_cmd = task.check_cmd
                timeout_s = 900
                meta = task.meta

                def materialize(self, dest: Path) -> None:
                    return None  # workspace 已就绪

                def check(self, ws: Path):
                    with Sandbox(ws, task_id=f"{self.task_id}-chk", timeout_s=600) as box:
                        return box.run(self.check_cmd)

            adapter = _Adapter()
            client = make_model_client(model=model, temperature=temperature)
            episode = await run_task_once(adapter, model_client=client, sample_index=sample_index)

            # 判定
            with Sandbox(workspace, task_id=f"{task.task_id}-eval", timeout_s=600) as box:
                result = box.run(task.check_cmd)
            payload.update(
                {
                    "success": bool(result.ok),
                    "steps": episode.steps,
                    "duration_s": round(time.time() - started, 1),
                    "tokens_in": episode.tokens_in,
                    "tokens_out": episode.tokens_out,
                    "check_tail": result.tail(400),
                    "trajectory_tools": [c["tool"] for c in episode.tool_calls if c["phase"] == "start"],
                }
            )
        except Exception as exc:  # noqa: BLE001
            payload["error"] = f"{type(exc).__name__}: {exc}"
            payload["duration_s"] = round(time.time() - started, 1)
    return payload


async def main_async(args: argparse.Namespace) -> None:
    tasks = load_verified()
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"真实任务: {len(tasks)}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"episodes_real_k{args.samples}.jsonl"
    if out.exists():
        out.unlink()

    semaphore = asyncio.Semaphore(args.concurrency)
    done = {"n": 0, "ok": 0}

    async def bounded(task, sample):
        async with semaphore:
            payload = await run_one(task, sample, args.model, args.temperature)
            done["n"] += 1
            done["ok"] += int(payload["success"])
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            print(
                f"[{done['n']}/{len(tasks)*args.samples}] {task.task_id} "
                f"{'OK  ' if payload['success'] else 'FAIL'} steps={payload['steps']} "
                f"{payload['duration_s']}s | {done['ok']}/{done['n']}",
                flush=True,
            )

    await asyncio.gather(*(bounded(t, i) for t in tasks for i in range(args.samples)))
    print(f"\n真实任务通过率: {done['ok']}/{done['n']} = {done['ok']/done['n']:.1%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--model", default=None)
    asyncio.run(main_async(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
