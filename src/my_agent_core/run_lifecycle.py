"""Small per-run ownership and cooperative asynchronous execution limits.

A deadline bounds awaited harness work; it cannot preempt synchronous Python code
or roll back tool side effects. Time spent by a consumer between yielded events
counts against elapsed time but never cancels the consumer's unrelated work.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class RunLimits:
    max_turns: int | None = None
    max_seconds: float | None = None

    def __post_init__(self):
        if self.max_turns is not None and (isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns <= 0):
            raise ValueError("max_turns must be a positive integer or None")
        if self.max_seconds is not None:
            value = self.max_seconds
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("max_seconds must be a positive finite number or None")


class RunLimitExceeded(RuntimeError):
    def __init__(self, kind, limit, observed):
        self.details = {"kind": kind, "limit": limit, "observed": observed}
        super().__init__(f"run limit exceeded: {kind}")


class RunAborted(Exception):
    """An explicit Agent.abort(), distinct from cancellation of its caller."""


class RunBudget:
    def __init__(self, limits: RunLimits, *, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.turns = 0

    def check(self):
        elapsed = self.clock() - self.started
        if self.limits.max_seconds is not None and elapsed >= self.limits.max_seconds:
            raise RunLimitExceeded("max_seconds", self.limits.max_seconds, elapsed)

    def remaining(self):
        self.check()
        if self.limits.max_seconds is None:
            return None
        return max(0.0, self.limits.max_seconds - (self.clock() - self.started))

    def begin_turn(self):
        self.check()
        if self.limits.max_turns is not None and self.turns >= self.limits.max_turns:
            raise RunLimitExceeded("max_turns", self.limits.max_turns, self.turns)
        self.turns += 1
        return self.turns


class RunState:
    def __init__(self, limits: RunLimits, signal):
        self.run_id = uuid4().hex
        self.signal = signal
        self.budget = RunBudget(limits)
        self.owner = asyncio.current_task()
        self.step: asyncio.Task | None = None
        self.aborted = False
        self.finished = asyncio.Event()
        self.stream = None
        self.partial = None

    def abort(self):
        self.aborted = True
        self.signal.cancel()
        if self.step is not None:
            self.step.cancel()

    async def next_event(self, stream):
        if self.aborted:
            raise RunAborted()
        remaining = self.budget.remaining()
        self.step = asyncio.create_task(anext(stream))
        try:
            done, _ = await asyncio.wait({self.step}, timeout=remaining)
            if not done:
                self.step.cancel()
                await asyncio.gather(self.step, return_exceptions=True)
                raise RunLimitExceeded("max_seconds", self.budget.limits.max_seconds,
                                       self.budget.clock() - self.budget.started)
            try:
                return self.step.result()
            except asyncio.CancelledError:
                if self.aborted:
                    raise RunAborted() from None
                raise
        finally:
            if self.step is not None and not self.step.done():
                self.step.cancel()
                await asyncio.gather(self.step, return_exceptions=True)
            self.step = None
