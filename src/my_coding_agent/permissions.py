"""业务权限门禁（PermissionGate）：基于 ToolCallHook 实现的无侵入安全审批门禁。

协议规范见 ``docs/dev/permission-approval.md``，核心设计：

- **fail-closed**：非 autonomous 模式下，没有 ``confirm_callback`` 一律返回 block；
  需要放行的嵌入方必须显式传 ``allow_without_callback=True``。
- ``mode="yolo"`` 仍映射为 autonomous，永不询问。
- ``review`` 模式直接放行只读工具与安全 shell 前缀；``strict`` 模式只读工具也要询问。
- 会话级白名单只存内存（``_session_approved``），进程结束即失效。
- 风险分级 ``assess_risk`` 由服务端计算，客户端只负责展示。
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from my_agent_core.hooks import HookResult, ToolCallHook

logger = logging.getLogger(__name__)

PermissionMode = Literal["review", "autonomous", "yolo", "strict"]
RiskLevel = Literal["low", "medium", "high"]
ApprovalScope = Literal["once", "session"]

READONLY_TOOLS = frozenset({"read", "grep", "find", "ls"})
MUTATING_TOOLS = frozenset({"write", "edit"})

SAFE_BASH_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "pytest",
    "python -m pytest",
    "uv run pytest",
)

DEFAULT_PERMISSION_TIMEOUT_MS = 300_000
PREVIEW_MAX_CHARS = 4096

#: 设备汇（写入它们不等于写入真实文件，例如 ``2>/dev/null``）。
_DEVICE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"})

#: 危险 bash 模式 → 人类可读原因（规范第 6 节 high 档）。
_DANGEROUS_BASH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", re.IGNORECASE), "递归/强制删除（rm -rf）"),
    (re.compile(r"\bsudo\b", re.IGNORECASE), "提权执行（sudo）"),
    (
        re.compile(r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", re.IGNORECASE),
        "下载内容直通 shell",
    ),
    (re.compile(r"\bchmod\s+(?:-[a-zA-Z]+\s+)*777\b", re.IGNORECASE), "开放全部权限（chmod 777）"),
    (re.compile(r">\s*/dev/(?:sd|hd|nvme|disk|xvd)", re.IGNORECASE), "写入块设备"),
    (re.compile(r"\bgit\s+push\b[^\n;|&]*(?:--force(?:-with-lease)?|-f)\b", re.IGNORECASE), "强制推送"),
    (re.compile(r"\btaskkill\b", re.IGNORECASE), "强制结束进程（taskkill）"),
)

#: 命令中的绝对路径 / 家目录路径（排除 URL 的 ``://`` 与相对路径）。
_PATH_CANDIDATE_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/][^\s"'`;|&<>]*|~[\\/][^\s"'`;|&<>]*|(?<![\w:/.~-])/[^\s"'`;|&<>]*)"""
)

#: 命令分词（用于识别 ``../`` 形式的相对越界路径）。
_SHELL_TOKEN_SPLIT_RE = re.compile(r"""[\s"'`;|&<>()]+""")

#: 命中任意一个即视为「不能直接放行的复合命令」。
_SHELL_META_TOKENS = (";", "&&", "||", "|", "&", "\n", "$(", "`", ">", "<")

_PATH_ARG_KEYS = ("path", "file_path", "filePath", "dir", "directory", "cwd")


@dataclass(frozen=True)
class PermissionDecision:
    """确认回调可返回的富决策：除批准与否外还能携带会话级 scope 与拒绝原因。"""

    approved: bool
    scope: ApprovalScope = "once"
    reason: str | None = None


@dataclass
class PermissionRequest:
    """权限审批请求对象（对应协议里的 ``permission_request.params``）。

    ``deny_reason`` 是内核侧回填字段：确认回调在拒绝时可以留下更精确的原因
    （例如「审批超时，已自动拒绝」），门禁会把它写进 ``HookResult.reason``。
    """

    action: str
    target: str
    details: dict[str, Any] = field(default_factory=dict)
    preview: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    risk: RiskLevel = "medium"
    reason: str | None = None
    deny_reason: str | None = None

    def __post_init__(self) -> None:
        if self.tool_name is None:
            self.tool_name = self.action


# ── 路径与风险分级 ────────────────────────────────────────────────────────────


