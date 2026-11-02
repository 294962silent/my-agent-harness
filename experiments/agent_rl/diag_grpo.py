"""诊断 GRPO 生成：为什么 completions 长度只有 1。"""

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

MODEL = "/root/autodl-tmp/models/Qwen3.5-9B"
SFT = "/root/autodl-tmp/outputs/sft-9b"

tok = AutoTokenizer.from_pretrained(MODEL)
print("eos_token:", repr(tok.eos_token), "id:", tok.eos_token_id)
print("pad_token:", repr(tok.pad_token), "id:", tok.pad_token_id)

bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                         bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4")
base = AutoModelForCausalLM.from_pretrained(MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16)

print("\n=== 不带 SFT adapter ===")
prompt = "<|im_start|>user\nFix mod.py. Output one JSON action.<|im_end|>\n<|im_start|>assistant\n"
enc = tok(prompt, return_tensors="pt").to("cuda")
with torch.no_grad():
    out = base.generate(**enc, max_new_tokens=60, do_sample=False, pad_token_id=tok.eos_token_id)
text = tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
print(f"生成 {out.shape[1]-enc['input_ids'].shape[1]} tokens: {text[:200]!r}")

print("\n=== 带 SFT adapter ===")
model = PeftModel.from_pretrained(base, SFT)
with torch.no_grad():
    out2 = model.generate(**enc, max_new_tokens=60, do_sample=False, pad_token_id=tok.eos_token_id)
text2 = tok.decode(out2[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
print(f"生成 {out2.shape[1]-enc['input_ids'].shape[1]} tokens: {text2[:200]!r}")

print("\n=== 数据集第一条 prompt 检查 ===")
import json

with open("/root/autodl-tmp/datasets/grpo_prompts.jsonl") as fh:
    row = json.loads(fh.readline())
p = row["prompt"]
print(f"长度 {len(p)} 字符")
print(f"开头: {p[:150]!r}")
print(f"结尾: {p[-100:]!r}")
