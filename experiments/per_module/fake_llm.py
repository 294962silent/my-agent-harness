"""逐模块共享的假 LLM：产出真实的 StreamEvent，零网络。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from my_agent_llm.models import Message, ToolCall
from my_agent_llm.events import StreamDoneEvent, StreamStartEvent, TextDeltaEvent


class ScriptedLLM:
    """按脚本产出 StreamEvent 的假 LLM。

    每条脚本：(text, stop_reason) 或 (text, stop_reason, tool_calls)
    stop_reason: "end_turn" | "length" | "tool_calls"
    """

    def __init__(
        self,
        script: list[tuple[str, str] | tuple[str, str, list[ToolCall]]],
    ) -> None:
        self.script = script
        self.calls = 0

    async def astream_events(
        self,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        signal: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        idx = min(self.calls, len(self.script) - 1)
        entry = self.script[idx]
        self.calls += 1

        text = entry[0]
        stop_reason = entry[1]
        tool_calls: list[ToolCall] = entry[2] if len(entry) > 2 else []

        partial = Message(role="assistant", content="")
        yield StreamStartEvent(partial=partial)

        if text:
            yield TextDeltaEvent(
                delta=text, partial=Message(role="assistant", content=text)
            )

        meta: dict[str, Any] = {"stop_reason": stop_reason}
        if tool_calls:
            meta["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        final = Message(role="assistant", content=text, metadata=meta)
        yield StreamDoneEvent(
            message=final, usage={"input_tokens": 10, "output_tokens": 5}
        )


class InstantLLM:
    """立即返回固定文本的最简假 LLM（用于测装配开销）。"""

    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.calls = 0

    async def astream_events(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls += 1
        yield StreamStartEvent(partial=Message(role="assistant", content=""))
        final = Message(
            role="assistant",
            content=self.text,
            metadata={"stop_reason": "end_turn"},
        )
        yield StreamDoneEvent(
            message=final, usage={"input_tokens": 1, "output_tokens": 1}
        )
