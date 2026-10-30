"""Runner：用真实模型在一个沙盒里跑一个任务，并记录完整轨迹。

执行模型：
1. 在宿主机建一个临时 workspace，把任务的初始文件写进去；
2. 启动容器，把该目录挂载为 `/workspace`；
3. agent 的工具（read/write/edit/bash）都指向这个**同一个宿主机目录**——
   容器内看到的就是 model 写下的内容；
4. 任务结束后在容器里跑 check_cmd 判定；
5. 导出 `{task_id, messages, tool_calls, reward, meta}`。

这样既得到真实的隔离（容器里跑测试/命令），又让轨迹采集不需要跨容器搬运。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.agent_rl.sandbox import Sandbox
from experiments.agent_rl.task import Task

SYSTEM_PROMPT = """You are a coding agent working inside a small repository.

Rules:
- Use the available tools to inspect and modify files.
- Always verify your work by running the test command mentioned in the task.
- Do not modify test files or the expected output files.
- When the task is complete, reply with a short summary of what you changed.
"""


@dataclass
class Episode:
    """一次任务执行（一条轨迹）。"""

    task_id: str
    template: str
    difficulty: str
    sample_index: int
    model: str
    success: bool
    reward: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    check_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "template": self.template,
            "difficulty": self.difficulty,
            "sample_index": self.sample_index,
            "model": self.model,
            "success": self.success,
            "reward": self.reward,
            "steps": self.steps,
            "duration_s": round(self.duration_s, 2),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "error": self.error,
            "messages": self.messages,
            "tool_calls": self.tool_calls,
            "check_tail": self.check_tail[:1500],
        }


class TrajectoryRecorder:
    """挂在 agent 的事件/工具回调上，把过程记下来。"""

    def __init__(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.steps = 0

    def on_tool_start(self, event: Any) -> None:
        # ToolCallHook(tool_call_id, tool_name, args) —— 直接读属性
        self.tool_calls.append(
            {
                "phase": "start",
                "tool": getattr(event, "tool_name", "?"),
                "args": _safe(getattr(event, "args", {})),
            }
        )

    def on_tool_end(self, event: Any) -> None:
        # ToolResultHook(tool_call_id, tool_name, result, is_error)
        self.tool_calls.append(
            {
                "phase": "end",
                "tool": getattr(event, "tool_name", "?"),
                "ok": not bool(getattr(event, "is_error", False)),
                "result": _safe(getattr(event, "result", "")),
            }
        )


def _safe(value: Any, limit: int = 800) -> Any:
    """把任意对象压成可 JSON 化的短表示。"""
    try:
        if isinstance(value, str):
            return value[:limit]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): _safe(v, limit) for k, v in list(value.items())[:20]}
        if isinstance(value, (list, tuple)):
            return [_safe(v, limit) for v in list(value)[:20]]
        return str(value)[:limit]
    except Exception:
        return "<unserializable>"


async def run_task_once(
    task: Task,
    *,
    model_client: Any,
    sample_index: int = 0,
    max_steps: int = 25,
    timeout_s: int | None = None,
) -> Episode:
    """跑一次任务，返回一条 Episode。"""
    from my_agent_core.agent import Agent
    from my_agent_core.session import Session
    from my_coding_agent.tools.bash import make_bash_tool
    from my_coding_agent.tools.edit import make_edit_tool
    from my_coding_agent.tools.find import make_find_tool
    from my_coding_agent.tools.grep import make_grep_tool
    from my_coding_agent.tools.ls import make_ls_tool
    from my_coding_agent.tools.read import make_read_tool
    from my_coding_agent.tools.write import make_write_tool

    started = time.time()
    episode = Episode(
        task_id=task.task_id,
        template=task.template,
        difficulty=task.difficulty,
        sample_index=sample_index,
        model=getattr(model_client, "model", "unknown"),
        success=False,
        reward=0.0,
    )

    with tempfile.TemporaryDirectory(prefix=f"run-{task.task_id}-") as tmp:
        workspace = Path(tmp) / "workspace"
        task.materialize(workspace)

        tools = [
            make_read_tool(workspace),
            make_write_tool(workspace),
            make_edit_tool(workspace),
            make_bash_tool(workspace),
            make_grep_tool(workspace),
            make_find_tool(workspace),
            make_ls_tool(workspace),
        ]

        session = Session(path=Path(tmp) / "session.jsonl")
        agent = Agent(
            llm=model_client,
            tools=tools,
            session=session,
            system_prompt=SYSTEM_PROMPT,
            model=getattr(model_client, "model", None),
            memory_dir=False,
        )

        from my_agent_core.hooks import ToolCallHook, ToolResultHook

        recorder = TrajectoryRecorder()
        agent.hooks.register(ToolCallHook, recorder.on_tool_start)
        agent.hooks.register(ToolResultHook, recorder.on_tool_end)

        try:
            await asyncio.wait_for(agent.run(task.prompt), timeout=timeout_s or task.timeout_s * 3)
        except asyncio.TimeoutError:
            episode.error = "agent_timeout"
        except Exception as exc:  # noqa: BLE001
            episode.error = f"{type(exc).__name__}: {exc}"

        # 判定：在容器里跑 check_cmd
        with Sandbox(workspace, task_id=task.task_id, timeout_s=task.timeout_s) as box:
            result = box.run(task.check_cmd)
        episode.success = bool(result.ok and not episode.error)
        episode.reward = 1.0 if episode.success else 0.0
        episode.check_tail = result.tail(800)

        episode.messages = [_message_to_dict(m) for m in agent.messages]
        episode.tool_calls = recorder.tool_calls
        episode.steps = sum(1 for c in recorder.tool_calls if c["phase"] == "start")
        episode.duration_s = time.time() - started
        episode.tokens_in = _sum_tokens(agent.messages, "input")
        episode.tokens_out = _sum_tokens(agent.messages, "output")

    return episode


def _message_to_dict(message: Any) -> dict[str, Any]:
    return {
        "role": getattr(message, "role", "?"),
        "content": _safe(getattr(message, "content", ""), limit=4000),
        "metadata": _safe(getattr(message, "metadata", None) or {}),
    }


def _sum_tokens(messages: list[Any], kind: str) -> int:
    total = 0
    for message in messages:
        meta = getattr(message, "metadata", None) or {}
        usage = meta.get("usage") or {}
        value = usage.get("prompt_tokens" if kind == "input" else "completion_tokens")
        if isinstance(value, int):
            total += value
    return total


def save_episodes(episodes: list[Episode], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for episode in episodes:
            fh.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")


def load_episodes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def make_model_client(model: str | None = None, temperature: float = 0.7) -> Any:
    """构造 DeepSeek 客户端（复用仓库已有的鉴权与网关配置）。"""
    from my_agent_llm import Config, LLM

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if not api_key:
        # 仓库的凭据中心：~/.my-pi-agent/auth.json -> providers.<provider>.<profile>.{key, base_url}
        auth_path = Path.home() / ".my-pi-agent" / "auth.json"
        if auth_path.exists():
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            providers = data.get("providers") or {}
            for provider_name in ("openai", "deepseek"):
                profile = providers.get(provider_name) or {}
                for entry in profile.values() if isinstance(profile, dict) else []:
                    if isinstance(entry, dict) and entry.get("key"):
                        api_key = entry["key"]
                        base_url = base_url or entry.get("base_url")
                        break
                if api_key:
                    break
    if not api_key:
        raise RuntimeError("缺少 API key：请设置 DEEPSEEK_API_KEY 或 ~/.my-pi-agent/auth.json")

    config = Config(
        api_key=api_key,
        base_url=base_url,
        model=model or "deepseek-v4.1-flash",
        temperature=temperature,
        max_tokens=4096,
        timeout=180,
    )
    return LLM(config)
