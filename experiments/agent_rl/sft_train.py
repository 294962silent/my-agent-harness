"""Qwen3.5-2B LoRA SFT：让模型学会输出规范的 agent 动作格式。

这是 GRPO 前的 warm-start——小模型直接跑 agent 时格式崩坏率 54%，
先 SFT 学会「只输出一个 JSON 动作」，再上 RL 才有意义。

在 E:\\agent-rl 的 venv 里跑：
    .venv\\Scripts\\python.exe sft_train.py --steps 300
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("HF_HOME", r"E:\agent-rl\cache")

import torch  # noqa: E402
from datasets import Dataset  # noqa: E402
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainingArguments,
)

MODEL_PATH = r"E:\agent-rl\models\Qwen3-1.7B"


def load_dataset(path: Path) -> Dataset:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            sample = json.loads(line)
            parts = []
            for message in sample["messages"]:
                parts.append(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>")
            rows.append({"text": "\n".join(parts)})
    return Dataset.from_list(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=r"E:\agent-rl\datasets\sft_train.jsonl")
    parser.add_argument("--output", default=r"E:\agent-rl\outputs\sft-lora")
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--accum", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--max-len", type=int, default=1024)
    parser.add_argument("--dtype", default="float16", choices=["float16", "bfloat16"])
    args = parser.parse_args()

    print(f"加载 tokenizer: {MODEL_PATH}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    compute_dtype = torch.float16 if args.dtype == "float16" else torch.bfloat16
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    print("加载模型（4bit）...")
    # Windows 页面文件紧张：device_map 用字符串走直连加载路径，
    # 用 dict 会让 accelerate 先建 CPU 副本再搬运，触发 os error 1455
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_PATH,
        quantization_config=bnb,
        device_map="cuda:0",
        dtype=compute_dtype,
        low_cpu_mem_usage=True,
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

    print(f"读取数据: {args.data}")
    dataset = load_dataset(Path(args.data))
    print(f"样本数: {len(dataset)}")

    def tokenize(batch):
        out = tokenizer(
            batch["text"],
            truncation=True,
            max_length=args.max_len,
            padding=False,
        )
        out["labels"] = out["input_ids"].copy()
        return out

    tokenized = dataset.map(tokenize, batched=True, remove_columns=["text"])

    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    training_args = TrainingArguments(
        output_dir=args.output,
        max_steps=args.steps,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_steps=20,
        logging_steps=10,
        save_steps=args.steps,
        save_total_limit=1,
        bf16=args.dtype == "bfloat16",
        fp16=args.dtype == "float16",
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        dataloader_pin_memory=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        data_collator=collator,
    )
    print("开始训练...")
    trainer.train()
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)
    print(f"完成，保存到 {args.output}")
    print("显存峰值: %.2f GB" % (torch.cuda.max_memory_allocated() / 1024**3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
