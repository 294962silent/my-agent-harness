"""Bounded, fair scheduling for concurrent sandbox-backed Agent runs.

The scheduler deliberately does not create one asyncio Task per accepted job.
Queued jobs are lightweight records. Only jobs that have acquired global,
tenant, sandbox, and memory capacity become Tasks.

CPU, process, and thread slots are acquired dynamically around tool execution.
Waiting for a model response therefore consumes a run/sandbox slot but does not
pin a worker thread or child-process slot.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
import time
from typing import Generic, Literal, TypeVar
from uuid import uuid4


T = TypeVar("T")
JobState = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
]


def _positive_int(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class SandboxSchedulerLimits:
    """Finite capacities shared by every tenant in this scheduler."""

    max_active_jobs: int = 8
    max_pending_jobs: int = 256
    max_active_per_tenant: int = 2
    max_pending_per_tenant: int = 64
    max_sandboxes: int = 8
    max_memory_mb: int = 8_192
    max_cpu_slots: int = 8
    max_process_slots: int = 8
    max_thread_slots: int = 32

    def __post_init__(self) -> None:
        for name in (
            "max_active_jobs",
            "max_pending_jobs",
            "max_active_per_tenant",
            "max_pending_per_tenant",
            "max_sandboxes",
            "max_memory_mb",
            "max_cpu_slots",
            "max_process_slots",
            "max_thread_slots",
        ):
            _positive_int(name, getattr(self, name))
        if self.max_active_jobs > self.max_sandboxes:
            raise ValueError("max_active_jobs cannot exceed max_sandboxes")
        if self.max_active_per_tenant > self.max_active_jobs:
            raise ValueError(
                "max_active_per_tenant cannot exceed max_active_jobs"
            )
        if self.max_pending_per_tenant > self.max_pending_jobs:
            raise ValueError(
                "max_pending_per_tenant cannot exceed max_pending_jobs"
            )


@dataclass(frozen=True, slots=True)
class SandboxJobResources:
    """Resources held for the full lifetime of one sandbox run."""

    sandbox_slots: int = 1
    memory_mb: int = 512

    def __post_init__(self) -> None:
        _positive_int("sandbox_slots", self.sandbox_slots)
        _positive_int("memory_mb", self.memory_mb)


@dataclass(frozen=True, slots=True)
class SandboxLease:
    job_id: str
    session_id: str
    fencing_token: int


class SandboxAdmissionRejected(RuntimeError):
    """Raised before accepting a job that cannot fit the bounded queue."""

    def __init__(
        self,
        reason: str,
        *,
        tenant_id: str,
        pending: int,
        active: int,
    ) -> None:
        self.reason = reason
        self.tenant_id = tenant_id
        self.pending = pending
        self.active = active
        super().__init__(
            f"sandbox admission rejected ({reason}): "
            f"tenant={tenant_id!r}, pending={pending}, active={active}"
        )


class SandboxJobTimeout(TimeoutError):
    """Stable timeout type surfaced to callers waiting on a ticket."""

    def __init__(
        self,
        job_id: str,
        timeout_seconds: float,
    ) -> None:
        self.job_id = job_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"sandbox job {job_id!r} exceeded {timeout_seconds:g} seconds"
        )


class SandboxResourceLimitError(RuntimeError):
    """Raised when a runner asks for a phase slot beyond configured capacity."""


@dataclass(frozen=True, slots=True)
class SandboxSchedulerStats:
    accepted: int
    rejected: int
    started: int
    completed: int
    failed: int
    cancelled: int
    timed_out: int
    pending: int
    active: int
    active_by_tenant: dict[str, int]
    used_sandboxes: int
    used_memory_mb: int
    peak_active: int
    peak_memory_mb: int
    used_cpu_slots: int
    used_process_slots: int
    used_thread_slots: int
    peak_process_slots: int
    peak_thread_slots: int


class ExecutionResourcePool:
    def __init__(
        self,
        *,
        cpu_limit: int,
        process_limit: int,
        thread_limit: int,
    ) -> None:
        self._limits = {
            "cpu_slots": cpu_limit,
            "process_slots": process_limit,
            "thread_slots": thread_limit,
        }
        self._used = {name: 0 for name in self._limits}
        self._peak = {name: 0 for name in self._limits}
        self._condition = asyncio.Condition()

    async def acquire(
        self,
        *,
        cpu_slots: int,
        process_slots: int,
        thread_slots: int,
    ) -> None:
        requested = {
            "cpu_slots": _nonnegative_int("cpu_slots", cpu_slots),
            "process_slots": _nonnegative_int(
                "process_slots", process_slots
            ),
            "thread_slots": _nonnegative_int(
                "thread_slots", thread_slots
            ),
        }
        if not any(requested.values()):
            return
        for name, amount in requested.items():
            if amount > self._limits[name]:
                raise SandboxResourceLimitError(
                    f"{name}={amount} exceeds configured limit "
                    f"{self._limits[name]}"
                )

        async with self._condition:
            await self._condition.wait_for(
                lambda: all(
                    self._used[name] + amount <= self._limits[name]
                    for name, amount in requested.items()
                )
            )
            for name, amount in requested.items():
                self._used[name] += amount
                self._peak[name] = max(
                    self._peak[name],
                    self._used[name],
                )

    async def release(
        self,
        *,
        cpu_slots: int,
        process_slots: int,
        thread_slots: int,
    ) -> None:
        requested = {
            "cpu_slots": cpu_slots,
            "process_slots": process_slots,
            "thread_slots": thread_slots,
        }
        if not any(requested.values()):
            return
        async with self._condition:
            for name, amount in requested.items():
                self._used[name] = max(0, self._used[name] - amount)
            self._condition.notify_all()

    def snapshot(self) -> tuple[dict[str, int], dict[str, int]]:
        return dict(self._used), dict(self._peak)


class SandboxExecution:
    """Handle passed to one admitted sandbox runner."""

    def __init__(
        self,
        *,
        job_id: str,
        tenant_id: str,
        session_id: str,
        lease: SandboxLease,
        resources: SandboxJobResources,
        resource_pool: ExecutionResourcePool,
        cancelled: asyncio.Event,
    ) -> None:
        self.job_id = job_id
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.lease_info = lease
        self.resources = resources
        self.memory_limit_mb = resources.memory_mb
        self._resource_pool = resource_pool
        self._cancelled = cancelled

    @property
    def fencing_token(self) -> int:
        return self.lease_info.fencing_token

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def assert_active(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError("sandbox execution was cancelled")

    @asynccontextmanager
    async def lease(
        self,
        *,
        cpu_slots: int = 0,
        process_slots: int = 0,
        thread_slots: int = 0,
    ) -> AsyncIterator[SandboxExecution]:
        """Hold execution-phase slots only while a tool/sandbox command runs."""

        self.assert_active()
        await self._resource_pool.acquire(
            cpu_slots=cpu_slots,
            process_slots=process_slots,
            thread_slots=thread_slots,
        )
        try:
            self.assert_active()
            yield self
        finally:
            await self._resource_pool.release(
                cpu_slots=cpu_slots,
                process_slots=process_slots,
                thread_slots=thread_slots,
            )


class SandboxTicket(asyncio.Future[T], Generic[T]):
    """Awaitable result carrying stable acceptance and fencing identity."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        job_id: str,
        tenant_id: str,
        session_id: str,
        accepted_seq: int,
    ) -> None:
        super().__init__(loop=loop)
        self.job_id = job_id
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.accepted_seq = accepted_seq
        self.fencing_token: int | None = None
        self._cancel_callback: Callable[[SandboxTicket[T]], None] | None = None

    def cancel(self, msg: object | None = None) -> bool:
        if self.done():
            return False
        callback = self._cancel_callback
        cancelled = super().cancel(msg=msg)
        if cancelled and callback is not None:
            callback(self)
        return cancelled


