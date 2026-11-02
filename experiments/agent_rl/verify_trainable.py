"""验证 Qwen3.5 系列在 sm_89 上能否反向传播（本地 Blackwell 上会 CUBLAS 崩溃）。

这是租卡后的第一个关键实验：如果能训，就用新版架构；不能，立刻切 Qwen3 系列。
"""

from __future__ import annotations

import sys

import torch

MODEL = sys.argv[1] if len(sys.argv) > 1 else "Qwen/Qwen3.5-9B"

print(f"=== 测试模型: {MODEL} ===")
print(f"GPU: {torch.cuda.get_device_name(0)} | cap: {torch.cuda.get_device_capability(0)}")
print(f"torch: {torch.__version__}")

from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
)

print("加载模型（4bit）...")
model = AutoModelForCausalLM.from_pretrained(
    MODEL, quantization_config=bnb, device_map="cuda:0", torch_dtype=torch.bfloat16
)
print(f"加载完成，显存 {torch.cuda.memory_allocated()/1024**3:.2f} GB")
print(f"架构: {type(model).__name__}")

tokenizer = AutoTokenizer.from_pretrained(MODEL)
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
print(f"可训练参数: {trainable/1e6:.1f}M")

# 关键测试：前向 + 反向
text = 'user: 修复 mod.py 的 bug\nassistant: {"name":"read","arguments":{"path":"mod.py"}}'
enc = tokenizer(text, return_tensors="pt").to("cuda")
print("开始反向传播测试...")
try:
    out = model(**enc, labels=enc["input_ids"])
    loss = out.loss
    loss.backward()
    print(f"\n✅ 反向传播成功！loss = {float(loss.detach()):.4f}")
    print(f"显存峰值: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB")
    grad_ok = any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.parameters() if p.requires_grad)
    print(f"梯度非零: {grad_ok}")
    sys.exit(0)
except Exception as exc:
    print(f"\n❌ 反向传播失败: {type(exc).__name__}")
    print(str(exc)[:500])
    sys.exit(1)
