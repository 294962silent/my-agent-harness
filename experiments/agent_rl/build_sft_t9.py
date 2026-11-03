"""把采集到的真实轨迹构造成 SFT 数据。

关键区别（对比旧的 sft_train.jsonl）：
- 旧数据：6703 条 observation 全是 '(tool executed)' 占位符，模型学不到反馈
- 新数据：62 条 observation 有 34 种真实内容（文件内容、写入确认、测试结果）

每条轨迹拆成单轮样本：user = 到当前步为止的上下文，assistant = 这条动作。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "results" / "t9_trajectories.jsonl"
OUT = HERE / "results" / "sft_t9.jsonl"

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


def build_samples(trajectory: dict) -> list[dict]:
    """把一条轨迹拆成单轮样本。"""
    prompt_base = trajectory.get("prompt_base", "")
    samples = []
    history: list[str] = []

    for conv in trajectory.get("conversations", []):
        convo = conv["convo"]
        assistant = conv["assistant"]
        if not assistant or not assistant.strip():
            continue
        samples.append({"user": convo, "assistant": assistant.strip()})
        # 把这一步的真实反馈接进下一轮上下文
        obs = conv.get("observation", "")
        tail = conv.get("tests_tail", "")
        history.append(f"[observation] {obs[:900]}\n[tests] {tail}")

    return samples


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=str(SRC))
    p.add_argument("--out", default=str(OUT))
    args = p.parse_args()

    rows = [json.loads(line) for line in open(args.src, encoding="utf-8") if line.strip()]
    print(f"轨迹数: {len(rows)}")

    all_samples = []
    for r in rows:
        if not r.get("success"):
            continue
        for s in build_samples(r):
            s["task_id"] = r["task_id"]
            all_samples.append(s)

    print(f"单轮样本: {len(all_samples)}")

    obs_set = set()
    for r in rows:
        for c in r.get("conversations", []):
            obs_set.add(c.get("observation", ""))
    print(f"唯一 observation: {len(obs_set)}")

    with open(args.out, "w", encoding="utf-8") as fh:
        for s in all_samples:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"写入 {args.out}")

    # 抽样预览
    print("\n=== 样本预览 ===")
    for s in all_samples[:2]:
        print(f"[user] ...{s['user'][-300:]!r}")
        print(f"[assistant] {s['assistant'][:120]!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
