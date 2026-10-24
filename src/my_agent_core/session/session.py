# pyright: reportUnusedCallResult=false, reportAttributeAccessIssue=false
"""Session 管理：树结构会话 + JSONL 文件持久化（纯正 Tau 只追加多态体系，配 my-agent-llm Message）。

一个会话 = 一棵树（entry 带 id/parent_id）+ current_id 指针。rewind = 移动指针、
旧分支保留。文件格式为纯粹的 SessionEntry 流：第 1 行是 SessionInfoEntry，后续每行为 MessageEntry/CompactionEntry 等。

落盘策略（format_version=2）：
- 仅新增 entry（消息链式追加）时只向文件末尾追加新行，不重写历史；
- rewind/fork/compaction/reset/元数据变更等结构性变更仍走原子全量重写；
- 全量重写时若 current 指针不在写入序列末尾，追加一条 CustomEntry 指针记录
  （namespace=my_agent_core.session），读取时「最后出现的指针记录为准」。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from my_agent_llm import Message, MessageContent

from .entries import (
    CompactionEntry,
    CustomEntry,
    MessageEntry,
    SessionEntry,
    SessionHeaderEntry,
    SessionInfoEntry,
)
from .jsonl import entry_from_json_line, entry_to_json_line
from .storage import _lock_file, _unlock_file
from .tree import path_to_entry

# 文件格式版本：2 = 支持增量追加 + 指针记录（legacy 文件无此标记，仍按旧语义读取）
_FORMAT_VERSION = 2
_FORMAT_VERSION_KEY = "format_version"
_POINTER_NAMESPACE = "my_agent_core.session"
_POINTER_CUSTOM_TYPE = "pointer"
_RESERVED_META_KEYS = ("current_id", "root_id", "compaction_floor", _FORMAT_VERSION_KEY)


class SessionTree:
    """树：entries（id→entry）+ current_id 指针 + root_id。"""

    def __init__(self) -> None:
        self.entries: dict[str, SessionEntry] = {}
        self.current_id: str | None = None
        self.root_id: str | None = None

    def add_entry(
        self, role: str, content: MessageContent, parent_id: str | None = None, **metadata: Any
    ) -> MessageEntry:
        """追加到 current 下（或指定 parent）。首个 entry 成为根。"""
        if parent_id is None:
            parent_id = self.current_id
        entry = MessageEntry(
            id=uuid4().hex[:8],
            parent_id=parent_id,
            message=Message(
                role=cast(Any, role),
                content=content,
                metadata=metadata or None,
            ),
        )
        self.entries[entry.id] = entry
        self.current_id = entry.id
        if self.root_id is None:
            self.root_id = entry.id
        return entry

    def get_current_path(self) -> list[SessionEntry]:
        """根 → current 的路径（Agent 上下文用）。空树返回 []。"""
        if self.current_id is None:
            return []
        return self.get_path_to_entry(self.current_id)

    def get_path_to_entry(self, entry_id: str) -> list[SessionEntry]:
        """根 → entry_id 的路径（fork 用）。不存在抛 ValueError。"""
        if entry_id not in self.entries:
            raise ValueError(f"Entry {entry_id} not found")
        try:
            return path_to_entry(self.entries, entry_id)
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    def rewind(self, entry_id: str) -> None:
        """移动 current 指针到已有节点；新 entry 将在其下追加（长新枝）。不存在抛 ValueError。"""
        if entry_id not in self.entries:
            raise ValueError(f"Entry {entry_id} not found")
        self.current_id = entry_id

    @classmethod
    def from_jsonl_iter(cls, lines: Iterable[str]) -> SessionTree:
        """从迭代器恢复树。中途某行损坏抛 ValueError（带行号），尾行由 load 处理。"""
        tree = cls()
        for idx, line in enumerate(lines, start=2):  # start=2 因为 line 1 是 session_info
            line = line.strip()
            if not line:
                continue
            try:
                entry = entry_from_json_line(line)
            except Exception as exc:
                raise ValueError(f"Corrupted entry at line {idx}: {exc}") from exc
            tree.entries[entry.id] = entry
            tree.current_id = entry.id
            if tree.root_id is None:
                tree.root_id = entry.id
        return tree


class Session:
    """一个会话 = 树 + 路径 + JSONL 文件（纯正 Tau 多态 SessionEntry 流）。

    文件第 1 行是 SessionInfoEntry，后续每行是一个多态 SessionEntry。
    """

    def __init__(self, *, path: Path, cwd: str | None = None, metadata: dict | None = None):
        """新建会话（纯对话，不含 system）。不立即写文件。"""
        self.path = Path(path)
        self.id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        self.created_at = datetime.now().isoformat()
        self.cwd = str(cwd) if cwd else str(Path.cwd())
        self.tree = SessionTree()
        self.compaction_floor: str | None = None
        self.metadata = metadata or {}  # 额外元数据（子代理：agent_type/parent_session_id），进 SessionInfoEntry
        self._storage: Any | None = None
        # 落盘基线：上次成功落盘后的 entry 序列/id/尺寸/元数据；用于判定能否只追加
        self._save_lock = threading.Lock()
        self._persisted_ids: tuple[str, ...] | None = None
        self._persisted_root_id: str | None = None
        self._persisted_floor: str | None = None
        self._persisted_metadata: dict[str, Any] | None = None
        self._persisted_header: tuple[str, str, str] | None = None
        self._persisted_size = -1
        self._append_enabled = False  # 仅当文件已带 format_version=2 标记时才允许增量追加

    @classmethod
    def load(cls, path: Path) -> Session:
        """从 JSONL 文件恢复整棵树。首行非 SessionInfoEntry/SessionHeaderEntry → ValueError；
        非尾行损坏 → ValueError（带行号）；尾行撕裂 → 丢弃该行（宽容兜底）。

        format_version=2 的文件支持增量追加与「指针记录」：最后出现的指针记录决定
        current_id/root_id/compaction_floor；旧格式（无标记）仍按首行 metadata 恢复，
        保证历史会话可读。
        """
        path = Path(path).resolve()
        try:
            with open(path, encoding="utf-8") as f:
                lines = list(f)
        except OSError as exc:
            raise ValueError(f"Failed to read session file {path}: {exc}") from exc
        if not lines:
            raise ValueError(f"Session file {path} is empty")

        try:
            first_entry = entry_from_json_line(lines[0])
        except Exception as exc:
            raise ValueError(f"Session file {path}: invalid header/info: {exc}") from exc

        if not isinstance(first_entry, (SessionInfoEntry, SessionHeaderEntry)):
            raise ValueError(f"Session file {path}: line 1 must be a valid SessionHeaderEntry or SessionInfoEntry")

        header_meta = dict(getattr(first_entry, "metadata", {}) or {})
        is_v2 = header_meta.get(_FORMAT_VERSION_KEY) == _FORMAT_VERSION

        tree_lines = lines[1:]
        if tree_lines and tree_lines[-1].strip():
            try:
                entry_from_json_line(tree_lines[-1])
            except Exception:
                tree_lines = tree_lines[:-1]

        if is_v2:
            tree, meta = _restore_v2_tree(tree_lines, header_meta)
        else:
            tree = SessionTree.from_jsonl_iter(tree_lines)
            meta = dict(header_meta)

        if getattr(first_entry, "name", None):
            meta["name"] = first_entry.name
        if getattr(first_entry, "title", None):
            meta["title"] = first_entry.title
        if getattr(first_entry, "parent_session", None):
            meta["parent_session"] = first_entry.parent_session
            meta["parent_session_path"] = first_entry.parent_session
        for e in tree.entries.values():
            if isinstance(e, SessionInfoEntry):
                sub_name = getattr(e, "name", None) or getattr(e, "title", None)
                if sub_name:
                    meta["name"] = sub_name
                    meta["title"] = sub_name

        # 旧格式：首行 metadata 的 current_id/root_id 是权威指针（兼容存量文件）
        if not is_v2:
            cur = meta.get("current_id")
            if isinstance(cur, str) and cur in tree.entries:
                tree.current_id = cur
            root = meta.get("root_id")
            if isinstance(root, str) and root in tree.entries:
                tree.root_id = root

        session = cls(path=path, cwd=first_entry.cwd)
        session.id = first_entry.id
        session.created_at = (
            datetime.fromtimestamp(first_entry.created_at).isoformat()
            if first_entry.created_at
            else datetime.now().isoformat()
        )
        session.tree = tree
        session.compaction_floor = meta.get("compaction_floor")
        session.metadata = {k: v for k, v in meta.items() if k not in _RESERVED_META_KEYS}
        session._sync_persisted_state(append_enabled=is_v2)
        return session

    def add_message(
        self, role: str, content: MessageContent, parent_id: str | None = None, **metadata: Any
    ) -> MessageEntry:
        """加到树 + save()。"""
        entry = self.tree.add_entry(role, content, parent_id, **metadata)
        self.save()
        return entry

    async def add_message_async(
        self, role: str, content: MessageContent, parent_id: str | None = None, **metadata: Any
    ) -> MessageEntry:
        """异步版 add_message：树变更留在事件循环线程，落盘（含 fsync）交给工作线程。

        Agent 每收到一个 MessageEnd 都会调用；避免长会话全量重写/fsync 阻塞承载 TUI 的事件循环。
        """
        entry = self.tree.add_entry(role, content, parent_id, **metadata)
        await asyncio.to_thread(self.save)
        return entry

    def _hydrate_message_images(self, message: Message) -> Message:
        """从磁盘资产目录透明水合图片的 Base64 数据并还原为规范格式。"""
        if not isinstance(message.content, list):
            return message
        has_hydrated = False
        hydrated_blocks = []
        for part in message.content:
            if isinstance(part, dict) and part.get("type") == "image":
                raw_data = part.get("data")
                asset_path = part.get("asset_path")
                if not raw_data and asset_path:
                    try:
                        from .assets import load_image_asset

                        loaded_b64 = load_image_asset(self.path.parent, asset_path)
                        part = {
                            "type": "image",
                            "data": loaded_b64,
                            "mime_type": part.get("mime_type", "image/png"),
                        }
                        has_hydrated = True
                    except Exception:
                        pass
                elif raw_data and "asset_path" in part:
                    part = {
                        "type": "image",
                        "data": raw_data,
                        "mime_type": part.get("mime_type", "image/png"),
                    }
                    has_hydrated = True
            hydrated_blocks.append(part)
        if has_hydrated:
            return message.model_copy(update={"content": hydrated_blocks})
        return message

    def get_current_path_messages(self, hydrate_images: bool = True) -> list[Message]:
        """当前路径上的纯对话消息列表（自动排除非 MessageEntry 节点）。

        hydrate_images=True 时自动从会话资产目录水合图片 Base64 数据，确保送给 LLM Provider 时具备完整视觉内容。
        """
        raw_msgs = [e.message for e in self.tree.get_current_path() if isinstance(e, MessageEntry)]
        if not hydrate_images:
            return raw_msgs
        return [self._hydrate_message_images(m) for m in raw_msgs]

    def add_summary_cache(
        self,
        summary: str,
        *,
        covered_count: int,
        retained_tail: list[dict],
        tokens_before: int,
        summary_usage: dict | None = None,
        summary_model: str | None = None,
    ) -> None:
        """写一条 CompactionEntry（不动 current_id）+ 更新 compaction_floor。"""
        metadata: dict[str, Any] = {
            "retained_tail": retained_tail,
            "covered_count": covered_count,
            "tokens_before": tokens_before,
        }
        if summary_usage is not None:
            metadata["summary_usage"] = summary_usage
        if summary_model is not None:
            metadata["summary_model"] = summary_model
        entry = CompactionEntry(
            parent_id=self.tree.current_id,
            summary=summary,
            replaces_entry_ids=[],
            metadata=metadata,
        )
        self.tree.entries[entry.id] = entry
        self.compaction_floor = self.tree.current_id
        self.save()

    def get_full_history_messages(self) -> list[Message]:
        """完整对话历史（等同于 get_current_path_messages，排除非 MessageEntry 节点）。"""
        return self.get_current_path_messages()

    def get_latest_compaction_cache(self) -> dict | None:
        """最新一条 CompactionEntry → {summary, covered_count, retained_tail}；无则 None。"""
        cache_entries = [
            e
            for e in self.tree.entries.values()
            if isinstance(e, CompactionEntry) or getattr(e, "type", None) == "compaction"
        ]
        if not cache_entries:
            return None
        latest = max(cache_entries, key=lambda e: len(self.tree.get_path_to_entry(e.id)))
        md = getattr(latest, "metadata", {}) or {}
        try:
            covered_count = int(md.get("covered_count", 0))
        except (ValueError, TypeError):
            covered_count = 0
        summary_text = latest.summary if isinstance(latest, CompactionEntry) else getattr(latest, "summary", "")
        return {
            "summary": summary_text,
            "covered_count": covered_count,
            "retained_tail": list(md.get("retained_tail", [])),
        }

    def rewind(self, entry_id: str) -> None:
        """移动 current 指针（旧分支保留）+ save。压缩后只能回 floor（含）之后，否则 ValueError。"""
        if self.compaction_floor is not None and not self._after_floor(entry_id):
            raise ValueError(
                f"Cannot rewind past compaction floor {self.compaction_floor}: "
                f"entry {entry_id} is prior to compacted history"
            )
        self.tree.rewind(entry_id)
        self.save()

    def _after_floor(self, entry_id: str) -> bool:
        """entry_id 是否在 compaction_floor（含）之后长出的节点：等于 floor，或沿 parent 链能走到 floor。"""
        if entry_id == self.compaction_floor:
            return True
        cur = self.tree.entries.get(entry_id)
        while cur is not None and cur.parent_id is not None:
            if cur.parent_id == self.compaction_floor:
                return True
            cur = self.tree.entries.get(cur.parent_id)
        return False

    # ── 持久化：增量追加 / 原子全量重写 ─────────────────────────

    @contextlib.contextmanager
    def _process_file_lock(self, timeout: float = 10.0) -> Iterator[None]:
        """跨进程排他锁：与 JsonlSessionStorage 共用 `.{name}.lock` 约定，保证追加串行。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / f".{self.path.name}.lock"
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        try:
            start = time.monotonic()
            while True:
                try:
                    _lock_file(fd)
                    break
                except (BlockingIOError, OSError, PermissionError):
                    if time.monotonic() - start > timeout:
                        raise TimeoutError(f"Timeout ({timeout}s) waiting for session lock {lock_path}") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                _unlock_file(fd)
        finally:
            os.close(fd)

    def _header_meta(self) -> dict[str, Any]:
        """首行 SessionInfoEntry 的 metadata：额外元数据 + 指针 + 格式版本。"""
        meta = dict(self.metadata)
        meta["current_id"] = self.tree.current_id
        meta["root_id"] = self.tree.root_id
        meta["compaction_floor"] = self.compaction_floor
        meta[_FORMAT_VERSION_KEY] = _FORMAT_VERSION
        return meta

    def _pointer_record_line(self) -> str | None:
        """current 指针不在写入序列末尾时，补一条指针记录（读取时最后一条生效）。

        常见于 rewind/branch 后的全量重写，以及压缩 entry（不动 current）后的追加。
        """
        if not self.tree.entries:
            return None
        last_id = next(reversed(self.tree.entries))
        if self.tree.current_id == last_id:
            return None
        record = CustomEntry(
            namespace=_POINTER_NAMESPACE,
            custom_type=_POINTER_CUSTOM_TYPE,
            data={
                "current_id": self.tree.current_id,
                "root_id": self.tree.root_id,
                "compaction_floor": self.compaction_floor,
            },
        )
        return entry_to_json_line(record)

    def _can_append_incrementally(self) -> bool:
        """是否只是「在文件尾部新增 entry」：是则只追加，不重写历史。"""
        if not self._append_enabled or self._persisted_ids is None:
            return False
        ids = tuple(self.tree.entries)
        persisted = self._persisted_ids
        if len(ids) <= len(persisted) or ids[: len(persisted)] != persisted:
            return False
        if (self.id, self.cwd, self.created_at) != self._persisted_header:
            return False
        if self.tree.root_id != self._persisted_root_id:
            return False
        if self.compaction_floor != self._persisted_floor:
            return False
        if self.metadata != self._persisted_metadata:
            return False
        try:
            # 文件被外部改写（尺寸变化）时放弃增量，退回全量重写，避免把陈旧前缀当事实
            return self.path.stat().st_size == self._persisted_size
        except OSError:
            return False

    def _append_incrementally(self) -> None:
        """只向 JSONL 尾部追加新增 entry（+ 必要时一条指针记录），带 fsync。"""
        persisted = self._persisted_ids or ()
        new_entries = list(self.tree.entries.values())[len(persisted) :]
        lines = [entry_to_json_line(e, assets_dir=self.path.parent) for e in new_entries]
        pointer_line = self._pointer_record_line()
        if pointer_line is not None:
            lines.append(pointer_line)
        payload = "".join(line + "\n" for line in lines)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise OSError(f"Failed to append session {self.path}: {exc}") from exc
        self._sync_persisted_state(append_enabled=True)

    def _rewrite_full(self) -> None:
        """原子全量重写：临时文件 + fsync + os.replace（写入首行会话头 + 所有多态 SessionEntry）。"""
        meta = self._header_meta()
        created_at_val: float | None = None
        try:
            if isinstance(self.created_at, str):
                created_at_val = datetime.fromisoformat(self.created_at).timestamp()
            elif isinstance(self.created_at, (int, float)):
                created_at_val = float(self.created_at)
        except Exception:
            created_at_val = None

        name_val = meta.get("name") or meta.get("title")
        info_entry = SessionInfoEntry(
            id=self.id,
            cwd=self.cwd,
            title=name_val,
            created_at=created_at_val,
            metadata=meta,
        )
        lines = [entry_to_json_line(info_entry)]
        lines.extend(entry_to_json_line(e, assets_dir=self.path.parent) for e in self.tree.entries.values())
        pointer_line = self._pointer_record_line()
        if pointer_line is not None:
            lines.append(pointer_line)
        payload = "".join(line + "\n" for line in lines)

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = Path(f.name)
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        self._sync_persisted_state(append_enabled=True)

    def _sync_persisted_state(self, *, append_enabled: bool) -> None:
        """记录磁盘上的事实，作为下一次「能否只追加」的基线。"""
        self._persisted_ids = tuple(self.tree.entries)
        self._persisted_root_id = self.tree.root_id
        self._persisted_floor = self.compaction_floor
        self._persisted_metadata = dict(self.metadata)
        self._persisted_header = (self.id, self.cwd, self.created_at)
        try:
            self._persisted_size = self.path.stat().st_size
        except OSError:
            self._persisted_size = -1
        self._append_enabled = append_enabled

    def save(self) -> None:
        """落盘：仅新增 entry 时尾部追加；结构性变更（rewind/compaction/reset/元数据）原子重写。

        追加与重写都在实例锁 + 跨进程文件锁内完成；fsync 阻塞由调用方挪到工作线程
        （Agent 走 add_message_async / asyncio.to_thread）。
        """
        with self._save_lock, self._process_file_lock():
            if self._can_append_incrementally():
                self._append_incrementally()
            else:
                self._rewrite_full()

    def append_entry(self, entry: SessionEntry) -> None:
        """追加一条任意多态 SessionEntry 并原子持久化。"""
        if isinstance(entry, SessionInfoEntry):
            name_val = getattr(entry, "name", None) or getattr(entry, "title", None)
            if name_val:
                self.metadata["name"] = name_val
                self.metadata["title"] = name_val
            if entry.cwd:
                self.cwd = entry.cwd
        self.tree.entries[entry.id] = entry
        if self.tree.root_id is None:
            self.tree.root_id = entry.id
        self.tree.current_id = entry.id
        self.save()

    def reset(self) -> None:
        """清空树 + 原子重写（纯对话，不含 system）。唯一破坏性操作。"""
        self.tree = SessionTree()
        self.compaction_floor = None
        self.save()

    def fork(self, entry_id: str, new_path: Path | None = None) -> Session:
        """从某 entry 分叉为新会话：复制根到 entry 的路径为新会话（新 id/路径，独立演化）。"""
        if entry_id not in self.tree.entries:
            raise ValueError(f"Entry {entry_id} not found")
        if new_path is None:
            sid = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
            new_path = self.path.parent / f"{sid}.jsonl"
        new_session = Session(path=new_path, cwd=self.cwd, metadata=dict(self.metadata))
        for entry in self.tree.get_path_to_entry(entry_id):
            if isinstance(entry, MessageEntry):
                new_session.add_message(entry.role, entry.content, **entry.metadata)
        return new_session

    @property
    def storage(self) -> Any:
        """底层只追加存储对象。"""
        if self._storage is None:
            from my_agent_core.session.storage import JsonlSessionStorage

            self._storage = JsonlSessionStorage(self.path)
        return self._storage

    def get_state(self) -> Any:
        """获取从根到当前 current_id 的 SessionState 状态快照。"""
        from my_agent_core.session.memory import SessionState

        return SessionState(
            messages=tuple(self.get_current_path_messages()),
            active_leaf_id=self.tree.current_id,
        )


