"""Request projection diagnostics at the provider's final wire boundary.

What it is: a comparable summary of what the provider is about to send (model,
system, ordered tool definitions, message transcript) plus a classification of how
it changed versus the previous request.

What it is NOT: it does not prove server-side prefix cache hits and does not expose
server tokenization. It stores only short hashes, counts and tool names — never
api keys, headers, auth data or raw prompt text. Records are bounded.

Callers may explicitly mark rewrites (e.g. summary replacement, old tool output
turned into a preview) so the next record's ``reasons`` explains the change; this
is the hook for later compaction stages. Tool order is preserved as given and
never re-sorted.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

_MAX_RECORDS = 8


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def digest_of(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectionRecord:
    seq: int
    provider: str
    model: str
    classification: str
    system_hash: str | None
    tools_hash: str | None
    tools_count: int
    tools_order: tuple[str, ...]
    messages_hash: str
    messages_count: int
    options_hash: str
    roles: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RequestProjection:
    """Bounded, per-provider diagnostic recorder."""

    provider: str
    capacity: int = _MAX_RECORDS
    _records: deque[ProjectionRecord] = field(default_factory=lambda: deque(maxlen=_MAX_RECORDS))
    _last: ProjectionRecord | None = None
    _pending_reasons: list[str] = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self) -> None:
        self._records = deque(maxlen=max(1, int(self.capacity)))

    # ── 后续阶段的显式标记入口 ──────────────────────────────
    def mark_message_rewrite(self, reason: str) -> None:
        """Declare why history/messages were rewritten (e.g. branch summary)."""
        if reason:
            marker = f"messages:{reason}"
            if marker not in self._pending_reasons:
                self._pending_reasons.append(marker)
                del self._pending_reasons[:-32]

    def mark_tool_preview(self, tool_name: str, reason: str) -> None:
        """Declare that an old tool result was replaced by a preview."""
        if tool_name:
            marker = f"tool_preview:{tool_name}:{reason or 'preview'}"
            if marker not in self._pending_reasons:
                self._pending_reasons.append(marker)
                del self._pending_reasons[:-32]

    # ── 观测 ────────────────────────────────────────────────
    def observe(
        self,
        *,
        model: str | None,
        system: Any | None,
        tools: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> ProjectionRecord:
        model_name = str(model or "")
        system_hash = digest_of(system) if system is not None else None
        tools_list = list(tools or [])
        tools_order = tuple(
            str((t.get("function") or {}).get("name") or t.get("name") or "") for t in tools_list
        )
        tools_hash = digest_of(tools_list) if tools_list else None
        messages_hash = digest_of(messages)
        roles = tuple(str(m.get("role", "")) for m in messages if isinstance(m, dict))
        # Transport auth/headers are deliberately outside request diagnostics.
        config = {k: v for k, v in (options or {}).items() if k not in {
            "api_key", "authorization", "headers", "extra_headers", "http_client", "auth"
        }}
        options_hash = digest_of(config)[:16]
        message_hashes = tuple(digest_of(m) for m in messages)
        prev = self._last

        if prev is None:
            classification = "initial"
        elif prev.model != model_name:
            classification = "model_changed"
        elif prev.tools_hash != (tools_hash[:16] if tools_hash else None) or prev.tools_order != tools_order:
            classification = "tools_changed"
        elif prev.system_hash != (system_hash[:16] if system_hash else None):
            classification = "system_changed"
        elif prev.options_hash != options_hash:
            classification = "request_config_changed"
        elif prev.messages_hash == messages_hash[:16]:
            classification = "unchanged"
        elif self._is_prefix(messages):
            classification = "append_only"
        else:
            classification = "messages_rewritten"

        reasons = list(self._pending_reasons)
        self._pending_reasons.clear()
        if classification in ("messages_rewritten", "append_only") and not reasons:
            reasons.append(f"messages:{classification}")

        self._seq += 1
        record = ProjectionRecord(
            seq=self._seq,
            provider=self.provider,
            model=model_name,
            classification=classification,
            system_hash=system_hash[:16] if system_hash else None,
            tools_hash=tools_hash[:16] if tools_hash else None,
            tools_count=len(tools_list),
            tools_order=tools_order,
            messages_hash=messages_hash[:16],
            messages_count=len(messages),
            options_hash=options_hash,
            roles=roles,
            reasons=tuple(reasons),
            at=round(time.time(), 3),
        )
        self._records.append(record)
        self._last = record
        self._last_message_hashes = message_hashes
        return record

    def _is_prefix(self, messages: list[dict[str, Any]]) -> bool:
        """True when the previously observed transcript is a strict prefix of now."""
        previous = getattr(self, "_last_message_hashes", None)
        return (
            isinstance(previous, tuple)
            and len(previous) < len(messages)
            and tuple(digest_of(m) for m in messages[:len(previous)]) == previous
        )

    # ── 读取 ────────────────────────────────────────────────
    @property
    def last(self) -> ProjectionRecord | None:
        return self._last

    def history(self) -> list[ProjectionRecord]:
        return list(self._records)
