"""Explicit, metadata-only Langfuse runtime with subscriber-scoped ownership.

No optional SDK import, client or thread is created while disabled. Application
owners should finish in-flight calls before close: call snapshots intentionally
keep their original subscribers, and late records then count as dropped.
"""

from __future__ import annotations

import math
from threading import Lock
from typing import TYPE_CHECKING

from .observation import ObserverSubscription
from .observation_export import BoundedExporter

if TYPE_CHECKING:
    from .client import LLM


class ObservationRuntime:
    """Own one exporter and its subscriptions; never own an LLM or base observer."""

    def __init__(self, exporter: BoundedExporter):
        self.exporter = exporter
        self._lock = Lock()
        self._closed = False
        self._subscriptions: dict[LLM, ObserverSubscription] = {}

    def attach(self, llm: LLM) -> ObserverSubscription:
        with self._lock:
            if self._closed:
                raise RuntimeError("observation runtime is closed")
            current = self._subscriptions.get(llm)
            if current is not None and not current.closed:
                return current
            subscription = llm.add_observer(self.exporter)
            self._subscriptions[llm] = subscription
            return subscription

    def close(self, timeout: float = 5) -> bool:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        with self._lock:
            self._closed = True
            subscriptions = tuple(self._subscriptions.values())
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.close()
        return self.exporter.close(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self.exporter.is_alive

    @property
    def dropped(self) -> int:
        return self.exporter.dropped

    @property
    def export_errors(self) -> int:
        return self.exporter.export_errors

    @property
    def close_errors(self) -> int:
        return self.exporter.close_errors


def create_observation_runtime(
    *,
    enabled: bool = False,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    capacity: int = 1024,
    network_timeout: float = 5,
) -> ObservationRuntime | None:
    """Create an owned optional runtime; enabled but missing credentials is an error."""
    if not enabled:
        return None
    if not public_key or not secret_key:
        raise ValueError("Explicit Langfuse public and secret keys are required")
    if type(capacity) is not int or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    if not math.isfinite(network_timeout) or network_timeout <= 0:
        raise ValueError("network_timeout must be finite and positive")
    from .langfuse_export import LangfuseSink

    sink = LangfuseSink.from_credentials(
        public_key=public_key, secret_key=secret_key, base_url=base_url, timeout=network_timeout
    )
    return ObservationRuntime(BoundedExporter(sink, capacity=capacity))
