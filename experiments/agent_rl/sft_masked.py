"""SFT 冷启动 v2：只对 assistant 部分计算损失。

v1 的致命 bug（见 DIRECTION_2.md 第十二节）：
    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
在**没有 labels** 时，它会把 input_ids 复制成 labels，
于是模型在整个序列上被训练——包括用户 prompt（956 字符）
和 `[observation]` 占位符（每条 29 字符 × 8）。

后果：约一半的训练信号用在「学习怎么写任务描述」上，
模型学到的是「复述 prompt 风格」而不是「输出动作」。

v2 的修正：
1. 手动拼接 "<|im_start|>role\n内容<|im_end|>\n"，不依赖 chat_template
   （Qwen3.5 模板会自动插入空思考标记，导致内容边界漂移）
2. 只有 assistant 的内容部分 label 有效，其余 -100
3. 用 DataCollatorForSeq2Seq 处理 padding 与 label padding
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("HF_HOME", "/root/autodl-tmp/cache")

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForSeq2Seq,
    Trainer,
    TrainingArguments,
)

MODEL = "/root/autodl-tmp/models/Qwen3.5-9B"

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


def build_example(messages: list[dict], tokenizer) -> dict:
    """把一条多轮样本变成 (input_ids, labels)，只有 assistant 内容有 label。

    直接手动拼接，不用 chat_template —— 后者会插入空思考标记，
    让 assistant 内容的起始位置不可预测，模型因此学会先输出思考过程。
    """
    input_ids: list[int] = []
    labels: list[int] = []

    def _tok(text: str) -> list[int]:
        return tokenizer(text, add_special_tokens=False)["input_ids"]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        head = _tok(IM_START + role + "\n")
        body = _tok(content)
        tail = _tok(IM_END + "\n")

        seg_ids = head + body + tail
        if role == "assistant":
            # 关键：tail 里的 <|im_end|> 就是 eos_token，必须让模型学会输出它，
            # 否则模型不知道何时停，会连续输出多条动作（实测 83% 的生成是多动作）。
            seg_lab = [-100] * len(head) + list(body) + list(tail)
        else:
            seg_lab = [-100] * len(seg_ids)
        input_ids.extend(seg_ids)
        labels.extend(seg_lab)

    return {"input_ids": input_ids, "labels": labels}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="/root/autodl-tmp/datasets/sft_train.jsonl")
    p.add_argument("--output", default="/root/autodl-tmp/outputs/sft-9b-v2")
    p.add_argument("--steps", type=int, default=300)
    p.add_argument("--batch", type=int, default=2)
    p.add_argument("--accum", type=int, default=8)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--max-len", type=int, default=3072)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--split-single", action="store_true",
                   help="把多轮轨迹拆成单轮样本，只学一条动作后停止")
    args = p.parse_args()

    print("=== SFT v2（只训练 assistant 部分）===", flush=True)

    tok = AutoTokenizer.from_pretrained(MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    raw = [json.loads(line) for line in open(args.data, encoding="utf-8") if line.strip()]
    print(f"原始样本: {len(raw)}", flush=True)

    if args.split_single:
        # 把多轮轨迹拆成单轮样本：每个 assistant 回复配上前文，单独成一个样本。
        # 这样模型只学「输出一条动作然后停」，不会学「连续输出整条轨迹」。
        split = []
        for sample in raw:
            msgs = sample["messages"]
            for i, msg in enumerate(msgs):
                if msg["role"] == "assistant":
                    split.append({"messages": msgs[: i + 1]})
        raw = split
        print(f"拆成单轮后: {len(raw)}", flush=True)

    examples = [build_example(s["messages"], tok) for s in raw]

    total_labels = sum(sum(1 for x in e["labels"] if x != -100) for e in examples)
    total_tokens = sum(len(e["labels"]) for e in examples)
    print(
        f"总 token: {total_tokens:,}, 有 label 的 token: {total_labels:,} "
        f"({total_labels / total_tokens:.1%})",
        flush=True,
    )

    ex0 = examples[0]
    kept = [tid for tid, lab in zip(ex0["input_ids"], ex0["labels"]) if lab != -100]
    print("\n第一条样本被训练的部分:", flush=True)
    print(repr(tok.decode(kept)[:400]), flush=True)
    print()

    if args.dry_run:
        print("dry-run 结束", flush=True)
        return 0

    ds = Dataset.from_list(examples)

    def truncate(batch):
        out_ids, out_lab = [], []
        for ids, lab in zip(batch["input_ids"], batch["labels"]):
            out_ids.append(ids[: args.max_len])
            out_lab.append(lab[: args.max_len])
        return {"input_ids": out_ids, "labels": out_lab}

    ds = ds.map(truncate, batched=True)

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    print("加载模型...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
    )
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    collator = DataCollatorForSeq2Seq(tokenizer=tok, padding=True, label_pad_token_id=-100)

    targs = TrainingArguments(
        output_dir=args.output,
        max_steps=args.steps,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=15,
        logging_steps=5,
        save_steps=args.steps,
        save_total_limit=1,
        bf16=True,
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        dataloader_pin_memory=False,
        gradient_checkpointing_kwargs={"use_reentrant": False},
    )

    trainer = Trainer(model=model, args=targs, train_dataset=ds, data_collator=collator)
    print("开始训练...", flush=True)
    trainer.train()
    trainer.save_model(args.output)
    tok.save_pretrained(args.output)
    print(f"完成，保存到 {args.output}", flush=True)
    print(f"显存峰值: {torch.cuda.max_memory_allocated() / 1024**3:.2f} GB", flush=True)
    print("SFT_V2_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
