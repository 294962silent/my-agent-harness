"""AgentPaths: 统一管理全局用户目录与项目本地资源的路径调度中心。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

__all__ = ["AgentPaths", "resolve_home"]


# 新目录名（推荐）；旧目录名（兼容既有登录态）
HOME_DIR_NAME = ".longarc"
LEGACY_HOME_DIR_NAMES = (".my-agent-harness", ".my-pi-agent")


def resolve_home() -> Path:
    """解析全局用户目录。

    优先级：
      1. 环境变量 LONGARC_HOME（显式指定，最高优先级）
      2. 环境变量 MY_AGENT_HOME（兼容旧配置）
      3. ~/.longarc（新目录，若已存在）
      4. ~/.my-agent-harness 或 ~/.my-pi-agent（向后兼容）
      5. ~/.longarc（全新安装时的默认值）

    这样新用户拿到干净的目录名，老用户升级后仍能用原有登录态。
    """
    explicit = os.environ.get("LONGARC_HOME") or os.environ.get("MY_AGENT_HOME")
    if explicit:
        return Path(explicit).resolve()

    home_root = Path.home()
    new_dir = home_root / HOME_DIR_NAME

    if new_dir.exists():
        return new_dir.resolve()
    for name in LEGACY_HOME_DIR_NAMES:
        legacy_dir = home_root / name
        if legacy_dir.exists():
            return legacy_dir.resolve()
    return new_dir.resolve()


@dataclass(frozen=True, slots=True)
class AgentPaths:
    """集中解析与管理 LongArc 的全局与项目级路径。"""

    home: Path = field(default_factory=resolve_home)
    agents_home: Path = field(default_factory=lambda: (Path.home() / ".agents").resolve())

    # ── 全局资源路径 ──
    @property
    def auth_path(self) -> Path:
        return self.home / "auth.json"

    @property
    def auth_lock_path(self) -> Path:
        return self.home / "auth.json.lock"

    @property
    def settings_path(self) -> Path:
        return self.home / "settings.json"

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def prompts_dir(self) -> Path:
        return self.home / "prompts"

    @property
    def themes_dir(self) -> Path:
        return self.home / "themes"

    @property
    def extensions_dir(self) -> Path:
        return self.home / "extensions"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    # ── 项目局部路径 ──
    def project_agent_dir(self, cwd: Path) -> Path:
        """项目级配置目录。

        新目录 `.longarc` 优先；若项目中只存在旧目录，则沿用旧目录，
        避免既有项目配置失效。
        """
        new_dir = cwd / HOME_DIR_NAME
        if new_dir.is_dir():
            return new_dir
        for name in LEGACY_HOME_DIR_NAMES:
            legacy_dir = cwd / name
            if legacy_dir.is_dir():
                return legacy_dir
        return new_dir

    def project_settings_path(self, cwd: Path) -> Path:
        return self.project_agent_dir(cwd) / "settings.json"

    def project_skills_dir(self, cwd: Path) -> Path:
        return self.project_agent_dir(cwd) / "skills"

    def project_agents_skills_dir(self, cwd: Path) -> Path:
        return cwd / ".agents" / "skills"

    # ── 会话分区映射算法 (Tau Slug + Hash 优化版) ──
    def project_session_dir(self, cwd: Path) -> Path:
        """根据项目 cwd 计算全局唯一的 sessions/<slug>-<hash> 目录。"""
        resolved = cwd.resolve()
        digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
        slug = self._slugify_path(resolved)
        target = self.sessions_dir / f"{slug}-{digest}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def default_session_path(self, cwd: Path) -> Path:
        return self.project_session_dir(cwd) / "default.jsonl"

    def project_logs_dir(self, cwd: Path) -> Path:
        """根据项目 cwd 计算全局唯一的 logs/<slug>-<hash> 目录。"""
        resolved = cwd.resolve()
        digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
        slug = self._slugify_path(resolved)
        target = self.logs_dir / f"{slug}-{digest}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def session_log_path(self, cwd: Path, session_id: str) -> Path:
        """获取指定会话的可读调试日志文件路径 (<session-id>.debug.log)。"""
        return self.project_logs_dir(cwd) / f"{session_id}.debug.log"

    def session_events_path(self, cwd: Path, session_id: str) -> Path:
        """获取指定会话的标准机器可读事件流文件路径 (<session-id>.events.jsonl)。"""
        return self.project_logs_dir(cwd) / f"{session_id}.events.jsonl"

    def ensure_directories(self) -> None:
        """初次启动自动建巢，静默初始化目录骨架。"""
        for d in (
            self.home,
            self.sessions_dir,
            self.skills_dir,
            self.prompts_dir,
            self.themes_dir,
            self.extensions_dir,
            self.logs_dir,
            self.memory_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slugify_path(path: Path, max_length: int = 48) -> str:
        parts = [p for p in path.parts if p not in (path.anchor, "")]
        try:
            rel = path.relative_to(Path.home())
            parts = ["home", *rel.parts]
        except ValueError:
            pass
        normalized = [clean for p in parts if (clean := re.sub(r"[^a-zA-Z0-9._-]+", "-", p).strip(".-_").lower())]
        slug = "-".join(normalized)
        if len(slug) <= max_length:
            return slug or "project"

        suffix_parts: list[str] = []
        cur_len = 0
        for p in reversed(normalized):
            if cur_len + len(p) + 1 > max_length:
                break
            suffix_parts.append(p)
            cur_len += len(p) + 1
        return "-".join(reversed(suffix_parts)) or slug[-max_length:].strip("-") or "project"
