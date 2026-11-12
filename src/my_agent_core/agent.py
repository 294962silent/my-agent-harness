# pyright: reportImportCycles=false
"""单层 Agent —— 状态 + 循环 + 工具执行全在一个类（原生异步驱动）。

模型调用 → 检查 tool_calls → 执行工具 → 观察结果写回消息 → 循环，
直到模型不再发起工具调用（经典退出条件：tool_calls 为空 → 结束）。

模型边界交给 my-agent-llm 的 LLM 门面；消息状态是 Message 对象列表。
"""

from __future__ import annotations

import logging

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from my_agent_core.background import (  # pyright: ignore[reportMissingImports]
    BackgroundRunner,
)
from my_agent_core.context import ContextManager, ContextSessionBridge
from my_agent_core.events import (
    AgentEnd,
    AgentStart,
    ContextCompacted,
    Event,
    MessageEnd,
    MessageUpdate,
    TurnEnd,
)
from my_agent_core.extensions import ExtensionManager
from my_agent_core.hooks import (  # pyright: ignore[reportMissingImports]
    AgentStartHook,
    HookRegistry,
    HookResult,
    UserInputHook,
)
from my_agent_core.loop import CancellationToken, run_agent_loop, _synthesize_interrupted_tool_calls
from my_agent_core.run_lifecycle import RunLimits, RunState, RunLimitExceeded, RunAborted
from my_agent_core.memory import MemoryStore, make_memory_tool, make_memory_read_tool
from my_agent_core.memory_distillation import MemoryDistiller, SOURCE_TEXT, input_text, is_trivial_memory_query
from my_agent_core.message_queue import MessageQueue, QueuedMessage
from my_agent_core.plugins import PluginManager
from my_agent_core.registry import ToolRegistry
from my_agent_core.retry import AutoRetryPolicy  # pyright: ignore[reportMissingImports]
from my_agent_core.session import Session
from my_agent_core.runtime_context import RuntimeContext, projection_message
from my_agent_core.skills import Skill, SkillManager
from my_agent_core.subagents import SubagentManager
from my_agent_core.task_store import TaskStore  # pyright: ignore[reportMissingImports]
from my_agent_core.tool_history import clean_provider_context
from my_agent_core.tools import Tool
from my_agent_core.tools.builtin.task import (
    make_task_tool,  # pyright: ignore[reportMissingImports]
)
from my_agent_core.tools.builtin.task_tools import (  # pyright: ignore[reportMissingImports]
    TaskGuardHook,
    make_todo_tool,
)
from my_agent_llm.observation import observation_context
from my_agent_llm import LLM, Message, MessageContent, TextContent  # pyright: ignore[reportMissingImports]


