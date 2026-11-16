"""Deterministic local context projections. No provider cache-edit API."""

from dataclasses import dataclass
import json

from my_agent_core.artifacts import TRUSTED_BOUNDED_TOOLS


class ContextBudgetExceeded(RuntimeError):
    """No safe request fits the configured input/output reserves."""


@dataclass(frozen=True)
class CompactionPolicy:
    mode: str = "baseline"
    recent_tool_groups: int = 2
    micro_min_chars: int = 1000
    model_context_window: int | None = None
    output_reserve: int = 1024
    safety_margin: int = 256
    summary_max_tokens: int = 1024

    def __post_init__(self):
        if self.mode not in ("baseline", "layered"):
            raise ValueError("unknown compaction mode")
        for name in ("recent_tool_groups", "micro_min_chars", "output_reserve", "safety_margin", "summary_max_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name in ("micro_min_chars", "summary_max_tokens") else 0):
                raise ValueError("invalid compaction reserve/limit")
        if self.model_context_window is not None and (
            type(self.model_context_window) is not int
            or self.model_context_window <= self.output_reserve + self.safety_margin + self.summary_max_tokens
        ):
            raise ValueError("context window cannot accommodate configured reserves")

    def input_limit(self, budget):
        window = self.model_context_window or budget
        return min(budget, window - self.output_reserve - self.safety_margin)

    def capability_profile(self):
        return {
            "mode": self.mode,
            "tool_micro": self.mode == "layered",
            "completed_prefix_fold": "explicit_caller_only" if self.mode == "layered" else "disabled",
            "precision_trimming": "unsupported",
            "provider_cache_edits": "not_sent",
            "model_context_window": self.model_context_window,
            "output_reserve": self.output_reserve,
            "safety_margin": self.safety_margin,
            "summary_max_tokens": self.summary_max_tokens,
            "recent_tool_groups": self.recent_tool_groups,
            "micro_min_chars": self.micro_min_chars,
        }


def _ids(message):
    calls = (message.metadata or {}).get("tool_calls") or []
    values = [c.get("id", "") if isinstance(c, dict) else getattr(c, "id", "") for c in calls]
    return [value if isinstance(value, str) else "" for value in values]


def closed_groups(messages):
    """Only unambiguous contiguous assistant(call)+all tool results, followed by a turn."""
    groups = []
    for i, message in enumerate(messages):
        ids = _ids(message) if message.role == "assistant" else []
        if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
            continue
        end = i + 1
        while end < len(messages) and messages[end].role == "tool":
            end += 1
        actual = [str((m.metadata or {}).get("tool_call_id", "")) for m in messages[i + 1 : end]]
        if end < len(messages) and len(actual) == len(ids) and set(actual) == set(ids):
            groups.append((i, end))
    return groups


def successful(message):
    """Unknown/error status is retained. Never infer success from arbitrary prose."""
    meta = message.metadata or {}
    if meta.get("is_error") or meta.get("error") or meta.get("success") is False:
        return False
    try:
        value = json.loads(message.content)
    except (TypeError, ValueError):
        value = {}
    if isinstance(value, dict):
        if value.get("error") or value.get("success") is False:
            return False
        for key in ("exit_code", "return_code"):
            if key in value:
                return type(value[key]) is int and value[key] == 0
    return meta.get("success") is True or meta.get("is_error") is False


def micro_project(view, originals, store, policy):
    """Same-length copies; originals are aligned pre-projection views, never rewritten."""
    out = list(view)
    if store is None:
        return out, 0
    groups = closed_groups(originals)
    eligible = groups[: max(0, len(groups) - policy.recent_tool_groups)]
    changed = 0
    for start, end in eligible:
        group = originals[start + 1 : end]
        if any(not successful(m) for m in group):
            continue  # all parallel results retain their necessary failure/status context
        updates = []
        for offset, message in enumerate(group, start + 1):
            meta = message.metadata or {}
            if (
                not isinstance(message.content, str)
                or len(message.content) < policy.micro_min_chars
                or meta.get("tool_name") in TRUSTED_BOUNDED_TOOLS
            ):
                continue
            try:
                ref = store.save(
                    message.content,
                    tool_name=str(meta.get("tool_name") or "tool"),
                    tool_call_id=str(meta.get("tool_call_id") or ""),
                )
                store.resolve(ref.handle())  # advertise only a verified readable immutable object
            except Exception:
                continue
            content = (
                f"[Earlier successful tool result; full output: {ref.handle()} bytes={ref.size}]\n"
                f"[Use artifact_read id={ref.handle()} cursor=0; status=success.]"
            )
            metadata = {**meta, "artifact_id": ref.handle(), "artifact_preview": True, "micro_projection": True}
            updates.append((offset, message.model_copy(update={"content": content, "metadata": metadata})))
        for index, message in updates:
            out[index] = message
        changed += bool(updates)
    return out, changed
