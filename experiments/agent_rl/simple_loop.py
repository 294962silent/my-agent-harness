"""极简 agent 循环：专为本地小模型（Qwen3.5-2B）设计。

为什么不用仓库的 ReAct 微内核：那套依赖原生 tool_calls 协议，
而 2B 模型只会输出文本。硬套会得到大量无法解析的调用。

这里用最小实现：
1. 把任务题面 + 文件树 + 工具说明塞进 prompt；
2. 模型输出一步动作（JSON）；
3. 执行动作，把结果回填；
4. 最多 N 步，或模型说 DONE 时结束；
5. 用沙盒跑判定拿奖励。

这个循环与 GRPO 天然兼容：每步就是一次策略采样，轨迹可直接用于训练。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.agent_rl.sandbox import Sandbox
from experiments.agent_rl.task import Task

SYSTEM = """You are fixing code in a small repository. Output exactly ONE JSON action per turn, nothing else.

Actions:
{"name":"read","arguments":{"path":"FILE"}}
{"name":"write","arguments":{"path":"FILE","content":"FULL CONTENT"}}
{"name":"edit","arguments":{"path":"FILE","oldText":"OLD","newText":"NEW"}}
{"name":"bash","arguments":{"command":"CMD"}}
{"name":"done","arguments":{}}