def _parse_distilled_facts(payload: str) -> list[dict[str, str]]:
    """从模型输出里抠出 JSON 数组；容忍 ```json 包裹与前后噪声。"""
    import json
    import re as _re

    text = payload.strip()
    fenced = _re.search(r"```(?:json)?\s*(.+?)```", text, _re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if key and value:
            out.append({"key": key, "value": value})
    return out[:5]


_LOG = logging.getLogger(__name__)


class Agent:
    """单层 Agent：持有 llm / 工具注册表 / 消息，内联 ReAct 异步循环。"""

    # ── 构造与装配 ──────────────────────────────────────────

    def __init__(
        self,
        *,
        llm: LLM,
        tools: list[Tool],
        session: Session,
        system_prompt: str | None = None,
        context_budget: int | None = None,
        keep_recent_tokens: int | None = None,
        skill_dirs: Sequence[str | Path] | None = None,
        model: str | None = None,
        subagent_dirs: Sequence[str | Path] | None = None,
        extension_dirs: Sequence[str | Path] | None = None,
        plugin_dirs: Sequence[str | Path] | None = None,
        memory_dir: str | Path | None | Literal[False] = None,
        memory_index=None,
        memory_index_owned: bool = False,
        memory_index_close_timeout: float = 0.1,
        memory_autodistill: bool = False,
        memory_idle_seconds: float = 30,
        memory_distill_timeout: float = 15,
        task_store: TaskStore | Path | str | None | Literal[False] = None,
        steering_mode: Literal["one-at-a-time", "all"] = "one-at-a-time",
        followup_mode: Literal["one-at-a-time", "all"] = "one-at-a-time",
        hooks: list[tuple[type, Callable[..., Any]]] | None = None,
        retry_policy: AutoRetryPolicy | None = None,
        artifact_store: Any | None = None,
        run_limits: RunLimits | None = None,
    ):
        """各参数语义见框架设计文档 §4.3（hook 通过 register_hook 挂载）。

        session 必填：run() 内每条消息落盘；构造时从 session 当前路径恢复纯对话，
        并用 system_prompt + skill 清单 + subagent 清单拼 system（消息首条）。
        context_budget 为 context 预算：None → 用 ContextManager 默认（100k）；显式传 → 覆盖。
        context 默认启用（每次 llm.chat 前 prepare 压缩视图）。
        skill_dirs 为 skill 机制来源：None → 探测 <cwd>/.agents/skills（不存在则空）；
        [] → 显式禁用；非空 list → 只扫这些目录。构造 skill_manager 追加清单块进
        system；self.skill_manager 公开可读，self.skills 为动态计算属性（@property 代理 manager.list()）。
        正文由宿主 invoke_skill 显式注入（模型侧无 read 工具）。
        subagent_dirs 三态同 skill_dirs：None → 探测 <cwd>/.agents/agents；[] → 禁用；
        非空 → 只扫这些目录。有 agent 时清单追加进 system，且自动装配 task 工具。
        extension_dirs 三态同 skill_dirs/subagent_dirs：None → 探测 <cwd>/.agents/extensions；
        [] → 禁用；非空 → 只扫这些目录。extension 在 _register_tools 之后加载，注册的工具可覆盖
        内置工具（对齐 pi）；hook 注册进 hooks（先于构造参数 hooks 触发）；命令存 extension_manager，
        上层 CLI 调 handle_command 派发。
        plugin_dirs 为 Claude Code 格式插件目录：None → 探测 <cwd>/.agents/plugins；
        [] → 显式禁用；非空 list → 扫各插件目录并自动解构其 skills/、agents/ 注入对应管理器。
        memory_dir 为持久化记忆存储目录：None → 探测 <cwd>/.my_agent_core/memory（存在才启用）；
        False → 显式禁用；str | Path → 显式指定目录。启用时构造 MemoryStore 并冻结快照，
        自动注入 <MEMORY_CONTEXT> 块进 system，并自动注册 memory 工具（add/replace/remove）。
        """
        self.llm = llm
        self.model = model  # 缺省 inherit：None 时 llm.chat 用 LLM 自身配置
        self.session = session
        self.artifact_store = artifact_store
        # 运行态事实的显式来源：只由宿主通过 set/update/clear 写入，按快照交付。
        self.runtime_context = RuntimeContext()
        self._system_prompt = system_prompt  # 保存，reset 重拼用
        self._aborted = False  # 中止状态标记
        self._current_signal: CancellationToken | None = None
        self.run_limits = run_limits or RunLimits()
        if not isinstance(self.run_limits, RunLimits):
            raise TypeError("run_limits must be RunLimits or None")
        self._active_run: RunState | None = None
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._subscribers: list[Callable[[Event], Any]] = []
        self.hooks = HookRegistry()
        self.registry = ToolRegistry()
        self.plugin_manager = PluginManager(plugin_dirs)
        self.skill_manager = SkillManager(
            skill_dirs, extra_dirs=self.plugin_manager.get_skill_dirs()
        )  # None→探测默认 / []→禁用 / 显式→目录
        self.subagent_manager = SubagentManager(
            subagent_dirs, extra_dirs=self.plugin_manager.get_subagent_dirs()
        )  # 三态同 skill_dirs

        self.memory_index = memory_index
        self.memory_index_owned = memory_index_owned
        self.memory_index_close_timeout = memory_index_close_timeout
        self.memory_index_close_status: bool | None = None
        self.memory_store = self._init_memory_store(memory_dir)  # memory 装配与快照冻结
        if self.memory_store is not None:
            self.memory_store.memory_index = memory_index
        # 会话结束时的记忆蒸馏（sleep-time）：默认关闭，打开后每轮结束抽取稳定事实
        self.memory_autodistill = bool(memory_autodistill)
        self._distill_cursor = 0
        self._memory_distiller = MemoryDistiller(self, idle_seconds=memory_idle_seconds, timeout=memory_distill_timeout)

        self.task_store = self._init_task_store(task_store)  # 任务看板仓库装配
        self.retry_policy = retry_policy or AutoRetryPolicy()  # 大模型自动重试策略
        self.message_queue = MessageQueue(
            steering_mode=steering_mode, followup_mode=followup_mode
        )  # 动态干预消息队列 (Pi-style steer & followup)
        self.background_runner = BackgroundRunner(self.message_queue)  # 后台异步执行器与孤儿进程防御调度引擎

        self._register_tools(tools)  # ① 工具注册统一（用户 + 内置 task + 内置 memory）
        self.extension_manager = ExtensionManager(self, extension_dirs)  # extension 装配
        self._extensions_loaded = False
        self.messages = self._init_messages(session, system_prompt)  # ② 拼 system + 恢复
        self._init_context(session, context_budget, keep_recent_tokens)  # ③ context 装配
        self._register_hooks(hooks)  # ④ hooks 批量注册

    def _init_task_store(self, task_store: TaskStore | Path | str | None | Literal[False]) -> TaskStore | None:
        """解析 task_store 三态并初始化 TaskStore：
        False → 显式禁用；
        TaskStore 实例 → 直接复用；
        str | Path → 指定工作区初始化；
        None → 探测 <cwd>/.my_agent_core/tasks.json（存在才启用）。
        """
        if isinstance(task_store, bool) and not task_store:
            return None
        if isinstance(task_store, TaskStore):
            return task_store
        if task_store is not None:
            return TaskStore(task_store)
        default_file = Path.cwd() / ".my_agent_core" / "tasks.json"
        if default_file.exists() and default_file.is_file():
            return TaskStore(Path.cwd())
        return None

    def _init_memory_store(self, memory_dir: str | Path | None | Literal[False]) -> MemoryStore | None:
        """解析 memory_dir 三态并初始化 MemoryStore：
        False → 显式禁用；
        None → 探测 <cwd>/.my_agent_core/memory（存在才启用）；
        str | Path → 显式指定目录。
        """
        if isinstance(memory_dir, bool) and not memory_dir:
            return None
        if memory_dir is not None:
            store = MemoryStore(memory_dir)
            store.load_from_disk()
            return store
        default_dir = Path.cwd() / ".my_agent_core" / "memory"
        if default_dir.exists() and default_dir.is_dir():
            store = MemoryStore(default_dir)
            store.load_from_disk()
            return store
        return None

    def _register_tools(self, tools: list[Tool]) -> None:
        """注册用户工具 + 内置 task 工具 + 内置 memory 工具（撞名 ValueError）。"""
        for t in tools:
            self.registry.register(t)
        if self.subagent_manager:
            if self.registry.get("task") is not None:
                raise ValueError("Tool name 'task' conflicts with the built-in subagent delegation tool")
            self.registry.register(make_task_tool(self.subagent_manager, self))
        if self.memory_store:
            if self.registry.get("memory") is not None:
                raise ValueError("Tool name 'memory' conflicts with the built-in memory tool")
            self.registry.register(make_memory_tool(self.memory_store))
            if self.registry.get("memory_read") is not None:
                raise ValueError("Tool name 'memory_read' conflicts with the built-in memory reader")
            self.registry.register(make_memory_read_tool(self.memory_store))
        if self.task_store:
            todo_tool = make_todo_tool(self.task_store)
            if self.registry.get(todo_tool.name) is not None:
                raise ValueError(f"Tool name '{todo_tool.name}' conflicts with built-in todo tool")
            self.registry.register(todo_tool)

    def _init_messages(self, session: Session, system_prompt: str | None) -> list[Message]:
        """拼 system（Agent 配置）+ 恢复 session 纯对话，合成初始 messages。"""
        mem_prompt = self.memory_store.format_all_for_system_prompt() if self.memory_store else None
        parts = [
            p
            for p in (
                system_prompt or "",
                self.skill_manager.format_prompt(),
                self.subagent_manager.format_prompt(),
                mem_prompt,
            )
            if p
        ]
        messages = session.get_full_history_messages()  # 纯对话（不含 system）
        if parts:
            messages.insert(0, Message(role="system", content="\n\n".join(parts)))
        return messages

    def _init_context(
        self,
        session: Session,
        context_budget: int | None,
        keep_recent_tokens: int | None,
    ) -> None:
        """装配 context 管理（默认启用）：context_budget None → 用 ContextManager 默认 budget。"""
        self._ctx_bridge = ContextSessionBridge(session)
        self._ctx = ContextManager(
            llm=self.llm,
            keep_recent_tokens=keep_recent_tokens,
            results_dir=self._ctx_bridge.results_dir(),
            artifact_store=self.artifact_store,
            before_compact=self._memory_distiller.precompress if self.memory_autodistill else None,
            before_compact_timeout=self._memory_distiller.timeout,
            **({} if context_budget is None else {"budget": context_budget}),
        )
        cache = self._ctx_bridge.restore_cache(self._ctx)
        self._ctx_cache_entry_id = (cache or {}).get("entry_id")
        self._ctx_session_identity = (self.session.id, str(self.session.path.resolve()))

    def _mark_summary_replaced(self, reason: str) -> None:
        """摘要视图替换时留下显式诊断原因；普通 LLM 无诊断接口则静默跳过。"""
        provider = getattr(self.llm, "_provider", None)
        projection = getattr(provider, "request_projection", None) or getattr(self.llm, "request_projection", None)
        if projection is not None and hasattr(projection, "mark_message_rewrite"):
            projection.mark_message_rewrite(reason)

    def _sync_context_cache(self) -> None:
        """每轮模型请求前对齐当前分支的摘要缓存（分支切换/恢复/Session 替换）。

        同一分支正常追加时 entry 身份不变 → 不做任何重载或重新摘要。
        """
        cache = self.session.get_latest_compaction_cache()
        identity = (cache or {}).get("entry_id")
        session_identity = (self.session.id, str(self.session.path.resolve()))
        session_changed = session_identity != getattr(self, "_ctx_session_identity", None)
        if not session_changed and identity == getattr(self, "_ctx_cache_entry_id", None):
            return
        self._ctx_bridge = ContextSessionBridge(self.session)
        self._ctx.results_dir = self._ctx_bridge.results_dir()
        if cache:
            self._ctx.restore_cache(
                summary=cache["summary"],
                covered_count=cache["covered_count"],
                retained_tail=cache["retained_tail"],
                entry_id=cache.get("entry_id"),
                provenance_version=cache.get("provenance_version"),
                covered_ids=cache.get("covered_ids"),
                first_kept_id=cache.get("first_kept_id"),
                source_path_id=cache.get("source_path_id"),
                covered_message_keys=cache.get("covered_message_keys"),
            )
            self._mark_summary_replaced("summary_replaced")
        elif session_changed or getattr(self, "_ctx_cache_entry_id", None) is not None:
            self._ctx.invalidate_cache("branch_or_session_changed")
            self._mark_summary_replaced("summary_dropped")
        self._ctx_cache_entry_id = identity
        self._ctx_session_identity = session_identity

    def _register_hooks(self, hooks: list[tuple[type, Callable[..., Any]]] | None) -> None:
        """构造时批量注册 hooks / 决策回调（对称 _register_tools）。"""
        if self.task_store:
            guard = TaskGuardHook(self.task_store, self.steer)
            self.subscribe(lambda ev: guard.on_agent_start(ev) if isinstance(ev, AgentStart) else None)
            self.subscribe(lambda ev: guard.on_turn_end(ev) if isinstance(ev, TurnEnd) else None)
        for target, callback in hooks or []:
            self.hooks.register(target, callback)

    # ── 公共 API ────────────────────────────────────────────

    @property
    def context_manager(self) -> ContextManager:
        """底层上下文管理器。"""
        return self._ctx

    @property
    def skills(self) -> list[Skill]:
        """动态获取当前注册的全部技能列表。"""
        return self.skill_manager.list()

    @property
    def system_prompt(self) -> str | None:
        """Agent 配置的初始系统提示词。"""
        return self._system_prompt

    def abort(self) -> None:
        """中止当前运行中的任务（取消流式输出，丢弃未完成半截文本并清空干预队列）。"""
        self._memory_distiller.cancel()
        if self._active_run is not None:
            self._active_run.abort()
        self._aborted = True
        if self._current_signal is not None:
            self._current_signal.cancel()
        self.message_queue.clear()
        self._queue_background_cleanup()

    def subscribe(self, listener: Callable[[Event], Any]) -> Callable[[], None]:
        """订阅所有生命周期事件通知，返回取消订阅的回调函数。"""
        self._subscribers.append(listener)

        def unsubscribe() -> None:
            if listener in self._subscribers:
                self._subscribers.remove(listener)

        return unsubscribe

    def steer(self, message: MessageContent) -> None:
        """注入即时转向指令（在下一个安全点打断/干预模型执行路线）。"""
        self.message_queue.add_steering(message)

    def follow_up(self, message: MessageContent) -> None:
        """追加排队追问指令（在当前任务彻底完成后自动开启下一段任务）。"""
        self.message_queue.add_followup(message)

    def clear_queue(self) -> list[QueuedMessage]:
        """清空当前排队的干预消息（包含 Steering 与 Follow-up），并返回被清除的消息列表。"""
        return self.message_queue.clear()

    def _get_steering_messages(self) -> Sequence[MessageContent]:
        """为底层循环提取当前排队的 steer 消息。"""
        if self.message_queue.has_steering():
            return [m.content for m in self.message_queue.get_steering_messages()]
        return []

    def _get_follow_up_messages(self) -> Sequence[MessageContent]:
        """为底层循环提取当前排队的 follow-up 消息。"""
        if self.message_queue.has_followup():
            return [m.content for m in self.message_queue.get_followup_messages()]
        return []

    @property
    def active_run_id(self) -> str | None:
        return self._active_run.run_id if self._active_run else None

    def _queue_background_cleanup(self) -> None:
        task = asyncio.create_task(self.background_runner.cancel_all())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_finished)

    def _cleanup_finished(self, task: asyncio.Task) -> None:
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _drain_cleanup(self) -> None:
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

    async def prompt_stream(self, user_input: MessageContent) -> AsyncIterator[Event]:
        """One owned run; consumer cancellation propagates after nested cleanup."""
        if self._active_run is not None:
            raise RuntimeError("Agent prompt is already running")
        state = RunState(self.run_limits, CancellationToken())
        self._active_run = state
        self._current_signal = state.signal
        self._aborted = False
        stream = self._prompt_stream_run(user_input, state)
        state.stream = stream
        ended = False
        completed = False
        try:
            while True:
                try:
                    with observation_context(root_id=state.run_id, purpose="agent"):
                        event = await state.next_event(stream)
                except StopAsyncIteration:
                    break
                if isinstance(event, MessageUpdate) and event.message.role == "assistant":
                    state.partial = event.message
                elif isinstance(event, MessageEnd) and event.message.role == "assistant":
                    state.partial = None
                if isinstance(event, AgentEnd):
                    ended = True
                    completed = event.stop_reason == "end_turn"
                    event = replace(event, run_id=state.run_id)
                yield event
        except (RunLimitExceeded, RunAborted) as stop:
            state.signal.cancel()
            await self._close_interrupted_tool_group(state.partial)
            state.partial = None
            reason = "budget_exhausted" if isinstance(stop, RunLimitExceeded) else "cancelled"
            end = AgentEnd(messages=list(self.messages), final_text=None,
                           iterations=state.budget.turns, stop_reason=reason, run_id=state.run_id,
                           limit_details=stop.details if isinstance(stop, RunLimitExceeded) else None)
            await self._notify(end)
            yield end
        finally:
            state.signal.cancel()
            try:
                await stream.aclose()
                if not ended:
                    await self._close_interrupted_tool_group(state.partial)
                    state.partial = None
                    self._queue_background_cleanup()
                await self._drain_cleanup()
            finally:
                if self._active_run is state:
                    self._current_signal = None
                    self._active_run = None
                state.finished.set()
                if completed and not state.aborted:
                    with observation_context(root_id=state.run_id, purpose="agent"):
                        self._memory_distiller.schedule()

    async def _close_interrupted_tool_group(self, partial: Message | None) -> None:
        """Keep observed tool-call intent and append missing interruption results.

        Cancellation may interrupt the microkernel before its normal repair events.
        Never persist an unfinished assistant text without tool calls.
        """
        history = self.session.get_full_history_messages()
        if partial is not None and (partial.metadata or {}).get("tool_calls") and not (
            history and history[-1].role == "assistant"
            and (history[-1].metadata or {}).get("tool_calls") == (partial.metadata or {}).get("tool_calls")
        ):
            message = partial.model_copy(update={"metadata": {**(partial.metadata or {}), "stop_reason": "cancelled"}})
            await self.session.add_message_async(message.role, message.content, **(message.metadata or {}))
            self.messages.append(message)
            history.append(message)
        index = next((i for i in range(len(history) - 1, -1, -1) if history[i].role == "assistant"), None)
        if index is None or any(m.role != "tool" for m in history[index + 1:]):
            return
        calls = (history[index].metadata or {}).get("tool_calls") or []
        existing = {(m.metadata or {}).get("tool_call_id") for m in history[index + 1:]}
        for message in _synthesize_interrupted_tool_calls(calls):
            if (message.metadata or {}).get("tool_call_id") not in existing:
                await self.session.add_message_async(message.role, message.content, **(message.metadata or {}))
                self.messages.append(message)

    async def _prompt_stream_run(self, user_input: MessageContent, state: RunState) -> AsyncIterator[Event]:
        await self._memory_distiller.stop()
        if self._memory_distiller.closed:
            raise RuntimeError("Agent is closed")
        if not self._extensions_loaded:
            await self.extension_manager.load()
            self._extensions_loaded = True

        # ── Hook 1: UserInputHook 拦截与改写（在进入 Session 和消息历史之前触发）
        prompt = Message(role="user", content=user_input, metadata=(
            {SOURCE_TEXT: input_text(user_input)} if self.memory_autodistill and self.memory_store is not None else None
        ))
        # 混合输入逐个改写文字块，保留图片位置；纯图片仍触发一次空文字 Hook。
        text_parts: list[TextContent] = (
            [{"type": "text", "text": prompt.content}]
            if isinstance(prompt.content, str)
            else [part for part in prompt.content if part["type"] == "text"]
        )
        fallback_part: TextContent = {"type": "text", "text": ""}
        parts_to_inspect = text_parts if text_parts else [fallback_part]
        for part in parts_to_inspect:
            user_input_decision = await self.hooks.emit(UserInputHook(input_text=part["text"]))
            if isinstance(user_input_decision, HookResult):
                if user_input_decision.block:
                    reason = f": {user_input_decision.reason}" if user_input_decision.reason else ""
                    end_ev = AgentEnd(
                        messages=list(self.messages),
                        final_text=f"(blocked{reason})",
                        iterations=0,
                        stop_reason="blocked",
                    )
                    await self._notify(end_ev)
                    yield end_ev
                    return
                if user_input_decision.updated_input is not None:
                    part["text"] = user_input_decision.updated_input
        if isinstance(prompt.content, str):
            prompt.content = text_parts[0]["text"] if text_parts else fallback_part["text"]
        elif not text_parts and fallback_part["text"]:
            prompt.content.insert(0, fallback_part)

        # 每轮按当前输入检索相关记忆，只注入命中的片段。
        # 系统提示里的 <MEMORY_CONTEXT> 保持冻结（前缀稳定、KV-Cache 友好），
        # 这里补的是"这一轮用得上"的部分，避免整份记忆每轮重复注入。
        if self.memory_store is not None:
            if isinstance(prompt.content, str):
                memory_query = prompt.content
            else:
                memory_query = " ".join(
                    str(part.get("text", "")) for part in prompt.content if isinstance(part, dict)
                )
            retrieved_memory = None if is_trivial_memory_query(memory_query) else await self.memory_store.aretrieve(memory_query)
            if retrieved_memory:
                if isinstance(prompt.content, str):
                    prompt.content = f"{retrieved_memory}\n\n{prompt.content}"
                else:
                    prompt.content = [{"type": "text", "text": retrieved_memory}, *prompt.content]

        # 同步到 session 当前指针：rewind 后同 Agent 续跑时，内存 transcript 以文件为准。
        system = [m for m in self.messages if m.role == "system"]
        restored = system + self.session.get_full_history_messages()
        # 对齐 Tau: 执行对话历史自愈与清洗，剔除无效断头，保证送入模型的转录本闭合
        self.messages = clean_provider_context(restored)
        # 分支切换/恢复/Session 替换：只按当前路径重新同步摘要缓存。
        self._sync_context_cache()

        # 准备 system_prompt
        system_prompt = self.system_prompt or ""
        system_msgs = [m for m in self.messages if m.role == "system"]
        if system_msgs:
            system_prompt = system_msgs[0].text_content

        # ── Hook 2: AgentStartHook 拦截启动或动态重写 system_prompt
        start_decision = await self.hooks.emit(AgentStartHook(system_prompt=system_prompt))
        if isinstance(start_decision, HookResult):
            if start_decision.block:
                reason = f": {start_decision.reason}" if start_decision.reason else ""
                end_ev = AgentEnd(
                    messages=list(self.messages),
                    final_text=f"(blocked{reason})",
                    iterations=0,
                    stop_reason="blocked",
                )
                await self._notify(end_ev)
                yield end_ev
                return
            if start_decision.updated_system_prompt is not None:
                system_prompt = start_decision.updated_system_prompt
                if self.messages and self.messages[0].role == "system":
                    self.messages[0] = Message(role="system", content=system_prompt)
                elif system_prompt:
                    self.messages.insert(0, Message(role="system", content=system_prompt))

        # 每轮模型请求前的安全边界：投影运行态快照（不插 system、不断开工具配对）。
        async def _project_view(messages: list[Message], view: list[Message]) -> None:
            canonical_projection = projection_message(self.runtime_context, messages)
            projection = canonical_projection or projection_message(self.runtime_context, view)
            if projection is None:
                # An explicit clear can itself have been summarized away.
                if self.runtime_context.is_empty:
                    for old in reversed(messages):
                        if (old.metadata or {}).get("kind") == "runtime_context_clear":
                            if not any((m.metadata or {}).get("kind") == "runtime_context_clear" for m in view):
                                view.append(old)
                            break
                return
            # A summary can hide an already-persisted fact snapshot. Restore it to
            # the outgoing view without duplicating canonical history.
            if canonical_projection is None:
                view.append(projection)
                return
            messages.append(projection)
            if view is not messages:
                view.append(projection)
            await self.session.add_message_async(projection.role, projection.content, **(projection.metadata or {}))

        # 委托核心 ReAct 纯函数微内核驱动事件流
        loop_gen = run_agent_loop(
            llm=self.llm,
            messages=self.messages,
            tools=self.registry,
            context_manager=self._ctx,
            model=self.model,
            system=system_prompt,
            prompts=[prompt],
            project_view=_project_view,
            artifact_store=self.artifact_store,
            signal=self._current_signal,
            get_steering_messages=self._get_steering_messages,
            get_follow_up_messages=self._get_follow_up_messages,
            before_model_call=self.hooks.emit,
            before_tool_call=self.hooks.emit,
            after_tool_call=self.hooks.emit,
            retry_policy=self.retry_policy,
            run_budget=state.budget,
        )

        async with contextlib.aclosing(loop_gen):
            async for event in loop_gen:
                # 同步 Session 状态与压缩写回
                if isinstance(event, MessageEnd):
                    msg = event.message
                    is_cancelled_partial_text = (
                        msg.role == "assistant"
                        and not (msg.metadata and msg.metadata.get("tool_calls"))
                        and (bool(msg.metadata and msg.metadata.get("stop_reason") == "cancelled") or self._aborted)
                    )
                    if msg.role != "system" and not is_cancelled_partial_text:
                        # 树变更留在事件循环线程；JSONL 落盘（fsync/重写）交给工作线程，避免阻塞 TUI
                        await self.session.add_message_async(msg.role, msg.content, **(msg.metadata or {}))
                elif isinstance(event, ContextCompacted):
                    self._persist_compaction()

                if isinstance(event, AgentEnd):
                    event = replace(event, run_id=state.run_id)

                # 分发到订阅者
                await self._notify(event)

                yield event

    async def run(self, user_input: MessageContent) -> str | None:
        """追加 user 消息 → 内部消费 prompt_stream 事件流 → 返回最终文本。"""
        final_text = None
        async with contextlib.aclosing(self.prompt_stream(user_input)) as events:
            async for event in events:
                if isinstance(event, AgentEnd):
                    if event.stop_reason == "cancelled":
                        return "(cancelled)"
                    if event.stop_reason == "budget_exhausted":
                        return "(budget exhausted)"
                    if event.stop_reason == "blocked":
                        return event.final_text or "(blocked)"
                    if event.stop_reason == "error":
                        raise RuntimeError(event.final_text or "Error during model stream")
                    final_text = event.final_text

        return final_text

    async def wait_memory_idle(self) -> None:
        """Explicitly drain optional memory work (tests/hosts); responses do not await it."""
        await self._memory_distiller.wait()

    async def aclose(self) -> None:
        """Cancel and reap this Agent's in-process memory tasks."""
        try:
            state = self._active_run
            if state is not None:
                state.abort()
                if state.step is None:
                    # Close a stream whose consumer is paused at a yielded event.
                    if state.stream is not None:
                        await state.stream.aclose()
                    await self._close_interrupted_tool_group(state.partial)
                    state.partial = None
                    self._queue_background_cleanup()
                    self._current_signal = None
                    self._active_run = None
                    state.finished.set()
                elif state.owner is not asyncio.current_task():
                    await state.finished.wait()
            await self._drain_cleanup()
            await self._memory_distiller.close()
        finally:
            if self.memory_index is not None and self.memory_index_owned:
                try:
                    self.memory_index_close_status = await self.memory_index.aclose(timeout=self.memory_index_close_timeout)
                except Exception:
                    self.memory_index_close_status = False

    async def invoke_skill(self, name: str, instructions: str = "") -> str | None:
        """显式调用：skill_manager.format_invocation 包装（未知名 ValueError）→
        self.run(包装文本) 跑一轮。"""
        return await self.run(self.skill_manager.format_invocation(name, instructions))

    def reset(self) -> None:
        """清空对话（保留 system）。session 树清空 + 重载 memory 快照并重拼 system，清空干预队列。"""
        self._memory_distiller.reset()
        self.message_queue.clear()
        self.session.reset()
        if self.memory_store:
            self.memory_store.load_from_disk()
        self.messages = self._init_messages(self.session, self._system_prompt)
        self._ctx.reset()

    async def compact(self, instructions: str | None = None) -> None:
        """手动触发压缩：无条件执行一次 compact 结构化摘要（写缓存 + 事件），不动 messages。"""
        await self._ctx.compact(self.messages, instructions=instructions)
        await self._handle_compaction()

    # ── 内部实现 ─────────────────────────────────────────────

    async def _notify(self, event: Event) -> None:
        """将生命周期事件安全广播给所有旁路订阅者（对标 Tau AgentHarness._notify）。"""
        if isinstance(event, AgentEnd) and event.run_id is None and self._active_run is not None:
            event = replace(event, run_id=self._active_run.run_id)
        for sub in list(self._subscribers):
            with contextlib.suppress(Exception):
                res = sub(event)
                if inspect.isawaitable(res):
                    await res

    def _persist_compaction(self) -> None:
        """写回压缩缓存，更新本分支缓存身份，并标记摘要视图替换原因。"""
        self._ctx_bridge.write_compaction(self._ctx)
        latest = self.session.get_latest_compaction_cache()
        new_id = (latest or {}).get("entry_id")
        if new_id != getattr(self, "_ctx_cache_entry_id", None):
            self._ctx_cache_entry_id = new_id
            self._mark_summary_replaced("summary_replaced")

    async def _handle_compaction(self) -> None:
        """prepare/force_compact 触发压缩后：写回 session（桥）+ 事件。"""
        self._persist_compaction()
        info = self._ctx.pending_compaction
        if info is not None:
            ev = ContextCompacted(
                tokens_before=info.tokens_before,
                tokens_after=info.tokens_after,
                summarized_count=info.summarized_count,
            )
            await self._notify(ev)
