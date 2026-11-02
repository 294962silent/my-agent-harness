"""服务器端 SFT 冷启动：Qwen3.5-9B 4bit LoRA。

目标：让 9B 模型学会 agent 的动作格式（JSON 动作），为 GRPO 做准备。

关键配置（针对 32GB 显存）：
- 4bit NF4 量化：模型 ~7GB
- LoRA r=16：29M 可训练参数
- seq_len 2048（真实轨迹较长）
- batch 2 × accum 8 = 有效 batch 16
"""

import argparse
import os

os.environ.setdefault("HF_HOME", "/root/autodl-tmp/cache")

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainingArguments,
)

MODEL = "/root/autodl-tmp/models/Qwen3.5-9B"


def load_dataset(path: str) -> Dataset:
    import json

    rows = []
    with open(path, encoding="utf-8") as fh:
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
    parser.add_argument("--data", default="/root/autodl-tmp/datasets/sft_train.jsonl")
    parser.add_argument("--output", default="/root/autodl-tmp/outputs/sft-9b")
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--accum", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--max-len", type=int, default=2048)
    args = parser.parse_args()

    print("=== SFT 冷启动 Qwen3.5-9B ===", flush=True)
    print(f"步数={args.steps} batch={args.batch} accum={args.accum} seq={args.max_len}", flush=True)

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

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

    print(f"读取数据: {args.data}", flush=True)
    dataset = load_dataset(args.data)
    print(f"样本: {len(dataset)}", flush=True)

    def tokenize(batch):
        enc = tokenizer(batch["text"], truncation=True, max_length=args.max_len, padding=False)
        return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]}

    tokenized = dataset.map(tokenize, batched=True, remove_columns=["text"])
    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    training_args = TrainingArguments(
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

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        data_collator=collator,
    )

    print("开始训练...", flush=True)
    trainer.train()
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)
    print(f"完成，保存到 {args.output}", flush=True)
    print(f"显存峰值: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)
    print("SFT_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
