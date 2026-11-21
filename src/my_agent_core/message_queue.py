"""动态干预消息队列 —— 驱动 Pi 风格的 Steer 转向与 Follow-up 追问。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from my_agent_llm import MessageContent


class MessageType(str, Enum):
    """排队干预消息类型。"""

    STEERING = "steering"  # 内层循环即时转向（安全点打断）
    FOLLOWUP = "followup"  # 外层循环排队追问（任务完成后驱动）


@dataclass
class QueuedMessage:
    """队列中的一条干预消息。"""

    content: MessageContent
    type: MessageType
    created_at: float = field(default_factory=time.time)
    session_id: str | None = None
    generation: int | None = None


class MessageQueue:
    """管理运行期动态干预消息的队列。"""

    def __init__(
        self,
        steering_mode: Literal["one-at-a-time", "all"] = "one-at-a-time",
        followup_mode: Literal["one-at-a-time", "all"] = "one-at-a-time",
    ) -> None:
        self.queue: list[QueuedMessage] = []
        self.steering_mode = steering_mode
        self.followup_mode = followup_mode
        self._execution_owner: tuple[str, int] | None = None

    def set_execution_owner(self, *, session_id: str, generation: int) -> None:
        """Set the session/generation eligible to consume queued input."""
        self._execution_owner = (session_id, generation)

    def clear_execution_owner(self, *, session_id: str, generation: int) -> None:
        """Clear eligibility only when the caller still owns the context."""
        if self._execution_owner == (session_id, generation):
            self._execution_owner = None

    def _is_eligible(self, message: QueuedMessage) -> bool:
        if self._execution_owner is None:
            return True
        if message.session_id is None and message.generation is None:
            return True
        return (message.session_id, message.generation) == self._execution_owner

    def add_steering(
        self,
        message: MessageContent,
        *,
        session_id: str | None = None,
        generation: int | None = None,
    ) -> QueuedMessage:
        """追加一条 Steering 转向消息。"""
        safe_content = copy.deepcopy(message) if isinstance(message, list) else message
        queued = QueuedMessage(
            content=safe_content,
            type=MessageType.STEERING,
            session_id=session_id,
            generation=generation,
        )
        self.queue.append(queued)
        return queued

    def add_followup(
        self,
        message: MessageContent,
        *,
        session_id: str | None = None,
        generation: int | None = None,
    ) -> QueuedMessage:
        """追加一条 Follow-up 追问消息。"""
        safe_content = copy.deepcopy(message) if isinstance(message, list) else message
        queued = QueuedMessage(
            content=safe_content,
            type=MessageType.FOLLOWUP,
            session_id=session_id,
            generation=generation,
        )
        self.queue.append(queued)
        return queued

    def get_steering_messages(self) -> list[QueuedMessage]:
        """获取并弹出待消费的 steering 消息。"""
        steering = [
            m
            for m in self.queue
            if m.type == MessageType.STEERING and self._is_eligible(m)
        ]
        if not steering:
            return []

        if self.steering_mode == "one-at-a-time":
            first = steering[0]
            self.queue.remove(first)
            return [first]

        steering_ids = {id(message) for message in steering}
        self.queue = [m for m in self.queue if id(m) not in steering_ids]
        return steering

    def get_followup_messages(self) -> list[QueuedMessage]:
        """获取并弹出待消费的 followup 消息。"""
        followup = [
            m
            for m in self.queue
            if m.type == MessageType.FOLLOWUP and self._is_eligible(m)
        ]
        if not followup:
            return []

        if self.followup_mode == "one-at-a-time":
            first = followup[0]
            self.queue.remove(first)
            return [first]

        followup_ids = {id(message) for message in followup}
        self.queue = [m for m in self.queue if id(m) not in followup_ids]
        return followup

    def pop_followup(self) -> QueuedMessage | None:
        """原子移除最早的一条 follow-up，不受批量消费模式影响。"""
        for index, message in enumerate(self.queue):
            if message.type == MessageType.FOLLOWUP and self._is_eligible(message):
                return self.queue.pop(index)
        return None

    def pop_next(self) -> QueuedMessage | None:
        """原子移除最早入队的消息，不受类型与消费模式影响。"""
        for index, message in enumerate(self.queue):
            if self._is_eligible(message):
                return self.queue.pop(index)
        return None

    def discard_stale(self, *, session_id: str, generation: int) -> list[QueuedMessage]:
        """Remove foreign or older input while retaining future reservations."""
        snapshot = list(self.queue)
        stale = [
            message
            for message in snapshot
            if (message.session_id is not None and message.session_id != session_id)
            or (
                message.session_id in {None, session_id}
                and message.generation is not None
                and message.generation < generation
            )
        ]
        if not stale:
            return []
        stale_ids = {id(message) for message in stale}
        self.queue = [message for message in snapshot if id(message) not in stale_ids]
        return stale

    def has_steering(self) -> bool:
        """检查是否存在未决的 steering 消息。"""
        return any(m.type == MessageType.STEERING and self._is_eligible(m) for m in self.queue)

    def has_followup(self) -> bool:
        """检查是否存在未决的 followup 消息。"""
        return any(m.type == MessageType.FOLLOWUP and self._is_eligible(m) for m in self.queue)

    def peek(self) -> QueuedMessage | None:
        """查看队首消息但不弹出。"""
        return self.queue[0] if self.queue else None

    def clear(self) -> list[QueuedMessage]:
        """清空队列并返回被清除的消息列表。"""
        cleared = list(self.queue)
        self.queue.clear()
        return cleared

    def get_status(self) -> str:
        """获取当前队列的可读状态。"""
        if not self.queue:
            return "Queue empty"
        steering_count = sum(1 for m in self.queue if m.type == MessageType.STEERING)
        followup_count = sum(1 for m in self.queue if m.type == MessageType.FOLLOWUP)
        parts = []
        if steering_count:
            parts.append(f"{steering_count} steering")
        if followup_count:
            parts.append(f"{followup_count} follow-up")
        return f"Queued: {', '.join(parts)}"

    def __len__(self) -> int:
        return len(self.queue)

    def __bool__(self) -> bool:
        return len(self.queue) > 0
