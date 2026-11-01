"""把本地 Qwen3.5-2B（4bit）接入现有沙盒评测管道。

提供与仓库 LLM 客户端相同的接口（achat / achat_stream），
这样 `experiments.agent_rl.runner.run_task_once` 可以直接用它跑任务。

用法（E 盘 venv）：
    E:\\agent-rl\\.venv\\Scripts\\python.exe qwen_client.py --task t1-002
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

MODEL_PATH = r"E:\agent-rl\models\Qwen3.5-2B"

_model = None
_tokenizer = None


def _load() -> tuple[Any, Any]:
    """懒加载模型（4bit），进程内复用。"""
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    _model = AutoModelForCausalLM.from_pretrained(
        MODEL_PATH, quantization_config=bnb, device_map="cuda", torch_dtype=torch.bfloat16
    )
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    return _model, _tokenizer


TOOL_SCHEMA = """可用工具（JSON 格式回答）：
- {"name": "ls", "arguments": {"path": "<目录>"}}
- {"name": "read", "arguments": {"path": "<文件路径>"}}
- {"name": "write", "arguments": {"path": "<文件路径>", "content": "<完整内容>"}}
- {"name": "edit", "arguments": {"path": "<文件路径>", "oldText": "<原文>", "newText": "<新文>"}}
- {"name": "bash", "arguments": {"command": "<命令>"}}

回答格式：先简短说明思路，然后给出一行 JSON：{"name": ..., "arguments": {...}}
如果任务已完成，只回答：DONE"""


class QwenClient:
    """模拟仓库 LLM 接口的本地模型客户端。"""

    def __init__(self, model: str = "qwen3.5-2b", temperature: float = 0.7, max_new_tokens: int = 512) -> None:
        self.model = model
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens
        self.calls = 0
        self.tokens_generated = 0

    def _render(self, messages: list[Any]) -> str:
        model, tok = _load()
        converted = []
        for message in messages:
            role = getattr(message, "role", None) or (message.get("role") if isinstance(message, dict) else "user")
            content = getattr(message, "content", None) or (message.get("content") if isinstance(message, dict) else "")
            if role == "system":
                continue
            converted.append({"role": "user" if role in ("user", "tool") else "assistant", "content": str(content)[:6000]})
        if not converted:
            converted = [{"role": "user", "content": "开始任务"}]
        return tok.apply_chat_template(converted, tokenize=False, add_generation_prompt=True)

    def _generate(self, prompt: str) -> tuple[str, int]:
        import torch

        model, tok = _load()
        inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=6000).to("cuda")
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=self.temperature > 0,
                temperature=self.temperature if self.temperature > 0 else None,
                top_p=0.95 if self.temperature > 0 else None,
                pad_token_id=tok.eos_token_id,
            )
        new_tokens = out.shape[1] - inputs["input_ids"].shape[1]
        text = tok.decode(out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True)
        self.calls += 1
        self.tokens_generated += int(new_tokens)
        return text, int(new_tokens)

    async def achat(self, messages, tools=None, model=None):
        from my_agent_llm import Response

        prompt = self._render(messages)
        text, _ = self._generate(prompt)
        return Response(content=text, model=self.model)

    async def achat_stream(self, messages, tools=None, model=None):
        from my_agent_llm import StreamChunk

        prompt = self._render(messages)
        text, _ = self._generate(prompt)
        yield StreamChunk(content=text)


def parse_tool_call(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一条工具调用 JSON。"""
    if "DONE" in text.strip()[-50:]:
        return None
    matches = re.findall(r'\{"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', text, re.S)
    if not matches:
        return None
    name, args_raw = matches[-1]
    try:
        args = json.loads(args_raw)
    except json.JSONDecodeError:
        return None
    return {"name": name, "arguments": args}


def main() -> int:
    import asyncio

    sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness")
    sys.path.insert(0, r"C:\Users\MLL\Desktop\my-agent-harness\src")
    from experiments.agent_rl.runner import run_task_once
    from experiments.agent_rl.task import generate_all
    from experiments.agent_rl import tasks as _tasks  # noqa: F401

    task_id = sys.argv[sys.argv.index("--task") + 1] if "--task" in sys.argv else "t1-002"
    tasks = {t.task_id: t for t in generate_all()}
    task = tasks[task_id]
    client = QwenClient(temperature=0.0)
    t0 = time.time()
    episode = asyncio.run(run_task_once(task, model_client=client))
    print(f"任务 {task_id}: {'OK' if episode.success else 'FAIL'}")
    print(f"步数 {episode.steps}, 耗时 {time.time()-t0:.0f}s, 生成 {client.tokens_generated} tokens")
    print("工具序列:", [c["tool"] for c in episode.tool_calls if c["phase"] == "start"][:15])
    print("判定:", episode.check_tail[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
