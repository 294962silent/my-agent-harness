"""JSONL format serialization, parsing, and error definitions for session entries.

对齐 Tau (tau_agent.session.jsonl) 架构设计：
本模块纯粹负责 SessionEntry 与单行 JSONL 文本之间的双向序列化与反序列化，
以及行格式异常定义。持久化驱动与文件锁已归位至 storage.py。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from .entries import MessageEntry, SessionEntry


class SessionJsonlError(ValueError):
    """JSONL 序列化、反序列化或格式损坏异常。"""


_ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)


def entry_to_json_line(entry: SessionEntry, assets_dir: Path | None = None) -> str:
    """将 SessionEntry 序列化为单行 JSON 字符串（不带尾随换行符）。

    若指定 assets_dir 且存在图片 Base64，自动执行资产溢出落盘并从 JSON 行中脱敏剔除。
    """
    try:
        if assets_dir is not None and isinstance(entry, MessageEntry) and isinstance(entry.message.content, list):
            from .assets import save_image_asset

            new_content = []
            has_image_to_spill = False
            for part in entry.message.content:
                if isinstance(part, dict) and part.get("type") == "image":
                    raw_data = part.get("data")
                    if raw_data:
                        has_image_to_spill = True
                        meta = save_image_asset(assets_dir, raw_data, str(part.get("mime_type", "image/png")))
                        spilled_block = {
                            "type": "image",
                            "asset_path": meta["asset_path"],
                            "sha256": meta["sha256"],
                            "mime_type": meta["mime_type"],
                            "width": meta["width"],
                            "height": meta["height"],
                            "size_bytes": meta["size_bytes"],
                        }
                        new_content.append(spilled_block)
                        continue
                new_content.append(part)

            if has_image_to_spill:
                entry = entry.model_copy(
                    update={"message": entry.message.model_copy(update={"content": new_content})}
                )
        return entry.model_dump_json(by_alias=True, exclude_none=True)
    except Exception as exc:
        raise SessionJsonlError(f"Failed to serialize entry to JSON line: {exc}") from exc


def entry_from_json_line(line: str) -> SessionEntry:
    """从单行 JSON 字符串解析 SessionEntry。"""
    stripped = line.strip()
    if not stripped:
        raise SessionJsonlError("JSONL line is empty or blank")

    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SessionJsonlError(f"Invalid JSON line: {exc}") from exc

    if not isinstance(raw, dict):
        raise SessionJsonlError(f"Expected JSON object, got {type(raw).__name__}")

    try:
        return _ENTRY_ADAPTER.validate_python(raw)
    except Exception as exc:
        raise SessionJsonlError(f"Cannot parse JSON object as SessionEntry: {exc}") from exc


__all__ = [
    "SessionJsonlError",
    "entry_to_json_line",
    "entry_from_json_line",
]
