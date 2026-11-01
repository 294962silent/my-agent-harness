"""用本地 Qwen3.5-2B 跑沙盒任务，测通过率——决定 GRPO 是否可行。

用法（E 盘 venv，需设置 PYTHONPATH）：
    E:\\agent-rl\\.venv\\Scripts\\python.exe probe.py --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401
from experiments.agent_rl.simple_loop import run_rollout  # noqa: E402
from experiments.agent_rl.task import generate_all  # noqa: E402

MODEL_PATH = r"E:\agent-rl\models\Qwen3-1.7B"
_model = None
_tok = None


def load() -> tuple:
    global _model, _tok
    if _model is None:
        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        # low_cpu_mem_usage: 边加载边搬到 GPU，避免在 CPU 侧堆积完整权重；
        # 关闭 mmap 可以规避 Windows 页面文件不足（os error 1455）
        _model = AutoModelForCausalLM.from_pretrained(
            MODEL_PATH,
            quantization_config=bnb,
            device_map="cuda:0",
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            use_safetensors=True,
        )
        _tok = AutoTokenizer.from_pretrained(MODEL_PATH)
    return _model, _tok


def make_generator(max_new_tokens: int = 400):
    def generate(prompt: str, temperature: float) -> tuple[str, int]:
        model, tok = load()
        inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=4500).to("cuda")
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=temperature > 0,
                temperature=temperature if temperature > 0 else None,
                top_p=0.95 if temperature > 0 else None,
                pad_token_id=tok.eos_token_id,
            )
        n = int(out.shape[1] - inputs["input_ids"].shape[1])
        text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        return text, n

    return generate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default="t1_bugfix")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--out", default=r"E:\agent-rl\outputs\probe.jsonl")
    args = parser.parse_args()

    tasks = [t for t in generate_all() if t.template == args.template][: args.limit]
    generator = make_generator()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    results = []
    for index, task in enumerate(tasks, 1):
        t0 = time.time()
        rollout = run_rollout(task, generator, max_steps=args.max_steps, temperature=args.temperature)
        results.append(rollout)
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rollout.to_dict(), ensure_ascii=False) + "\n")
        print(
            f"[{index}/{len(tasks)}] {task.task_id} {'OK  ' if rollout.success else 'FAIL'} "
            f"steps={len(rollout.steps)} {time.time()-t0:.0f}s tokens={rollout.total_tokens}"
        )
        if rollout.steps:
            first = rollout.steps[0]
            print(f"       首步动作: {first.action}")

    ok = sum(1 for r in results if r.success)
    print(f"\n通过率: {ok}/{len(results)} = {ok/len(results):.0%}")
    print(f"平均步数: {sum(len(r.steps) for r in results)/len(results):.1f}")
    print(f"平均 token: {sum(r.total_tokens for r in results)/len(results):.0f}")
    by_action = defaultdict(int)
    for r in results:
        for s in r.steps:
            if s.action:
                by_action[s.action.get("name", "?")] += 1
    print("动作分布:", dict(by_action))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
