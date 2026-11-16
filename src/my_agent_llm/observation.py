"""Metadata-only call observation. Observers must be fast and synchronous.

Usage is the provider-normalized snapshot, not raw SDK billing or estimated cost.
No prompts, response bodies, provider options or exception messages are recorded.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
import time
from types import MappingProxyType
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True)
class ObservationContext:
    root_id: str | None = None
    parent_id: str | None = None
    purpose: str | None = None


_CONTEXT: ContextVar[ObservationContext] = ContextVar("llm_observation", default=ObservationContext())


@contextmanager
def observation_context(*, root_id: str | None = None, parent_id: str | None = None,
                        purpose: str | None = None) -> Iterator[ObservationContext]:
    """Inherit unspecified values; usable around sync calls and across awaits."""
    current = _CONTEXT.get()
    context = ObservationContext(root_id if root_id is not None else current.root_id,
                                 parent_id if parent_id is not None else current.parent_id,
                                 purpose if purpose is not None else current.purpose)
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


@dataclass(frozen=True)
class CallStart:
    call_id: str
    root_id: str
    parent_id: str | None
    purpose: str
    provider: str
    model: str
    method: str
    started_at: float


@dataclass(frozen=True)
class CallEnd(CallStart):
    status: Literal["ok", "error", "cancelled", "closed"]
    duration_seconds: float
    usage: Mapping[str, int] | None = None
    error_type: str | None = None

    def __post_init__(self):
        if self.usage is not None:
            object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


CallRecord = CallStart | CallEnd
Observer = Callable[[CallRecord], None]


class ObserverSubscription:
    """An idempotent handle removing only the observer it registered."""

    def __init__(self, remove: Callable[[], None]):
        self._remove: Callable[[], None] | None = remove
        self._lock = Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._remove is None

    def close(self) -> None:
        with self._lock:
            remove, self._remove = self._remove, None
        if remove is not None:
            remove()


class InMemoryObserver:
    """Thread-safe bounded recorder for tests and local evaluation."""
    def __init__(self, capacity: int = 2048):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._records: deque[CallRecord] = deque(maxlen=capacity)
        self._lock = Lock()
        self.dropped = 0

    def __call__(self, record: CallRecord) -> None:
        with self._lock:
            if len(self._records) == self._records.maxlen:
                self.dropped += 1
            self._records.append(record)

    @property
    def records(self) -> tuple[CallRecord, ...]:
        with self._lock:
            return tuple(self._records)


class CallObservation:
    """Internal lifecycle helper; does not retain call inputs or response bodies."""
    def __init__(self, observer: Observer | None, *, provider: str, model: str, method: str):
        context = _CONTEXT.get()
        self.start = CallStart(uuid4().hex, context.root_id or uuid4().hex, context.parent_id,
                               context.purpose or "unspecified", provider, model, method, time.time())
        self.observer = observer
        self.clock = time.monotonic()
        self.usage: Mapping[str, int] | None = None
        self.status: Literal["ok", "error", "cancelled", "closed"] = "ok"
        self.error_type: str | None = None
        self.finished = False
        self._emit(self.start)

    def _emit(self, record: CallRecord) -> None:
        if self.observer is not None:
            try:
                self.observer(record)
            except BaseException:
                # Observer cancellation/errors must never cancel the model task.
                pass

    def scope(self):
        return observation_context(root_id=self.start.root_id, parent_id=self.start.call_id,
                                   purpose=self.start.purpose)

    def update_usage(self, usage: Mapping[str, int] | None) -> None:
        if usage is not None:
            # Streaming values may already be cumulative; keep the latest, never sum.
            self.usage = MappingProxyType(dict(usage))

    def update_outcome(self, outcome) -> None:
        if outcome in ("cancelled", "aborted"):
            self.status = "cancelled"
        elif outcome == "provider_error":
            self.status = "error"

    def fail(self, error: BaseException) -> None:
        if isinstance(error, GeneratorExit):
            if self.status == "ok":
                self.status = "closed"
        else:
            self.status = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            self.error_type = type(error).__name__

    def finish(self) -> None:
        if not self.finished:
            self.finished = True
            self._emit(CallEnd(**vars(self.start), status=self.status,
                               duration_seconds=max(0, time.monotonic() - self.clock),
                               usage=self.usage, error_type=self.error_type))