def _as_path(value: Any, workspace: Path | None) -> Path | None:
    """把工具参数里的路径转成绝对 Path；无法解析时返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        return None
    if not path.is_absolute():
        path = (workspace or Path.cwd()) / path
    return path


def _is_within_workspace(path: Path, workspace: Path) -> bool:
    """判断 path 是否位于 workspace 内（含 workspace 本身）。"""
    try:
        path.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def _path_escapes_workspace(path: Path, workspace: Path) -> bool:
    return not _is_within_workspace(path, workspace)


def _first_path_arg(args: dict[str, Any]) -> str | None:
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if value:
            return str(value)
    return None


def _bash_outside_paths(command: str, workspace: Path) -> str | None:
    """返回首个逃出 workspace 的绝对/相对路径；没有则返回 None。"""
    for match in _PATH_CANDIDATE_RE.finditer(command):
        raw = match.group(0)
        normalized = raw.replace("/", "\\") if os.name == "nt" else raw
        if raw.lower() in _DEVICE_SINKS or normalized.lower() in _DEVICE_SINKS:
            continue
        path = _as_path(raw, workspace)
        if path is None or not path.is_absolute():
            continue
        if _path_escapes_workspace(path, workspace):
            return raw

    # 相对路径越界：``cat ../secret`` / ``cat foo/../../bar``
    for token in _SHELL_TOKEN_SPLIT_RE.split(command):
        candidate = token.split("=", 1)[1] if "=" in token else token
        if ".." not in candidate:
            continue
        path = _as_path(candidate, workspace)
        if path is not None and _path_escapes_workspace(path, workspace):
            return candidate
    return None


def _bash_danger_reason(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_BASH_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def _assess(tool_name: str, args: dict[str, Any], workspace: Path | None) -> tuple[RiskLevel, str | None]:
    """计算风险等级与人类可读原因（未提供 workspace 时以 CWD 为默认边界）。"""
    ws = workspace if workspace is not None else Path.cwd().resolve()

    if tool_name == "bash":
        command = str(args.get("command") or "")
        danger = _bash_danger_reason(command)
        if danger is not None:
            return "high", f"高危命令：{danger}"
        outside = _bash_outside_paths(command, ws)
        if outside is not None:
            return "high", f"命令访问工作区外路径：{outside}"
        return "medium", "执行 shell 命令"

    path_arg = _first_path_arg(args)
    if path_arg is not None:
        path = _as_path(path_arg, ws)
        if path is not None and _path_escapes_workspace(path, ws):
            return "high", f"目标路径在工作区之外：{path_arg}"

    if tool_name in MUTATING_TOOLS:
        return "medium", "将修改工作区文件"
    if tool_name in READONLY_TOOLS:
        return "low", None
    return "medium", None


def assess_risk(
    tool_name: str,
    args: dict[str, Any] | None,
    workspace: Path | str | None = None,
) -> RiskLevel:
    """服务端风险分级（规范第 6 节）：``low`` | ``medium`` | ``high``。

    - high：bash 命中危险模式，或命令/路径参数指向 workspace 之外；
    - medium：其余 bash 以及 write / edit；
    - low：只读工具（仅 strict 模式会弹窗）。

    未显式传入 ``workspace`` 时以当前工作目录作为默认边界。
    """
    ws = Path(workspace).resolve() if workspace is not None else None
    risk, _ = _assess(tool_name, args or {}, ws)
    return risk


def _safe_bash_prefix(command: str) -> bool:
    """review 模式下可免审的安全 shell 前缀（拒绝链式/管道/重定向绕过）。"""
    if any(token in command for token in _SHELL_META_TOKENS):
        return False
    return any(command.startswith(prefix) for prefix in SAFE_BASH_PREFIXES)


def _primary_target(tool_name: str, args: dict[str, Any]) -> str:
    for key in ("path", "file_path", "command", "pattern", "url", "name"):
        value = args.get(key)
        if value:
            return str(value)
    return tool_name


def _build_preview(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name not in MUTATING_TOOLS:
        return None
    raw = args.get("content")
    if raw is None:
        raw = args.get("edits")
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    if len(text) <= PREVIEW_MAX_CHARS:
        return text
    return text[: PREVIEW_MAX_CHARS - 1] + "…"


# ── 权限门禁 ─────────────────────────────────────────────────────────────────


class PermissionGate:
    """基于 ToolCallHook 实现的无侵入安全审批门禁（fail-closed）。"""

    def __init__(
        self,
        mode: PermissionMode = "review",
        confirm_callback: Callable[
            [PermissionRequest],
            Awaitable[bool | PermissionDecision | dict[str, Any]] | bool | PermissionDecision | dict[str, Any],
        ]
        | None = None,
        allow_without_callback: bool = False,
        workspace: Path | str | None = None,
        session_approved: set[str] | None = None,
    ) -> None:
        self.mode: PermissionMode = "autonomous" if mode == "yolo" else mode
        self.confirm_callback = confirm_callback
        self.allow_without_callback = allow_without_callback
        self.workspace: Path | None = Path(workspace).resolve() if workspace is not None else None
        self._session_approved: set[str] = set(session_approved or ())

    # ── 会话级白名单（scope=session） ──
    def approve_for_session(self, tool_name: str) -> None:
        """把某个工具加入本会话白名单：后续同名工具不再询问。"""
        if tool_name:
            self._session_approved.add(tool_name)

    def is_session_approved(self, tool_name: str) -> bool:
        return tool_name in self._session_approved

    def reset_session(self) -> None:
        """清空会话级白名单（例如切换会话/模式时）。"""
        self._session_approved.clear()

    async def __call__(self, hook: ToolCallHook) -> HookResult:
        if self.mode == "autonomous":
            return HookResult()

        tool_name = hook.tool_name
        args = hook.args if isinstance(hook.args, dict) else {}

        # 1. 会话级白名单直接放行（含 strict 模式的只读工具）
        if tool_name in self._session_approved:
            return HookResult()

        # 2. 只读工具放行（strict 模式除外）
        if self.mode != "strict" and tool_name in READONLY_TOOLS:
            return HookResult()

        # 3. 安全 Shell 前缀放行（仅 review 模式）
        if tool_name == "bash" and self.mode == "review":
            command = str(args.get("command", "")).strip()
            if _safe_bash_prefix(command):
                return HookResult()

        # 4. fail-closed：没有审批通道时必须拒绝
        if self.confirm_callback is None:
            if self.allow_without_callback:
                return HookResult()
            return HookResult(
                block=True,
                reason=f"审批回调缺失（fail-closed），已自动拒绝工具 [{tool_name}] 的执行请求。",
            )

        risk, risk_reason = _assess(tool_name, args, self.workspace)
        request = PermissionRequest(
            action=tool_name,
            target=_primary_target(tool_name, args),
            details=dict(args),
            preview=_build_preview(tool_name, args),
            tool_name=tool_name,
            tool_call_id=getattr(hook, "tool_call_id", None),
            risk=risk,
            reason=risk_reason,
        )

        try:
            result = self.confirm_callback(request)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # 审批异常绝不能 fail-open（HookRegistry 会吞掉异常继续执行）
            logger.exception("权限审批回调执行失败，已按 fail-closed 拒绝工具 %s", tool_name)
            return HookResult(
                block=True,
                reason=f"审批流程异常，已自动拒绝工具 [{tool_name}] 的执行请求：{exc}",
            )

        if isinstance(result, PermissionDecision):
            # 只有显式 True 才批准：任何真值兜底都可能把编程错误变成放行
            approved = result.approved is True
            scope: ApprovalScope = result.scope if result.scope in ("once", "session") else "once"
            if not approved:
                request.deny_reason = result.reason or "用户拒绝了该工具的审批请求。"
        elif isinstance(result, dict):
            # 兼容直接返回协议响应体的嵌入方：{approved, scope, reason}
            approved = result.get("approved") is True
            raw_scope = result.get("scope")
            scope = raw_scope if raw_scope in ("once", "session") else "once"
            if not approved:
                reason = result.get("reason")
                request.deny_reason = (
                    reason.strip() if isinstance(reason, str) and reason.strip() else "用户拒绝了该工具的审批请求。"
                )
        else:
            approved = result is True
            scope = "once"

        if approved:
            if scope == "session":
                self.approve_for_session(tool_name)
            return HookResult()

        return HookResult(
            block=True,
            reason=request.deny_reason or f"用户拒绝了工具 [{tool_name}] 的执行请求。",
        )
