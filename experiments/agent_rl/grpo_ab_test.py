"""完整的 GRPO 训练前后对比：Qwen3-1.7B × 真实沙盒任务。

这是方向二的最终验证：
1. 用真实任务（T8 长程任务）测基线通过率
2. 跑 GRPO 训练（奖励 = 结构奖励，可快速验证）
3. 再测通过率，看是否提升

为什么用 1.7B 而不是 2B：本地实测 Qwen3.5-2B 在 Blackwell 上反向传播崩溃
（CUBLAS_STATUS_INTERNAL_ERROR，线性注意力），1.7B 是标准架构。

用法：
    python grpo_ab_test.py --mode baseline    # 只测基线
    python grpo_ab_test.py --mode train       # 训练
    python grpo_ab_test.py --mode eval        # 训练后评测
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("HF_HOME", r"E:\agent-rl\cache")

sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")

MODEL = r"E:\agent-rl\models\Qwen3-1.7B"
ADAPTER = r"E:\agent-rl\outputs\grpo-17b"
RESULTS = Path(r"C:\Users\MLL\Desktop\my-agent-harness\experiments\agent_rl\results")

SYSTEM = """You are fixing code in a Python repository. Output exactly ONE JSON action per turn.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"ls","arguments":{"path":"DIR"}}
{"name":"done","arguments":{}}

