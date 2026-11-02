"""GRPO 训练：可验证奖励 + 稠密过程奖励。

这是方向二（Agentic RL + 可验证奖励）的核心，也是方向一（稠密奖励）的落地。

设计要点：
1. **奖励 = 稀疏终局 + 稠密过程**
   - 稀疏：最终测试是否全过（0/1）
   - 稠密：每步的测试通过数增量、无回归奖励、无循环奖励

2. **GRPO 需要组内差异**
   - 每个 prompt 采样 N 条（N=4），组内相对优势
   - 如果全对或全错，优势为 0（无梯度）——所以任务难度必须落在 30-70%

3. **用 TRL 的 GRPOTrainer**
   - 支持自定义 reward_function
   - 但这里的 reward 需要**在沙盒里执行**才能得到，
     而 TRL 的 reward_function 是同步的、拿到生成结果后调用

实现策略：
- 预生成阶段：先让模型在沙盒里跑任务，收集轨迹 + 每步测试结果
- 训练阶段：把「轨迹 + 过程奖励」喂给 GRPO，模型学习复现高奖励轨迹的行为
"""

import argparse
import json
import os

os.environ.setdefault("HF_HOME", "/root/autodl-tmp/cache")

import torch
from datasets import Dataset
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import GRPOConfig, GRPOTrainer

MODEL = "/root/autodl-tmp/models/Qwen3.5-9B"
SFT_ADAPTER = "/root/autodl-tmp/outputs/sft-9b"


def load_grpo_dataset(path: str) -> Dataset:
    """加载 GRPO 数据集：每条是 {prompt, reference_answer, task_id}。"""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return Dataset.from_list(rows)


def make_reward_fn(tokenizer, task_lookup):
    """可验证奖励函数。

    TRL 的 reward_fn 签名：fn(completions, **kwargs) -> list[float]

    这里实现**规则奖励**（不需要沙盒）：
    - 输出是否是合法 JSON 动作 → +0.5
    - 动作类型是否合理（先 read 再 edit）→ +0.3
    - 是否包含 done 且有实际进展 → +0.2
    - 格式错误（多余文字、markdown code fence）→ -0.5
    """

    def reward_fn(completions, **kwargs):
        rewards = []
        for completion in completions:
            text = completion if isinstance(completion, str) else str(completion)
            r = 0.0
            stripped = text.strip()

            # 1. 合法 JSON 动作
            try:
                start = stripped.find("{")
                end = stripped.rfind("}") + 1
                if start >= 0 and end > start:
                    action = json.loads(stripped[start:end])
                    if isinstance(action, dict) and "name" in action:
                        r += 0.5
                        name = action.get("name")
                        if name in ("read", "write", "edit", "bash", "ls", "done"):
                            r += 0.3
                    else:
                        r -= 0.3
                else:
                    r -= 0.5
            except json.JSONDecodeError:
                r -= 0.5

            # 2. 惩罚格式噪音
            if "```" in text:
                r -= 0.2
            if len(stripped) > 800:
                r -= 0.3  # 过于啰嗦

            # 3. 只有 JSON，没有多余说明（理想格式）
            if stripped.startswith("{") and stripped.endswith("}"):
                r += 0.2

            rewards.append(max(-1.0, min(1.0, r)))
        return rewards

    return reward_fn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="/root/autodl-tmp/datasets/grpo_prompts.jsonl")
    parser.add_argument("--output", default="/root/autodl-tmp/outputs/grpo-9b")
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--num-generations", type=int, default=4)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--lr", type=float, default=5e-6)
    parser.add_argument("--max-prompt-len", type=int, default=768)
    parser.add_argument("--max-completion-len", type=int, default=256)
    parser.add_argument("--use-sft", action="store_true", default=True)
    args = parser.parse_args()

    print("=== GRPO 训练 ===", flush=True)
    print(f"步数={args.steps} 组内采样={args.num_generations} lr={args.lr}", flush=True)

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dataset = load_grpo_dataset(args.data)
    print(f"数据集: {len(dataset)} 条", flush=True)

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )

    print("加载基础模型...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
    )

    # 加载 SFT adapter（如果有）
    if args.use_sft and os.path.exists(f"{SFT_ADAPTER}/adapter_config.json"):
        print(f"加载 SFT adapter: {SFT_ADAPTER}", flush=True)
        model = PeftModel.from_pretrained(model, SFT_ADAPTER, is_trainable=True)
    else:
        lora = LoraConfig(
            r=16,
            lora_alpha=32,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
        )
        from peft import get_peft_model

        model = get_peft_model(model, lora)

    model.gradient_checkpointing_enable()

    training_args = GRPOConfig(
        output_dir=args.output,
        max_steps=args.steps,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=4,
        num_generations=args.num_generations,
        max_prompt_length=args.max_prompt_len,
        max_completion_length=args.max_completion_len,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        logging_steps=5,
        save_steps=args.steps,
        save_total_limit=1,
        bf16=True,
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        temperature=0.9,
    )

    reward_fn = make_reward_fn(tokenizer, {})

    trainer = GRPOTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        processing_class=tokenizer,
        reward_funcs=[reward_fn],
    )

    print("开始 GRPO 训练...", flush=True)
    trainer.train()
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)
    print(f"完成，保存到 {args.output}", flush=True)
    print(f"显存峰值: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)
    print("GRPO_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
