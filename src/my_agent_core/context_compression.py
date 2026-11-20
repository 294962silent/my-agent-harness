"""Deterministic request-view compression layers.

The functions in this module never mutate the canonical Session transcript.
They return copied message lists that can be sent to a provider.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from my_agent_core.artifacts import (
    ArtifactStore,
    TRUSTED_BOUNDED_TOOLS,
    render_preview,
)
from my_agent_llm.models import Message


TokenCounter = Callable[[list[Message]], int]
PROJECTION_LAYERS = (
    "layer2:snip",
    "layer3:microcompact",
    "layer4:collapse",
)
_PROJECTION_LAYER_SET = frozenset(PROJECTION_LAYERS)
_MAX_PROJECTION_IDS = 10_000
_MAX_COLLAPSE_SUMMARY_CHARS = 100_000


@dataclass(frozen=True)
class CompressionConfig:
    enabled: bool = True
    five_layers_enabled: bool = False
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    tool_result_max_chars: int = 6_000
    tool_round_max_chars: int = 24_000
    context_trigger_ratio: float = 0.8
    microcompact_min_chars: int = 200

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        if type(self.five_layers_enabled) is not bool:
            raise ValueError("five_layers_enabled must be a bool")
        integer_values = {
            "reserve_tokens": self.reserve_tokens,
            "keep_recent_tokens": self.keep_recent_tokens,
            "tool_result_max_chars": self.tool_result_max_chars,
            "tool_round_max_chars": self.tool_round_max_chars,
            "microcompact_min_chars": self.microcompact_min_chars,
        }
        for name, value in integer_values.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.tool_round_max_chars < self.tool_result_max_chars:
            raise ValueError("tool_round_max_chars must be >= tool_result_max_chars")
        if not 0 < self.context_trigger_ratio <= 1:
            raise ValueError("context_trigger_ratio must be in (0, 1]")


@dataclass(frozen=True)
class ProjectionItem:
    projection_id: str
    message: Message
    original_index: int


def validate_projection_plan(plan: Any) -> bool:
    """Validate a persisted request projection without trusting Session JSON.

    The plan is a bounded, deterministic description of a view transformation.
    Canonical message bodies and private labels never belong here.
    """
    if not isinstance(plan, dict) or plan.get("version") != 1:
        return False
    source_count = plan.get("source_count")
    if type(source_count) is not int or source_count < 0:
        return False
    source_hash = plan.get("source_hash")
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(char not in "0123456789abcdef" for char in source_hash)
    ):
        return False

    for key in ("snip_ids", "microcompact_projection_ids"):
        values = plan.get(key)
        if (
            not isinstance(values, list)
            or len(values) > _MAX_PROJECTION_IDS
            or any(not isinstance(value, str) or not value for value in values)
        ):
            return False
        if len(values) != len(set(values)):
            return False

    layers = plan.get("layers")
    if (
        not isinstance(layers, list)
        or len(layers) > len(PROJECTION_LAYERS)
        or any(layer not in _PROJECTION_LAYER_SET for layer in layers)
        or len(layers) != len(set(layers))
    ):
        return False

    collapse = plan.get("collapse")
    if collapse is not None:
        if not isinstance(collapse, dict) or "layer4:collapse" not in layers:
            return False
        start_id = collapse.get("start_id")
        end_id = collapse.get("end_id")
        summary = collapse.get("summary")
        if not isinstance(start_id, str) or not start_id:
            return False
        if not isinstance(end_id, str) or not end_id:
            return False
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > _MAX_COLLAPSE_SUMMARY_CHARS
        ):
            return False
    elif "layer4:collapse" in layers:
        return False

    cache_epoch = plan.get("cache_epoch", 0)
    if type(cache_epoch) is not int or cache_epoch < 0:
        return False
    budget_exceeded = plan.get("budget_exceeded", False)
    if type(budget_exceeded) is not bool:
        return False
    return True


def _canonical_message(message: Message) -> str:
    return json.dumps(
        message.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def project_messages(messages: list[Message]) -> list[ProjectionItem]:
    """Assign deterministic IDs while disambiguating identical messages."""
    occurrences: dict[str, int] = defaultdict(int)
    items: list[ProjectionItem] = []
    for index, message in enumerate(messages):
        digest = hashlib.sha256(_canonical_message(message).encode("utf-8")).hexdigest()[:16]
        base = f"projection:{message.role}:{digest}"
        ordinal = occurrences[base]
        occurrences[base] += 1
        items.append(
            ProjectionItem(
                projection_id=f"{base}:{ordinal}",
                message=message,
                original_index=index,
            )
        )
    return items


def _message_chars(message: Message) -> int:
    return len(_canonical_message(message))


def _tokens(
    messages: list[Message],
    ratio: float | None,
    token_counter: TokenCounter | None,
) -> int:
    if token_counter is not None:
        return token_counter(messages)
    chars = sum(_message_chars(message) for message in messages)
    return max(1, round(chars * ratio)) if ratio is not None else max(1, chars // 4)


def _tool_call_ids(message: Message) -> set[str]:
    if message.role != "assistant" or not message.metadata:
        return set()
    calls = message.metadata.get("tool_calls") or []
    return {
        str(call.get("id"))
        for call in calls
        if isinstance(call, dict) and call.get("id") is not None
    }


def _tool_result_id(message: Message) -> str | None:
    if message.role != "tool" or not message.metadata:
        return None
    value = message.metadata.get("tool_call_id")
    return str(value) if value is not None else None


@dataclass(frozen=True)
class ConversationGroup:
    start: int
    end: int
    complete: bool

    def ids(self, items: list[ProjectionItem]) -> list[str]:
        return [item.projection_id for item in items[self.start : self.end]]


def _conversation_groups(messages: list[Message]) -> list[ConversationGroup]:
    start = 1 if messages and messages[0].role == "system" else 0
    starts = [i for i in range(start, len(messages)) if messages[i].role == "user"]
    if not starts:
        return [ConversationGroup(start, len(messages), False)] if start < len(messages) else []
    if starts[0] > start:
        starts.insert(0, start)
    groups: list[ConversationGroup] = []
    for index, group_start in enumerate(starts):
        group_end = starts[index + 1] if index + 1 < len(starts) else len(messages)
        expected: set[str] = set()
        results: set[str] = set()
        for message in messages[group_start:group_end]:
            expected.update(_tool_call_ids(message))
            result_id = _tool_result_id(message)
            if result_id is not None:
                results.add(result_id)
        complete = expected == results
        groups.append(ConversationGroup(group_start, group_end, complete))
    return groups


def _tail_group_start(
    messages: list[Message],
    *,
    keep_recent_tokens: int,
    ratio: float | None,
    token_counter: TokenCounter | None = None,
) -> int:
    groups = _conversation_groups(messages)
    if not groups:
        return len(messages)
    remaining = 0
    selected_start = groups[-1].start
    for group in reversed(groups):
        selected_start = group.start
        remaining += _tokens(
            messages[group.start : group.end],
            ratio,
            token_counter,
        )
        if remaining >= keep_recent_tokens:
            break
    return selected_start


def _legacy_spill(
    message: Message,
    *,
    index: int,
    results_dir: Path,
) -> Message:
    content = message.content if isinstance(message.content, str) else ""
    if not content:
        return message
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = results_dir.resolve()
        raw_tid = str((message.metadata or {}).get("tool_call_id", index))
        hashed_tid = hashlib.sha1(raw_tid.encode("utf-8", errors="replace")).hexdigest()[:12]
        safe_tid = raw_tid if re.fullmatch(r"[A-Za-z0-9_.-]+", raw_tid) else hashed_tid
        path = (resolved_dir / f"{safe_tid}.txt").resolve()
        if not path.is_relative_to(resolved_dir):
            path = (resolved_dir / f"{hashed_tid}.txt").resolve()
        path.write_text(content, encoding="utf-8")
    except OSError:
        return message
    metadata = dict(message.metadata or {})
    metadata["artifact_preview"] = True
    metadata["persisted_output"] = str(path)
    return message.model_copy(
        update={
            "content": (
                f"<persisted-output>\nFull: {path}\n"
                f"Preview:\n{content[:2000]}\n</persisted-output>"
            ),
            "metadata": metadata,
        }
    )


def _externalize(
    message: Message,
    *,
    index: int,
    artifact_store: ArtifactStore | None,
    results_dir: Path | None,
    threshold: int,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> Message:
    metadata = message.metadata or {}
    if message.role != "tool" or not isinstance(message.content, str):
        return message
    if metadata.get("artifact_preview"):
        return message
    tool_name = str(metadata.get("tool_name") or "tool")
    if metadata.get("artifact_bounded") is True and tool_name in TRUSTED_BOUNDED_TOOLS:
        return message
    if artifact_store is not None:
        preview, preview_meta = render_preview(
            message.content,
            artifact_store,
            tool_name=tool_name,
            tool_call_id=str(metadata.get("tool_call_id") or index),
            threshold=threshold,
            **(
                {}
                if head_chars is None or tail_chars is None
                else {"head_chars": head_chars, "tail_chars": tail_chars}
            ),
        )
        if not preview_meta.get("artifact_preview"):
            return message
        new_metadata = dict(metadata)
        new_metadata.update(preview_meta)
        return message.model_copy(update={"content": preview, "metadata": new_metadata})
    if results_dir is not None:
        return _legacy_spill(message, index=index, results_dir=results_dir)
    return message


def _turn_groups(messages: list[Message]) -> list[list[int]]:
    groups: list[list[int]] = []
    current: list[int] | None = None
    for index, message in enumerate(messages):
        ids = _tool_call_ids(message)
        if ids:
            current = [index]
            groups.append(current)
            continue
        if current is not None and message.role == "tool":
            current.append(index)
            continue
        current = None
    return groups


def apply_tool_result_budget(
    messages: list[Message],
    *,
    artifact_store: ArtifactStore | None,
    results_dir: Path | None,
    config: CompressionConfig,
) -> tuple[list[Message], dict[str, Any]]:
    view = list(messages)
    externalized: set[int] = set()

    for index, message in enumerate(view):
        if (
            message.role == "tool"
            and isinstance(message.content, str)
            and len(message.content) > config.tool_result_max_chars
        ):
            projected = _externalize(
                message,
                index=index,
                artifact_store=artifact_store,
                results_dir=results_dir,
                threshold=config.tool_result_max_chars,
            )
            if projected is not message:
                view[index] = projected
                externalized.add(index)

    for group in _turn_groups(view):
        tool_indices = [
            index
            for index in group
            if view[index].role == "tool" and isinstance(view[index].content, str)
        ]
        total = sum(
            len(view[index].content)
            for index in tool_indices
            if isinstance(view[index].content, str)
        )
        if total <= config.tool_round_max_chars:
            continue
        remaining = sorted(
            (index for index in tool_indices if index not in externalized),
            key=lambda index: (-len(view[index].content), index),
        )
        for index in remaining:
            projected = _externalize(
                view[index],
                index=index,
                artifact_store=artifact_store,
                results_dir=results_dir,
                threshold=0,
                head_chars=512,
                tail_chars=128,
            )
            if projected is view[index]:
                continue
            old_len = len(view[index].content)
            view[index] = projected
            externalized.add(index)
            total -= old_len - len(projected.content)
            if total <= config.tool_round_max_chars:
                break

    return view, {
        "layer": "layer1:tool_budget",
        "changed": bool(externalized),
        "changed_count": len(externalized),
    }


def select_snip_ids(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    ratio: float | None,
    target_tokens: int | None = None,
    reserve_groups_for_collapse: int = 0,
    token_counter: TokenCounter | None = None,
) -> list[str]:
    if type(reserve_groups_for_collapse) is not int or reserve_groups_for_collapse < 0:
        raise ValueError("reserve_groups_for_collapse must be a non-negative integer")
    messages = [item.message for item in items]
    groups = _conversation_groups(messages)
    tail_start = _tail_group_start(
        messages,
        keep_recent_tokens=keep_recent_tokens,
        ratio=ratio,
        token_counter=token_counter,
    )
    candidate_groups = [
        group
        for index, group in enumerate(groups)
        if index > 0
        and group.end <= tail_start
        and group.start < tail_start
        and group.complete
    ]
    if reserve_groups_for_collapse > 0:
        candidate_groups = candidate_groups[:-reserve_groups_for_collapse]
    selected: list[str] = []
    selected_set: set[str] = set()
    for group in candidate_groups:
        ids = group.ids(items)
        selected.extend(ids)
        selected_set.update(ids)
        if target_tokens is None:
            continue
        remaining = [
            item.message
            for item in items
            if item.projection_id not in selected_set
        ]
        if _tokens(remaining, ratio, token_counter) <= target_tokens:
            break
    return selected


def apply_snip(items: list[ProjectionItem], ids: set[str]) -> list[Message]:
    return [
        item.message.model_copy(deep=True)
        for item in items
        if item.projection_id not in ids
    ]


def apply_microcompact(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    min_chars: int,
    ratio: float | None,
    token_counter: TokenCounter | None = None,
) -> tuple[list[Message], list[str]]:
    messages = [item.message for item in items]
    tail_start = _tail_group_start(
        messages,
        keep_recent_tokens=keep_recent_tokens,
        ratio=ratio,
        token_counter=token_counter,
    )
    view: list[Message] = []
    changed: list[str] = []
    for index, item in enumerate(items):
        message = item.message
        metadata = dict(message.metadata or {})
        if (
            index < tail_start
            and message.role == "tool"
            and isinstance(message.content, str)
            and len(message.content) > min_chars
            and not metadata.get("microcompacted")
            and not metadata.get("artifact_bounded")
        ):
            call_id = str(metadata.get("tool_call_id") or item.projection_id)
            metadata["microcompacted"] = True
            metadata["microcompacted_chars"] = len(message.content)
            changed.append(call_id)
            view.append(
                message.model_copy(
                    update={
                        "content": (
                            "[microcompacted tool result; "
                            f"tool_call_id={call_id}]"
                        ),
                        "metadata": metadata,
                    }
                )
            )
            continue
        view.append(message.model_copy(deep=True))
    return view, changed


def select_collapse_range(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    ratio: float | None,
    token_counter: TokenCounter | None = None,
) -> tuple[int, int] | None:
    messages = [item.message for item in items]
    groups = _conversation_groups(messages)
    if len(groups) < 3:
        return None
    head_end = groups[0].end
    tail_start = _tail_group_start(
        messages,
        keep_recent_tokens=keep_recent_tokens,
        ratio=ratio,
        token_counter=token_counter,
    )
    if tail_start <= head_end:
        return None
    middle_groups = [
        group
        for group in groups[1:]
        if group.start >= head_end and group.end <= tail_start and group.complete
    ]
    if not middle_groups:
        return None
    return middle_groups[0].start, middle_groups[-1].end


def apply_collapse(
    messages: list[Message],
    start: int,
    end: int,
    summary: str,
) -> list[Message]:
    collapsed = Message(
        role="user",
        content=f"[Context collapse]\n\n{summary.strip()}",
        metadata={"context_collapse": True},
    )
    return [
        *(message.model_copy(deep=True) for message in messages[:start]),
        collapsed,
        *(message.model_copy(deep=True) for message in messages[end:]),
    ]


__all__ = [
    "CompressionConfig",
    "ConversationGroup",
    "ProjectionItem",
    "PROJECTION_LAYERS",
    "apply_collapse",
    "apply_microcompact",
    "apply_snip",
    "apply_tool_result_budget",
    "project_messages",
    "select_collapse_range",
    "select_snip_ids",
    "validate_projection_plan",
]
