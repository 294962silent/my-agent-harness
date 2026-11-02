"""构造 GRPO 训练用的 prompt 数据集。

GRPO 需要「同一 prompt 采样多条」的格式，每条只是一个任务描述。
模型的任务：输出下一步的 JSON 动作。

数据来源：现有的 115 个合成任务 + 21 个真实任务。
每条数据 = 完整的 agent 上下文（任务 + 文件内容 + 历史）→ 期望模型输出一个动作。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/autodl-tmp")
RESULTS = Path("/root/autodl-tmp/datasets")

SYSTEM = """You are fixing code in a small repository. Output exactly ONE JSON action per turn, nothing else.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"done","arguments":{}}

IMPORTANT:
- Output ONLY the JSON object. No explanation, no markdown fences.
"""


def build_prompts(sft_path: Path) -> list[dict]:
    """从 SFT 数据里提取「第一条 user 消息」作为 GRPO 的 prompt。"""
    rows = []
    with sft_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            sample = json.loads(line)
            messages = sample.get("messages", [])
            if not messages:
                continue
            first = messages[0]
            if first.get("role") != "user":
                continue
            prompt_text = first["content"]
            # 截断，避免 prompt 过长
            if len(prompt_text) > 3000:
                prompt_text = prompt_text[:3000] + "\n...(truncated)"
            # GRPO 需要 chat messages 格式，否则模型看不到对话结构
            rows.append(
                {
                    "prompt": [{"role": "user", "content": prompt_text}],
                    "task_id": sample.get("task_id", "?"),
                    "num_steps": sample.get("num_steps", 0),
                }
            )
    return rows


def main() -> int:
    src = RESULTS / "sft_train.jsonl"
    if not src.exists():
        print(f"找不到 {src}")
        return 1

    rows = build_prompts(src)
    out = RESULTS / "grpo_prompts.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    lens = [len(r["prompt"][0]["content"]) for r in rows]
    print(f"GRPO 数据集: {len(rows)} 条")
    print(f"prompt 长度: min={min(lens)} avg={sum(lens)//len(lens)} max={max(lens)}")
    print(f"输出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