def _is_pointer_record(entry: SessionEntry) -> bool:
    """是否为格式 v2 的指针记录（CustomEntry，不进入会话树）。"""
    return (
        isinstance(entry, CustomEntry)
        and entry.namespace == _POINTER_NAMESPACE
        and entry.custom_type == _POINTER_CUSTOM_TYPE
    )


def _restore_v2_tree(lines: Iterable[str], header_meta: dict[str, Any]) -> tuple[SessionTree, dict[str, Any]]:
    """恢复 format_version=2 的树。

    语义：指针记录按出现顺序覆盖 current/root/floor；普通 entry 依次入树并把
    current 推进到自身（追加进去的 entry 天然成为新的活动叶）。这样
    「全量重写 + 尾部指针记录 + 后续增量追加」都能还原出与内存一致的指针。
    """
    tree = SessionTree()
    meta = dict(header_meta)
    pointer = meta.get("current_id")
    root = meta.get("root_id")
    floor = meta.get("compaction_floor")
    last_entry_id: str | None = None
    for idx, line in enumerate(lines, start=2):  # start=2 因为 line 1 是会话头
        line = line.strip()
        if not line:
            continue
        try:
            entry = entry_from_json_line(line)
        except Exception as exc:
            raise ValueError(f"Corrupted entry at line {idx}: {exc}") from exc
        if _is_pointer_record(entry):
            data = entry.data if isinstance(entry, CustomEntry) else {}
            pointer = data.get("current_id")
            root = data.get("root_id", root)
            floor = data.get("compaction_floor", floor)
            continue
        tree.entries[entry.id] = entry
        last_entry_id = entry.id
        pointer = entry.id
        if root is None:
            root = entry.id
    if pointer is not None and pointer not in tree.entries:
        pointer = last_entry_id
    if root is not None and root not in tree.entries:
        root = next(iter(tree.entries), None)
    tree.current_id = pointer
    tree.root_id = root
    meta["current_id"] = pointer
    meta["root_id"] = root
    meta["compaction_floor"] = floor
    return tree, meta
