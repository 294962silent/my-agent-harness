"""把 t9 SFT 数据转成训练格式（含 chatml 包装和多轮上下文扩充）。

设计要点（吸取前三轮 SFT 的教训）：
1. 只训练 assistant 的 body + <|im_end|>（eos 必须参与训练）
2. 每条样本是**单轮**：user 上下文 → 一条动作 → 停止
3. 保留真实 observation（这是与旧数据最本质的区别）

输出格式：每行 {"input_ids": [...], "labels": [...]}
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("HF_HOME", r"E:\agent-rl\cache")

from transformers import AutoTokenizer  # noqa: E402

MODEL = r"E:\agent-rl\models\Qwen3-1.7B"
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


def build_example(user: str, assistant: str, tokenizer) -> dict:
    """构造 (input_ids, labels)，只有 assistant 内容 + <|im_end|> 有 label。"""
    input_ids = []
    labels = []

    def _tok(text: str) -> list[int]:
        return tokenizer(text, add_special_tokens=False)["input_ids"]

    # user 段（不训练）
    head = _tok(IM_START + "user\n")
    body = _tok(user)
    tail = _tok(IM_END + "\n")
    input_ids += head + body + tail
    labels += [-100] * len(head + body + tail)

    # assistant 段（训练 body + eos）
    head = _tok(IM_START + "assistant\n")
    body = _tok(assistant)
    tail = _tok(IM_END + "\n")
    input_ids += head + body + tail
    labels += [-100] * len(head) + list(body) + list(tail)

    return {"input_ids": input_ids, "labels": labels}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=r"C:\Users\MLL\Desktop\my-agent-harness\experiments\agent_rl\results\sft_t9.jsonl")
    p.add_argument("--out", default=r"C:\Users\MLL\Desktop\my-agent-harness\experiments\agent_rl\results\sft_t9_train.jsonl")
    p.add_argument("--max-len", type=int, default=2048)
    args = p.parse_args()

    tok = AutoTokenizer.from_pretrained(MODEL)
    rows = [json.loads(line) for line in open(args.src, encoding="utf-8") if line.strip()]
    print(f"源样本: {len(rows)}")

    out_rows = []
    kept_labels = 0
    total_labels = 0
    for r in rows:
        ex = build_example(r["user"], r["assistant"], tok)
        if len(ex["input_ids"]) > args.max_len:
            # 截断时保留尾部（含 assistant）
            ex["input_ids"] = ex["input_ids"][-args.max_len :]
            ex["labels"] = ex["labels"][-args.max_len :]
        n_lab = sum(1 for x in ex["labels"] if x != -100)
        if n_lab == 0:
            continue
        kept_labels += n_lab
        total_labels += len(ex["labels"])
        out_rows.append(ex)

    print(f"有效样本: {len(out_rows)}")
    print(f"总 token: {total_labels:,}, 训练 token: {kept_labels:,} ({kept_labels/total_labels:.1%})")

    with open(args.out, "w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r) + "\n")
    print(f"写入 {args.out}")

    ex = out_rows[0]
    kept = [t for t, l in zip(ex["input_ids"], ex["labels"]) if l != -100]
    print(f"\n第一条被训练的部分: {tok.decode(kept)[:200]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