Output ONLY the JSON object. No explanation, no markdown fences.
"""


def parse_action(text: str) -> dict | None:
    import json as _json

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    depth = 0
    start = -1
    in_str = escape = False
    for idx, ch in enumerate(cleaned):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = _json.loads(cleaned[start : idx + 1])
                except _json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(data, dict) and "name" in data:
                    args = data.get("arguments")
                    if not isinstance(args, dict):
                        args = {k: v for k, v in data.items() if k != "name"}
                    return {"name": data["name"], "arguments": args}
                start = -1
    return None


def exec_action(workspace: Path, action: dict) -> str:
    name = action.get("name")
    args = action.get("arguments") or {}
    if name == "read":
        target = workspace / str(args.get("path", ""))
        return target.read_text(encoding="utf-8", errors="replace")[:2000] if target.exists() else "ERROR: no file"
    if name == "ls":
        target = workspace / str(args.get("path", "."))
        if not target.exists():
            return "ERROR: no dir"
        return " ".join(sorted(p.name for p in target.iterdir()))[:600]
    if name in ("write", "edit"):
        target = workspace / str(args.get("path", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "write":
            target.write_text(str(args.get("content", "")), encoding="utf-8")
            return f"wrote {target.name}"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        old = str(args.get("oldText", ""))
        if not old or old not in content:
            return "ERROR: oldText not found"
        target.write_text(content.replace(old, str(args.get("newText", "")), 1), encoding="utf-8")
        return f"edited {target.name}"
    if name == "bash":
        try:
            proc = subprocess.run(
                str(args.get("command", "true")), shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
            )
            return ((proc.stdout or "") + (proc.stderr or ""))[:1000]
        except subprocess.TimeoutExpired:
            return "ERROR: timeout"
    return f"ERROR: unknown {name}"


def run_tests(workspace: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=workspace, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


def load_model(use_adapter: bool):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    tok = AutoTokenizer.from_pretrained(MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
    )
    if use_adapter and Path(ADAPTER, "adapter_config.json").exists():
        model = PeftModel.from_pretrained(model, ADAPTER)
        print(f"已加载 adapter: {ADAPTER}", flush=True)
    model.eval()
    return model, tok


def generate(model, tok, prompt: str, max_new: int = 300) -> str:
    import torch

    enc = tok(prompt, return_tensors="pt", truncation=True, max_length=2500).to("cuda")
    with torch.no_grad():
        out = model.generate(
            **enc, max_new_tokens=max_new, do_sample=False,
            pad_token_id=tok.eos_token_id,
        )
    return tok.decode(out[0][enc["input_ids"].shape[1] :], skip_special_tokens=True)


async def eval_tasks(model, tok, task_ids: list[str], max_steps: int) -> list[dict]:
    """在真实任务上评测。"""
    from experiments.agent_rl.task import generate_all
    from experiments.agent_rl import tasks as _tasks  # noqa: F401

    all_tasks = {t.task_id: t for t in generate_all()}
    results = []

    for task_id in task_ids:
        task = all_tasks.get(task_id)
        if task is None:
            print(f"  跳过 {task_id}（不存在）", flush=True)
            continue

        print(f"\n  {task.task_id} ({task.meta.get('num_stages', '?')} 阶段) ...", flush=True)
        started = time.time()

        with tempfile.TemporaryDirectory(prefix="ab-") as tmp:
            ws = Path(tmp) / "ws"
            task.materialize(ws)
            ok0, _ = run_tests(ws)

            files_list = "\n".join(f"  {n}" for n in sorted(task.files))
            base_prompt = f"{SYSTEM}\n\n=== TASK ===\n{task.prompt}\n\n=== FILES ===\n{files_list}\n\n"
            history = []
            tools = []
            success = False

            for step in range(max_steps):
                convo = base_prompt + "\n".join(history[-4:]) + "\n=== YOUR TURN ===\n"
                text = generate(model, tok, convo)
                if "DONE" in text.strip()[-30:]:
                    tools.append("done")
                    break
                action = parse_action(text)
                if action is None:
                    tools.append("?")
                    history.append("[observation] ERROR: no JSON action found.")
                    continue
                tool = action.get("name", "?")
                tools.append(tool)
                obs = exec_action(ws, action)
                ok, out = run_tests(ws)
                tail = out.strip().splitlines()[-1][:60] if out.strip() else ""
                if ok:
                    success = True
                    break
                history.append(f"[observation] {obs[:500]}\n[tests] {tail}")
                if sum(len(h) for h in history) > 6000:
                    history = history[-3:]

            if not success:
                ok_final, _ = run_tests(ws)
                success = ok_final

            results.append(
                {
                    "task_id": task.task_id,
                    "success": success,
                    "steps": len(tools),
                    "tools": tools,
                    "duration_s": round(time.time() - started, 1),
                }
            )
            print(f"    {'OK  ' if success else 'FAIL'} {len(tools)} 步 {time.time()-started:.0f}s", flush=True)
            print(f"    工具: {' -> '.join(tools[:12])}", flush=True)

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="baseline", choices=["baseline", "train", "eval"])
    parser.add_argument("--tasks", default="t8e-001,t8e-002")
    parser.add_argument("--steps", type=int, default=20)
    args = parser.parse_args()

    task_ids = [t.strip() for t in args.tasks.split(",")]

    if args.mode == "train":
        return train()

    use_adapter = args.mode == "eval"
    print(f"=== {args.mode.upper()} 评测 ===", flush=True)
    model, tok = load_model(use_adapter)
    results = asyncio.run(eval_tasks(model, tok, task_ids, args.steps))

    ok = sum(1 for r in results if r["success"])
    print(f"\n通过率: {ok}/{len(results)} = {ok/len(results):.0%}" if results else "无结果")

    out = RESULTS / f"ab_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out}", flush=True)
    return 0


def train() -> int:
    """用真实任务 prompt 跑 GRPO。"""
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import GRPOConfig, GRPOTrainer

    from experiments.agent_rl.grpo_reward import make_grpo_reward_fn

    print("=== GRPO 训练（Qwen3-1.7B）===", flush=True)

    tok = AutoTokenizer.from_pretrained(MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="cuda:0", dtype=torch.bfloat16
    )
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=8, lora_alpha=16,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    # 数据：真实任务的 prompt
    from experiments.agent_rl.task import generate_all
    from experiments.agent_rl import tasks as _tasks  # noqa: F401

    all_tasks = {t.task_id: t for t in generate_all() if "boundary" in t.template or t.task_id.startswith("t8e")}
    rows = []
    for task in all_tasks.values():
        files_list = "\n".join(f"  {n}" for n in sorted(task.files))
        text = f"{SYSTEM}\n\n=== TASK ===\n{task.prompt}\n\n=== FILES ===\n{files_list}\n"
        rows.append({"prompt": [{"role": "user", "content": text[:2000]}]})
    print(f"训练样本: {len(rows)}", flush=True)

    ds = Dataset.from_list(rows)
    cfg = GRPOConfig(
        output_dir=ADAPTER,
        max_steps=30,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=1,
        num_generations=2,
        max_completion_length=200,
        learning_rate=1e-5,
        logging_steps=5,
        save_steps=30,
        save_total_limit=1,
        bf16=True,
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        temperature=1.0,
    )
    trainer = GRPOTrainer(
        model=model, args=cfg, train_dataset=ds,
        processing_class=tok, reward_funcs=[make_grpo_reward_fn()],
    )
    print("开始训练...", flush=True)
    result = trainer.train()
    trainer.save_model(ADAPTER)
    tok.save_pretrained(ADAPTER)
    print(f"\n最终 loss: {result.training_loss:.4f}", flush=True)
    print(f"显存峰值: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)
    print(f"adapter 已保存到 {ADAPTER}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