IMPORTANT:
- Output ONLY the JSON object. No explanation, no markdown fences.
- Do NOT use "done" until the tests actually pass.
- Read the test file first to learn what is expected.
"""


@dataclass
class Step:
    thought: str
    action: dict[str, Any] | None
    observation: str
    tokens: int = 0


@dataclass
class Rollout:
    task_id: str
    success: bool
    reward: float
    steps: list[Step] = field(default_factory=list)
    duration_s: float = 0.0
    total_tokens: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "reward": self.reward,
            "num_steps": len(self.steps),
            "duration_s": round(self.duration_s, 1),
            "total_tokens": self.total_tokens,
            "error": self.error,
            "trajectory": [
                {"thought": s.thought, "action": s.action, "observation": s.observation[:500]}
                for s in self.steps
            ],
        }


def _tree(root: Path, limit: int = 40) -> str:
    lines = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") and part != "." for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root)
        if path.is_file():
            lines.append(f"  {rel} ({path.stat().st_size}B)")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "  (empty)"


def _exec_action(box: Sandbox, workspace: Path, action: dict[str, Any]) -> str:
    """在沙盒里执行一个动作，返回观察结果。"""
    name = action.get("name")
    args = action.get("arguments") or {}

    if name == "ls":
        result = box.run(["ls", "-la", args.get("path", ".")])
        return result.stdout[:1500] or result.stderr[:500]
    if name == "read":
        path = args.get("path", "")
        target = workspace / path
        if not target.exists():
            return f"ERROR: {path} does not exist"
        return target.read_text(encoding="utf-8", errors="replace")[:2500]
    if name == "write":
        path = args.get("path", "")
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.get("content", ""), encoding="utf-8")
        return f"Wrote {path} ({len(args.get('content',''))} chars)"
    if name == "edit":
        path = args.get("path", "")
        target = workspace / path
        if not target.exists():
            return f"ERROR: {path} does not exist"
        content = target.read_text(encoding="utf-8")
        old = args.get("oldText", "")
        new = args.get("newText", "")
        if not old or old not in content:
            return "ERROR: oldText not found in file"
        target.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"Edited {path}"
    if name == "done":
        return "DONE"
    if name == "bash":
        result = box.run(["sh", "-lc", args.get("command", "true")])
        out = (result.stdout or "") + (result.stderr or "")
        return f"exit={result.exit_code}\n{out[:1500]}"
    return f"ERROR: unknown action {name}"


_VALID_NAMES = {"read", "write", "edit", "bash", "ls", "done"}


def parse_action(text: str) -> dict[str, Any] | None:
    """从模型输出解析动作。

    Qwen3.5 会在 <think> 块里写草稿、可能输出多个 JSON。策略：
    1. 去掉 <think>...</think> 里的内容（那是草稿，不是最终决定）；
    2. 用括号配对切出所有顶层 JSON 对象；
    3. 取**最后一个**合法动作（模型最终的决定在最后）。
    """
    import re as _re

    cleaned = _re.sub(r"<think>.*?</think>", "", text, flags=_re.S)
    if "<think>" in cleaned:  # 没有闭合的 think，取最后一个 </think> 之后
        cleaned = cleaned.split("</think>")[-1]

    candidates = _extract_json_objects(cleaned)
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if name not in _VALID_NAMES:
            continue
        args = data.get("arguments")
        if not isinstance(args, dict):
            args = {k: v for k, v in data.items() if k != "name"}
        return {"name": name, "arguments": args}
    return None


def _extract_json_objects(text: str) -> list[str]:
    """用括号配对切出所有顶层 {...} 片段（支持字符串里的转义）。"""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for index, ch in enumerate(text):
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
                start = index
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : index + 1])
                start = -1
    return out


def run_rollout(
    task: Task,
    generator,
    max_steps: int = 12,
    temperature: float = 0.7,
    system: str = SYSTEM,
) -> Rollout:
    """跑一条轨迹。generator(prompt) -> (text, n_tokens)。"""
    started = time.time()
    rollout = Rollout(task_id=task.task_id, success=False, reward=0.0)

    with _temp_ws() as workspace:
        task.materialize(workspace)
        history: list[str] = []
        signatures: list[str] = []
        preview = []
        for name in sorted(task.files):
            content = task.files[name]
            preview.append(f"--- {name} ---\n{content[:1200]}")
        prompt_prefix = (
            f"{system}\n\n=== TASK ===\n{task.prompt}\n\n"
            f"=== REPOSITORY FILES (already shown, no need to read again) ===\n"
            + "\n".join(preview)[:6000]
            + "\n\n"
        )

        with Sandbox(workspace, task_id=f"ro-{task.task_id}", timeout_s=task.timeout_s) as box:
            for step_index in range(max_steps):
                convo = prompt_prefix + "\n".join(history) + "\n=== YOUR TURN ===\n"
                text, ntok = generator(convo, temperature)
                rollout.total_tokens += ntok

                if "DONE" in text.strip()[-30:]:
                    rollout.steps.append(Step(thought=text[:300], action=None, observation="done", tokens=ntok))
                    break

                action = parse_action(text)
                if action is not None and action.get("name") == "done":
                    rollout.steps.append(Step(thought=text[:300], action=action, observation="agent finished", tokens=ntok))
                    break
                if action is None:
                    rollout.steps.append(Step(thought=text[:300], action=None, observation="ERROR: no action parsed", tokens=ntok))
                    history.append(
                        f"[assistant] {text[:400]}\n"
                        '[observation] ERROR: no JSON action found. Reply with ONLY: {"name":"read","arguments":{"path":"mod.py"}}'
                    )
                    continue

                observation = _exec_action(box, workspace, action)
                rollout.steps.append(Step(thought=text[:300], action=action, observation=observation, tokens=ntok))

                # 卡住检测：同一动作连续重复 2 次，或观察里出现 ERROR，就给出更强提示
                signature = json.dumps(action, sort_keys=True, ensure_ascii=False)[:200]
                repeated = step_index > 0 and signatures and signatures[-1] == signature
                signatures.append(signature)

                hint = ""
                if repeated:
                    hint = (
                        "\n[SYSTEM] You repeated the same action and it did not help. "
                        "The file content shown above is the CURRENT content. "
                        "Try a different approach: use write with the complete corrected file."
                    )
                elif observation.startswith("ERROR"):
                    hint = (
                        "\n[SYSTEM] Your last action failed. Read the file again and copy oldText EXACTLY "
                        "from the shown content."
                    )
                history.append(f"[assistant] {text[:400]}\n[observation] {observation[:1200]}{hint}")
                # 控制上下文长度
                if sum(len(h) for h in history) > 12000:
                    history = history[-4:]

            # 判定
            result = box.run(task.check_cmd)
            rollout.success = bool(result.ok)
            rollout.reward = 1.0 if rollout.success else 0.0
            rollout.error = None if rollout.success else result.tail(300)

    rollout.duration_s = time.time() - started
    return rollout


class _temp_ws:
    """临时工作区上下文。"""

    def __init__(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="arl-ro-")

    def __enter__(self) -> Path:
        return Path(self._tmp.name) / "ws"

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()
