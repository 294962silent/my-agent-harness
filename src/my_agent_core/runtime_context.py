"""Runtime context projection: explicit dynamic facts, delivered as stable snapshots.

Design rules
------------
- The snapshot is a normal ``user`` message appended at the end of the transcript
  right before a new turn (a safe boundary). It is never injected into the system
  prompt and never placed between an assistant ``tool_calls`` message and its
  tool results.
- First delivery appends the full snapshot. If nothing changed, no second copy is
  appended. A change appends a NEW full snapshot; earlier snapshots are never
  rewritten (C1/C2).
- Clearing appends one explicit clear marker, then stays silent until facts return.
- Dedupe is stateless: it looks at what is actually visible in the message list /
  restored session history. A different session (or a branch that no longer shows
  the old snapshot) therefore re-projects instead of wrongly assuming delivery.
- With no dynamic facts and no prior snapshot, nothing is added: old behaviour is
  preserved byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from my_agent_llm.models import Message

SNAPSHOT_KIND = "runtime_context_snapshot"
CLEAR_KIND = "runtime_context_clear"
_SNAPSHOT_HEADER = "[RUNTIME CONTEXT SNAPSHOT]"
_CLEAR_TEXT = (
    "[RUNTIME CONTEXT CLEARED]\n"
    "No runtime facts are currently available for this session."
)


class RuntimeContext:
    """Mutable, source-explicit runtime facts owned by the host/Agent.

    Keys are short labels chosen by the caller (e.g. ``cwd``, ``gpu``,
    ``active_task``). Values are strings rendered verbatim; callers must not put
    secrets here because snapshots enter the model input.
    """

    def __init__(self) -> None:
        self._facts: dict[str, str] = {}
        self._generation = 0

    # ── 公开 API ────────────────────────────────────────────
    def set(self, key: str, value: Any) -> None:
        if not key or not isinstance(key, str):
            raise ValueError("runtime fact key must be a non-empty string")
        text = "" if value is None else str(value)
        if self._facts.get(key) == text:
            return
        self._facts[key] = text
        self._generation += 1

    def update(self, facts: dict[str, Any]) -> None:
        for key, value in facts.items():
            self.set(key, value)

    def remove(self, key: str) -> None:
        if key in self._facts:
            del self._facts[key]
            self._generation += 1

    def clear(self) -> None:
        if self._facts:
            self._facts.clear()
            self._generation += 1

    def snapshot(self) -> dict[str, str]:
        return dict(self._facts)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def is_empty(self) -> bool:
        return not self._facts

    @property
    def digest(self) -> str:
        payload = json.dumps(self._facts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def render(self) -> str:
        body = "\n".join(f"- {k}: {v}" for k, v in sorted(self._facts.items()))
        return f"{_SNAPSHOT_HEADER}\ndigest: {self.digest[:16]}\n{body}\n[/RUNTIME CONTEXT SNAPSHOT]"


def _projection_metadata(message: Message) -> dict[str, Any] | None:
    meta = message.metadata
    if not isinstance(meta, dict):
        return None
    kind = meta.get("kind")
    if kind in (SNAPSHOT_KIND, CLEAR_KIND):
        return meta
    return None


def last_projection(messages: list[Message]) -> dict[str, Any] | None:
    """Most recent runtime-context projection visible in the transcript, if any."""
    for message in reversed(messages):
        meta = _projection_metadata(message)
        if meta is not None:
            return meta
    return None


def projection_message(ctx: RuntimeContext, messages: list[Message]) -> Message | None:
    """Return the message to append now, or ``None`` when nothing should change."""
    last = last_projection(messages)
    if ctx.is_empty:
        if last is None or last.get("kind") == CLEAR_KIND:
            return None  # nothing, or already clearly cleared
        return Message(
            role="user",
            content=_CLEAR_TEXT,
            metadata={"kind": CLEAR_KIND, "generation": ctx.generation},
        )
    if last is not None and last.get("kind") == SNAPSHOT_KIND and last.get("snapshot_hash") == ctx.digest:
        return None  # unchanged; do not repeat
    return Message(
        role="user",
        content=ctx.render(),
        metadata={
            "kind": SNAPSHOT_KIND,
            "snapshot_hash": ctx.digest,
            "generation": ctx.generation,
        },
    )