class InMemoryLeaseRegistry:
    """Single-worker session ownership with monotonically increasing fences."""

    def __init__(self) -> None:
        self._active: dict[str, SandboxLease] = {}
        self._next_token = 0

    def acquire(self, *, job_id: str, session_id: str) -> SandboxLease | None:
        if session_id in self._active:
            return None
        self._next_token += 1
        lease = SandboxLease(
            job_id=job_id,
            session_id=session_id,
            fencing_token=self._next_token,
        )
        self._active[session_id] = lease
        return lease

    def release(self, lease: SandboxLease) -> None:
        current = self._active.get(lease.session_id)
        if (
            current is not None
            and current.job_id == lease.job_id
            and current.fencing_token == lease.fencing_token
        ):
            self._active.pop(lease.session_id, None)


@dataclass(slots=True)
class _Job(Generic[T]):
    ticket: SandboxTicket[T]
    runner: Callable[[SandboxExecution], Awaitable[T]]
    resources: SandboxJobResources
    timeout_seconds: float | None
    enqueued_at: float
    state: JobState = "queued"
    task: asyncio.Task[None] | None = None
    execution: SandboxExecution | None = None
    timeout_fired: bool = False
    exception: BaseException | None = None


@dataclass(slots=True)
class _Counters:
    accepted: int = 0
    rejected: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    peak_active: int = 0
    peak_memory_mb: int = 0


