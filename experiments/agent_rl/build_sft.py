"""从真实 episode 构造 SFT 训练数据（用于小模型 warm-start）。

背景：Qwen3.5-2B 直接跑任务时 54% 的步骤输出无法解析成 JSON 动作，
通过率 0–20%。标准做法是先用高质量轨迹做 **SFT warm-start**，
让模型学会输出规范的动作格式，再做 GRPO。

数据来源：`episodes_*.jsonl` 里的 `tool_calls`（真实的工具名 + 参数）。
每条 episode 还原成多轮对话：
    user: 任务 + 文件内容 + 工具说明
    assistant: {"name": "read", "arguments": {...}}
    user: [observation] 工具返回
    assistant: ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

RESULTS = Path(__file__).resolve().parent / "results"
OUT = Path(r"E:\agent-rl\datasets")

SYSTEM = """You are fixing code in a small repository. Output exactly ONE JSON action per turn, nothing else.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"done","arguments":{}}

IMPORTANT:
- Output ONLY the JSON object. No explanation, no markdown fences.
- Do NOT use "done" until the tests actually pass.
- Read the test file first to learn what is expected.
"""


def load_episodes(paths: list[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    items.append(json.loads(line))
    return items


def build_sample(episode: dict[str, Any], task_lookup: dict[str, Any]) -> dict[str, Any] | None:
    """把一个 episode 还原成 SFT 样本（messages 列表）。"""
    if not episode.get("success"):
        return None
    task = task_lookup.get(episode["task_id"])
    if task is None:
        return None

    calls = [c for c in episode.get("tool_calls", []) if c.get("phase") == "start"]
    if not calls:
        return None

    files_preview = []
    for name in sorted(task.files):
        files_preview.append(f"--- {name} ---\n{task.files[name][:800]}")
    repo_block = "\n".join(files_preview)[:4000]

    first_user = (
        f"{SYSTEM}\n\n=== TASK ===\n{task.prompt}\n\n"
        f"=== REPOSITORY FILES (already shown) ===\n{repo_block}\n"
    )

    messages: list[dict[str, str]] = [{"role": "user", "content": first_user}]
    for call in calls:
        action = {"name": call["tool"], "arguments": call.get("args") or {}}
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({"role": "user", "content": "[observation] (tool executed)"})

    messages.append({"role": "assistant", "content": json.dumps({"name": "done", "arguments": {}})})
    return {
        "task_id": episode["task_id"],
        "messages": messages,
        "num_steps": len(calls),
        "template": episode.get("template"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inputs",
        default="episodes_main_k5.jsonl,episodes_t7.jsonl,episodes_calibrate.jsonl",
    )
    parser.add_argument("--out", default=str(OUT / "sft_train.jsonl"))
    parser.add_argument("--max-samples", type=int, default=None)
    args = parser.parse_args()

    paths = [RESULTS / name.strip() for name in args.inputs.split(",")]
    episodes = load_episodes(paths)

    from experiments.agent_rl import tasks as _tasks  # noqa: F401
    from experiments.agent_rl.task import generate_all

    task_lookup = {t.task_id: t for t in generate_all()}

    samples = []
    for episode in episodes:
        sample = build_sample(episode, task_lookup)
        if sample:
            samples.append(sample)
    if args.max_samples:
        samples = samples[: args.max_samples]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")

    steps = [s["num_steps"] for s in samples]
    print(f"episodes 读入: {len(episodes)}")
    print(f"SFT 样本: {len(samples)}")
    print(f"平均步数: {sum(steps)/len(steps):.1f}" if steps else "无样本")
    print(f"输出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
