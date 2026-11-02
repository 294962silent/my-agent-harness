"""验证服务器上模型文件的完整性 + 能否反向传播。"""

import sys

import torch

MODEL = "/root/autodl-tmp/models/Qwen3.5-9B"

print(f"=== 验证 {MODEL} ===", flush=True)
print(f"GPU: {torch.cuda.get_device_name(0)} | cap: {torch.cuda.get_device_capability(0)}", flush=True)
print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory/1024**3:.1f} GB", flush=True)

from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

print("加载 tokenizer...", flush=True)
tok = AutoTokenizer.from_pretrained(MODEL)
print(f"词表大小: {len(tok)}", flush=True)

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
)

print("加载模型（4bit，约 2 分钟）...", flush=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
)
print(f"加载完成，显存 {torch.cuda.memory_allocated()/1024**3:.2f} GB", flush=True)
print(f"架构: {type(model).__name__}", flush=True)

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
trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"可训练参数: {trainable/1e6:.1f}M", flush=True)

text = 'user: 修复 mod.py\nassistant: {"name":"read","arguments":{"path":"mod.py"}}'
enc = tok(text, return_tensors="pt").to("cuda")
print("反向传播测试...", flush=True)
try:
    out = model(**enc, labels=enc["input_ids"])
    loss = out.loss
    loss.backward()
    print(f"\n✅ 成功！loss = {float(loss.detach()):.4f}", flush=True)
    print(f"峰值显存: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)
    grad_ok = any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.parameters() if p.requires_grad)
    print(f"梯度非零: {grad_ok}", flush=True)
    print("RESULT: PASS", flush=True)
except Exception as exc:
    print(f"\n❌ 失败: {type(exc).__name__}", flush=True)
    print(str(exc)[:600], flush=True)
    print("RESULT: FAIL", flush=True)