class SandboxScheduler:
    """Bounded global admission plus tenant-fair sandbox scheduling."""

    def __init__(
        self,
        limits: SandboxSchedulerLimits | None = None,
        *,
        lease_registry: InMemoryLeaseRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits or SandboxSchedulerLimits()
        self._leases = lease_registry or InMemoryLeaseRegistry()
        self._clock = clock
        self._resource_pool = ExecutionResourcePool(
            cpu_limit=self._limits.max_cpu_slots,
            process_limit=self._limits.max_process_slots,
            thread_limit=self._limits.max_thread_slots,
        )
        self._tenant_queues: dict[str, deque[_Job[object]]] = {}
        self._tenant_ring: deque[str] = deque()
        self._jobs: dict[str, _Job[object]] = {}
        self._active_jobs: dict[str, _Job[object]] = {}
        self._active_by_tenant: dict[str, int] = {}
        self._active_sessions: set[str] = set()
        self._pending_count = 0
        self._used_sandboxes = 0
        self._used_memory_mb = 0
        self._accepted_seq = 0
        self._counters = _Counters()
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def limits(self) -> SandboxSchedulerLimits:
        return self._limits

    def submit(
        self,
        runner: Callable[[SandboxExecution], Awaitable[T]],
        *,
        tenant_id: str,
        session_id: str,
        resources: SandboxJobResources | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxTicket[T]:
        """Accept a job without creating its execution Task."""

        if self._closed:
            raise RuntimeError("SandboxScheduler is closed")
        if not callable(runner):
            raise TypeError("runner must be callable")
        tenant_id = self._identity("tenant_id", tenant_id)
        session_id = self._identity("session_id", session_id)
        resources = resources or SandboxJobResources()
        timeout = self._validate_timeout(timeout_seconds)
        loop = self._ensure_loop()
        self._validate_resources(tenant_id, resources)

        tenant_pending = len(self._tenant_queues.get(tenant_id, ()))
        total_active = len(self._active_jobs)
        if self._pending_count >= self._limits.max_pending_jobs:
            self._counters.rejected += 1
            raise SandboxAdmissionRejected(
                "global_queue_full",
                tenant_id=tenant_id,
                pending=self._pending_count,
                active=total_active,
            )
        if tenant_pending >= self._limits.max_pending_per_tenant:
            self._counters.rejected += 1
            raise SandboxAdmissionRejected(
                "tenant_queue_full",
                tenant_id=tenant_id,
                pending=tenant_pending,
                active=self._active_by_tenant.get(tenant_id, 0),
            )

        self._accepted_seq += 1
        job_id = uuid4().hex
        ticket: SandboxTicket[T] = SandboxTicket(
            loop=loop,
            job_id=job_id,
            tenant_id=tenant_id,
            session_id=session_id,
            accepted_seq=self._accepted_seq,
        )
        ticket._cancel_callback = self._ticket_cancelled
        ticket.add_done_callback(self._consume_ticket_exception)
        job: _Job[T] = _Job(
            ticket=ticket,
            runner=runner,
            resources=resources,
            timeout_seconds=timeout,
            enqueued_at=self._clock(),
        )
        self._jobs[job_id] = job  # type: ignore[assignment]
        queue = self._tenant_queues.setdefault(tenant_id, deque())
        if not queue:
            self._tenant_ring.append(tenant_id)
        queue.append(job)  # type: ignore[arg-type]
        self._pending_count += 1
        self._counters.accepted += 1
        self._kick()
        return ticket

    async def await_idle(self) -> None:
        """Wait until all accepted jobs have settled and resources are released."""

        while self._pending_count or self._active_jobs:
            self._kick()
            dispatcher = self._dispatcher
            if dispatcher is not None and not dispatcher.done():
                await asyncio.shield(dispatcher)
                continue
            await asyncio.sleep(0)

    async def close(self) -> None:
        """Reject new work, cancel queued/active jobs, and await cleanup."""

        if self._closed:
            return
        self._closed = True
        for queue in list(self._tenant_queues.values()):
            for job in list(queue):
                job.ticket.cancel("scheduler closed")
        for job in list(self._active_jobs.values()):
            job.ticket.cancel("scheduler closed")
        self._wake.set()
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher is not asyncio.current_task():
            await asyncio.gather(dispatcher, return_exceptions=True)
        tasks = [
            job.task
            for job in self._active_jobs.values()
            if job.task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def cancel(self, job_id: str) -> bool:
        """Cancel one queued or running job by stable job ID."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        return job.ticket.cancel("cancelled by scheduler")

    def stats(self) -> SandboxSchedulerStats:
        used, peak = self._resource_pool.snapshot()
        return SandboxSchedulerStats(
            accepted=self._counters.accepted,
            rejected=self._counters.rejected,
            started=self._counters.started,
            completed=self._counters.completed,
            failed=self._counters.failed,
            cancelled=self._counters.cancelled,
            timed_out=self._counters.timed_out,
            pending=self._pending_count,
            active=len(self._active_jobs),
            active_by_tenant=dict(self._active_by_tenant),
            used_sandboxes=self._used_sandboxes,
            used_memory_mb=self._used_memory_mb,
            peak_active=self._counters.peak_active,
            peak_memory_mb=self._counters.peak_memory_mb,
            used_cpu_slots=used["cpu_slots"],
            used_process_slots=used["process_slots"],
            used_thread_slots=used["thread_slots"],
            peak_process_slots=peak["process_slots"],
            peak_thread_slots=peak["thread_slots"],
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("SandboxScheduler cannot move between event loops")
        return loop

    def _kick(self) -> None:
        self._wake.set()
        if self._dispatcher is None or self._dispatcher.done():
            if self._closed:
                return
            self._dispatcher = asyncio.create_task(
                self._dispatch_loop(),
                name="sandbox-scheduler",
            )

    async def _dispatch_loop(self) -> None:
        try:
            while True:
                while True:
                    job = self._next_dispatchable_job()
                    if job is None:
                        break
                    self._activate(job)

                if not self._pending_count:
                    if not self._active_jobs:
                        return
                    self._wake.clear()
                    if self._pending_count:
                        continue
                    await self._wake.wait()
                    continue

                self._wake.clear()
                if self._next_dispatchable_job_exists():
                    continue
                await self._wake.wait()
        finally:
            self._dispatcher = None
            if not self._closed and (self._pending_count or self._active_jobs):
                self._kick()

    def _next_dispatchable_job_exists(self) -> bool:
        if len(self._active_jobs) >= self._limits.max_active_jobs:
            return False
        for tenant_id in self._tenant_ring:
            if self._active_by_tenant.get(tenant_id, 0) >= (
                self._limits.max_active_per_tenant
            ):
                continue
            for job in self._tenant_queues.get(tenant_id, ()):
                if (
                    job.ticket.session_id not in self._active_sessions
                    and self._fits_capacity(job)
                ):
                    return True
        return False

    def _next_dispatchable_job(self) -> _Job[object] | None:
        if len(self._active_jobs) >= self._limits.max_active_jobs:
            return None
        tenant_count = len(self._tenant_ring)
        for _ in range(tenant_count):
            tenant_id = self._tenant_ring.popleft()
            queue = self._tenant_queues.get(tenant_id)
            if not queue:
                self._tenant_queues.pop(tenant_id, None)
                continue
            self._tenant_ring.append(tenant_id)
            if self._active_by_tenant.get(tenant_id, 0) >= (
                self._limits.max_active_per_tenant
            ):
                continue
            for index, job in enumerate(queue):
                if (
                    job.ticket.session_id in self._active_sessions
                    or not self._fits_capacity(job)
                ):
                    continue
                del queue[index]
                if not queue:
                    self._tenant_queues.pop(tenant_id, None)
                    try:
                        self._tenant_ring.remove(tenant_id)
                    except ValueError:
                        pass
                return job
        return None

    def _fits_capacity(self, job: _Job[object]) -> bool:
        return (
            len(self._active_jobs) < self._limits.max_active_jobs
            and self._used_sandboxes + job.resources.sandbox_slots
            <= self._limits.max_sandboxes
            and self._used_memory_mb + job.resources.memory_mb
            <= self._limits.max_memory_mb
        )

    def _activate(self, job: _Job[object]) -> None:
        assert job.state == "queued"
        lease = self._leases.acquire(
            job_id=job.ticket.job_id,
            session_id=job.ticket.session_id,
        )
        if lease is None:
            # Reinsertion is intentionally not used: the in-memory active-session
            # check runs before this call. A custom cross-process lease registry
            # needs a future retry-aware adapter.
            raise RuntimeError("lease acquired for an already-active session")

        self._pending_count -= 1
        self._active_jobs[job.ticket.job_id] = job
        self._active_sessions.add(job.ticket.session_id)
        self._active_by_tenant[job.ticket.tenant_id] = (
            self._active_by_tenant.get(job.ticket.tenant_id, 0) + 1
        )
        self._used_sandboxes += job.resources.sandbox_slots
        self._used_memory_mb += job.resources.memory_mb
        self._counters.started += 1
        self._counters.peak_active = max(
            self._counters.peak_active,
            len(self._active_jobs),
        )
        self._counters.peak_memory_mb = max(
            self._counters.peak_memory_mb,
            self._used_memory_mb,
        )

        job.state = "running"
        job.ticket.fencing_token = lease.fencing_token
        cancelled = asyncio.Event()
        job.execution = SandboxExecution(
            job_id=job.ticket.job_id,
            tenant_id=job.ticket.tenant_id,
            session_id=job.ticket.session_id,
            lease=lease,
            resources=job.resources,
            resource_pool=self._resource_pool,
            cancelled=cancelled,
        )
        job.task = asyncio.create_task(
            self._run_job(job, cancelled),
            name=f"sandbox-job:{job.ticket.job_id[:12]}",
        )
        job.task.add_done_callback(
            lambda task, active_job=job: self._job_task_done(
                active_job,
                task,
            )
        )

    async def _run_job(
        self,
        job: _Job[object],
        cancelled: asyncio.Event,
    ) -> None:
        assert job.execution is not None
        result: object | None = None
        error: BaseException | None = None
        try:
            work = job.runner(job.execution)
            if job.timeout_seconds is None:
                result = await work
            else:
                result = await asyncio.wait_for(
                    work,
                    timeout=job.timeout_seconds,
                )
        except asyncio.TimeoutError:
            job.timeout_fired = True
            error = SandboxJobTimeout(
                job.ticket.job_id,
                job.timeout_seconds or 0,
            )
        except asyncio.CancelledError:
            cancelled.set()
            if not job.ticket.done():
                job.ticket.cancel("sandbox job cancelled")
            raise
        except BaseException as exc:
            error = exc
        cancelled.set()
        if error is not None and not job.ticket.done():
            job.ticket.set_exception(error)
        elif error is None and not job.ticket.done() and job.state == "running":
            job.ticket.set_result(result)

    def _job_task_done(
        self,
        job: _Job[object],
        task: asyncio.Task[None],
    ) -> None:
        del task
        if job.execution is not None:
            self._leases.release(job.execution.lease_info)
        self._active_sessions.discard(job.ticket.session_id)
        self._used_sandboxes -= job.resources.sandbox_slots
        self._used_memory_mb -= job.resources.memory_mb
        tenant_active = self._active_by_tenant.get(
            job.ticket.tenant_id,
            0,
        ) - 1
        if tenant_active > 0:
            self._active_by_tenant[job.ticket.tenant_id] = tenant_active
        else:
            self._active_by_tenant.pop(job.ticket.tenant_id, None)
        self._active_jobs.pop(job.ticket.job_id, None)
        self._jobs.pop(job.ticket.job_id, None)
        if job.state == "running":
            self._finish_state(job)
        self._kick()

    def _finish_state(self, job: _Job[object]) -> None:
        if job.ticket.cancelled():
            job.state = "cancelled"
            self._counters.cancelled += 1
        elif job.timeout_fired:
            job.state = "timed_out"
            self._counters.timed_out += 1
        elif job.ticket.exception() is not None:
            job.state = "failed"
            self._counters.failed += 1
        else:
            job.state = "completed"
            self._counters.completed += 1

    def _ticket_cancelled(self, ticket: SandboxTicket[object]) -> None:
        job = self._jobs.get(ticket.job_id)
        if job is None or job.state != "running" or job.task is None:
            self._remove_pending(ticket.job_id)
            self._kick()
            return
        if job.execution is not None:
            job.execution._cancelled.set()
        if not job.task.done() and job.task.cancelling() == 0:
            job.task.cancel()
        self._kick()

    def _remove_pending(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.state != "queued":
            return
        queue = self._tenant_queues.get(job.ticket.tenant_id)
        if queue is None:
            return
        try:
            queue.remove(job)
        except ValueError:
            return
        self._pending_count -= 1
        self._jobs.pop(job_id, None)
        job.state = "cancelled"
        self._counters.cancelled += 1
        if not queue:
            self._tenant_queues.pop(job.ticket.tenant_id, None)
            try:
                self._tenant_ring.remove(job.ticket.tenant_id)
            except ValueError:
                pass

    @staticmethod
    def _consume_ticket_exception(ticket: SandboxTicket[object]) -> None:
        if ticket.cancelled():
            return
        try:
            ticket.exception()
        except asyncio.CancelledError:
            return

    def _validate_resources(
        self,
        tenant_id: str,
        resources: SandboxJobResources,
    ) -> None:
        if resources.sandbox_slots > self._limits.max_sandboxes:
            self._counters.rejected += 1
            raise SandboxAdmissionRejected(
                "sandbox_capacity_exceeded",
                tenant_id=tenant_id,
                pending=self._pending_count,
                active=len(self._active_jobs),
            )
        if resources.memory_mb > self._limits.max_memory_mb:
            self._counters.rejected += 1
            raise SandboxAdmissionRejected(
                "memory_capacity_exceeded",
                tenant_id=tenant_id,
                pending=self._pending_count,
                active=len(self._active_jobs),
            )

    @staticmethod
    def _identity(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _validate_timeout(value: float | None) -> float | None:
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        return float(value)


__all__ = [
    "ExecutionResourcePool",
    "InMemoryLeaseRegistry",
    "SandboxAdmissionRejected",
    "SandboxExecution",
    "SandboxJobResources",
    "SandboxJobTimeout",
    "SandboxLease",
    "SandboxResourceLimitError",
    "SandboxScheduler",
    "SandboxSchedulerLimits",
    "SandboxSchedulerStats",
    "SandboxTicket",
]
