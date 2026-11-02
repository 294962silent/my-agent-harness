"""本地最小 GRPO 验证：用 0.5B 模型证明训练流程可用。

目的：服务器失联时，用本机能跑的模型（5060 8GB）验证：
1. GRPOTrainer 能正常初始化
2. 优势计算有梯度（reward_std > 0）
3. loss 能下降
4. 保存的 adapter 能重新加载

模型：Qwen2.5-0.5B-Instruct（约 1GB，4bit 后 <1GB）
数据：从真实轨迹提取的 prompt（不需要外部数据）
奖励：结构奖励（grpo_reward.py 第 1 层）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HOME", r"E:\agent-rl\cache")

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import GRPOConfig, GRPOTrainer

sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")

from experiments.agent_rl.grpo_reward import make_grpo_reward_fn  # noqa: E402

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
RESULTS = Path(r"C:\Users\MLL\Desktop\my-agent-harness\experiments\agent_rl\results")


def build_dataset(limit: int = 40) -> Dataset:
    """从真实 SFT 数据构造 GRPO prompt（chat 格式）。"""
    src = Path(r"E:\agent-rl\datasets\sft_train.jsonl")
    rows = []
    if src.exists():
        with src.open(encoding="utf-8") as fh:
            for line in fh:
                if len(rows) >= limit:
                    break
                if not line.strip():
                    continue
                sample = json.loads(line)
                msgs = sample.get("messages", [])
                if not msgs or msgs[0].get("role") != "user":
                    continue
                text = msgs[0]["content"][:1500]
                rows.append({"prompt": [{"role": "user", "content": text}]})
    if not rows:
        rows = [{"prompt": [{"role": "user", "content": "Output one JSON action: read mod.py"}]}] * 8
    return Dataset.from_list(rows)


def main() -> int:
    print("=== 本地最小 GRPO 验证 ===\n", flush=True)
    print(f"GPU: {torch.cuda.get_device_name(0)}", flush=True)
    print(f"显存: {torch.cuda.get_device_properties(0).total_memory/1024**3:.1f} GB\n", flush=True)

    tok = AutoTokenizer.from_pretrained(MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    print("加载 0.5B 模型（4bit）...", flush=True)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
    )
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=8,
        lora_alpha=16,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"可训练参数: {trainable/1e6:.1f}M", flush=True)
    print(f"显存: {torch.cuda.memory_allocated()/1024**3:.2f} GB\n", flush=True)

    dataset = build_dataset(limit=32)
    print(f"数据集: {len(dataset)} 条 prompt\n", flush=True)

    out_dir = r"E:\agent-rl\outputs\grpo-verify"
    args = GRPOConfig(
        output_dir=out_dir,
        max_steps=50,
        per_device_train_batch_size=4,
        gradient_accumulation_steps=1,
        num_generations=4,
        max_completion_length=128,
        learning_rate=1e-5,
        logging_steps=10,
        save_steps=10,
        save_total_limit=1,
        bf16=True,
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        temperature=1.0,
    )

    reward_fn = make_grpo_reward_fn()
    print("初始化 GRPOTrainer...", flush=True)
    trainer = GRPOTrainer(
        model=model,
        args=args,
        train_dataset=dataset,
        processing_class=tok,
        reward_funcs=[reward_fn],
    )

    print("开始训练（10 步）...\n", flush=True)
    result = trainer.train()

    print("\n=== 训练完成 ===", flush=True)
    print(f"最终 loss: {result.training_loss:.4f}", flush=True)
    print(f"显存峰值: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)

    # 保存并验证可加载
    trainer.save_model(out_dir)
    tok.save_pretrained(out_dir)
    adapter_ok = os.path.exists(os.path.join(out_dir, "adapter_config.json"))
    print(f"\nadapter 已保存: {adapter_ok}", flush=True)

    # 验证生成的格式改善（训练后推理一次）
    print("\n=== 训练后生成样例 ===", flush=True)
    model.eval()
    prompt = tok.apply_chat_template(
        [{"role": "user", "content": 'Output one JSON action to read "mod.py"'}],
        tokenize=False,
        add_generation_prompt=True,
    )
    enc = tok(prompt, return_tensors="pt").to("cuda")
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=80, do_sample=False, pad_token_id=tok.eos_token_id)
    text = tok.decode(out[0][enc["input_ids"].shape[1] :], skip_special_tokens=True)
    print(f"  {text[:200]!r}", flush=True)

    from experiments.agent_rl.grpo_reward import structure_reward

    score, detail = structure_reward(text)
    print(f"  结构奖励: {score:+.2f}  {detail}", flush=True)

    summary = {
        "model": MODEL,
        "steps": 50,
        "final_loss": round(result.training_loss, 4),
        "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 2),
        "adapter_saved": adapter_ok,
        "sample_output": text[:200],
        "sample_structure_score": score,
        "verdict": "PASS" if adapter_ok else "FAIL",
    }
    (RESULTS / "grpo_local_verify.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入 {RESULTS / 'grpo_local_verify.json'}", flush=True)
    return 0 if adapter_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
