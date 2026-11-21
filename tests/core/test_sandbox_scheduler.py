from __future__ import annotations

import asyncio

import pytest

from my_agent_core.sandbox_scheduler import (
    SandboxAdmissionRejected,
    SandboxJobResources,
    SandboxJobTimeout,
    SandboxScheduler,
    SandboxSchedulerLimits,
)


def test_scheduler_is_exported_from_core_package() -> None:
    from my_agent_core import SandboxScheduler as ExportedSandboxScheduler

    assert ExportedSandboxScheduler is SandboxScheduler


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.anyio
async def test_hundred_jobs_keep_active_tasks_and_memory_bounded() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=8,
            max_pending_jobs=128,
            max_active_per_tenant=2,
            max_pending_per_tenant=32,
            max_sandboxes=8,
            max_memory_mb=8,
        )
    )
    release = asyncio.Event()
    saturated = asyncio.Event()
    active = 0
    peak_active = 0

    async def runner(execution):
        nonlocal active, peak_active
        execution.assert_active()
        active += 1
        peak_active = max(peak_active, active)
        if active == 8:
            saturated.set()
        try:
            await release.wait()
            return execution.job_id
        finally:
            active -= 1

    tickets = [
        scheduler.submit(
            runner,
            tenant_id=f"tenant-{index % 5}",
            session_id=f"session-{index}",
            resources=SandboxJobResources(memory_mb=1),
        )
        for index in range(100)
    ]

    await asyncio.wait_for(saturated.wait(), timeout=2.0)
    during = scheduler.stats()
    assert during.accepted == 100
    assert during.active == 8
    assert during.pending == 92
    assert during.used_sandboxes == 8
    assert during.used_memory_mb == 8
    running_tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("sandbox-job:")
    ]
    assert len(running_tasks) == 8

    release.set()
    results = await asyncio.gather(*tickets)
    await scheduler.await_idle()

    assert len(results) == 100
    assert peak_active == 8
    final = scheduler.stats()
    assert final.completed == 100
    assert final.pending == 0
    assert final.active == 0
    assert final.used_sandboxes == 0
    assert final.used_memory_mb == 0
    assert final.peak_active == 8
    assert final.peak_memory_mb == 8
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("sandbox-job:")
    ]
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "sandbox-scheduler"
    ]
    await scheduler.close()


@pytest.mark.anyio
async def test_tenant_round_robin_prevents_one_tenant_from_filling_worker() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=3,
            max_pending_jobs=16,
            max_active_per_tenant=1,
            max_pending_per_tenant=8,
            max_sandboxes=3,
            max_memory_mb=16,
        )
    )
    release = asyncio.Event()
    started: list[str] = []

    def runner(label: str):
        async def run(execution):
            started.append(label)
            await release.wait()
            return label

        return run

    tickets = [
        scheduler.submit(
            runner("A1"),
            tenant_id="A",
            session_id="a1",
            resources=SandboxJobResources(memory_mb=1),
        ),
        scheduler.submit(
            runner("A2"),
            tenant_id="A",
            session_id="a2",
            resources=SandboxJobResources(memory_mb=1),
        ),
        scheduler.submit(
            runner("B1"),
            tenant_id="B",
            session_id="b1",
            resources=SandboxJobResources(memory_mb=1),
        ),
        scheduler.submit(
            runner("B2"),
            tenant_id="B",
            session_id="b2",
            resources=SandboxJobResources(memory_mb=1),
        ),
        scheduler.submit(
            runner("C1"),
            tenant_id="C",
            session_id="c1",
            resources=SandboxJobResources(memory_mb=1),
        ),
    ]

    await _wait_until(lambda: len(started) == 3)
    assert started == ["A1", "B1", "C1"]
    release.set()
    assert set(await asyncio.gather(*tickets)) == {"A1", "A2", "B1", "B2", "C1"}
    await scheduler.close()


@pytest.mark.anyio
async def test_queue_admission_is_bounded_per_global_and_tenant() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=1,
            max_pending_jobs=2,
            max_active_per_tenant=1,
            max_pending_per_tenant=2,
            max_sandboxes=1,
            max_memory_mb=8,
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_runner(execution):
        started.set()
        await release.wait()
        return execution.job_id

    first = scheduler.submit(
        blocking_runner,
        tenant_id="A",
        session_id="a1",
        resources=SandboxJobResources(memory_mb=1),
    )
    await started.wait()
    second = scheduler.submit(
        blocking_runner,
        tenant_id="A",
        session_id="a2",
        resources=SandboxJobResources(memory_mb=1),
    )
    third = scheduler.submit(
        blocking_runner,
        tenant_id="B",
        session_id="b1",
        resources=SandboxJobResources(memory_mb=1),
    )
    with pytest.raises(SandboxAdmissionRejected, match="global_queue_full"):
        scheduler.submit(
            blocking_runner,
            tenant_id="B",
            session_id="b2",
            resources=SandboxJobResources(memory_mb=1),
        )

    release.set()
    await asyncio.gather(first, second, third)
    assert scheduler.stats().rejected == 1
    await scheduler.close()


@pytest.mark.anyio
async def test_same_session_is_serialized_and_fencing_tokens_increase() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=2,
            max_pending_jobs=8,
            max_active_per_tenant=2,
            max_pending_per_tenant=4,
            max_sandboxes=2,
            max_memory_mb=8,
        )
    )
    first_started = asyncio.Event()
    other_started = asyncio.Event()
    release = asyncio.Event()
    same_session_started: list[int] = []

    async def same_session_runner(execution):
        same_session_started.append(execution.fencing_token)
        if len(same_session_started) == 1:
            first_started.set()
        await release.wait()
        return execution.fencing_token

    async def other_runner(execution):
        other_started.set()
        await release.wait()
        return execution.fencing_token

    first = scheduler.submit(
        same_session_runner,
        tenant_id="A",
        session_id="same",
        resources=SandboxJobResources(memory_mb=1),
    )
    second = scheduler.submit(
        same_session_runner,
        tenant_id="A",
        session_id="same",
        resources=SandboxJobResources(memory_mb=1),
    )
    other = scheduler.submit(
        other_runner,
        tenant_id="B",
        session_id="different",
        resources=SandboxJobResources(memory_mb=1),
    )

    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await asyncio.wait_for(other_started.wait(), timeout=2.0)
    assert scheduler.stats().pending == 1
    assert first.fencing_token != other.fencing_token

    release.set()
    first_token = await first
    other_token = await other
    second_token = await second
    assert len({first_token, other_token, second_token}) == 3
    assert same_session_started == [first_token, second_token]
    await scheduler.close()


@pytest.mark.anyio
async def test_tool_slots_are_not_held_during_model_wait() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=4,
            max_pending_jobs=16,
            max_active_per_tenant=1,
            max_pending_per_tenant=8,
            max_sandboxes=4,
            max_memory_mb=16,
            max_cpu_slots=1,
            max_process_slots=1,
            max_thread_slots=2,
        )
    )
    all_waiting = asyncio.Event()
    allow_tools = asyncio.Event()
    one_tool_running = asyncio.Event()
    release_tool = asyncio.Event()
    waiting = 0
    tool_active = 0
    peak_tool_active = 0

    async def runner(execution):
        nonlocal waiting, tool_active, peak_tool_active
        waiting += 1
        if waiting == 4:
            all_waiting.set()
        await all_waiting.wait()
        await allow_tools.wait()
        async with execution.lease(
            cpu_slots=1,
            process_slots=1,
            thread_slots=1,
        ):
            tool_active += 1
            peak_tool_active = max(peak_tool_active, tool_active)
            one_tool_running.set()
            await release_tool.wait()
            tool_active -= 1
        return execution.job_id

    tickets = [
        scheduler.submit(
            runner,
            tenant_id=f"T{index}",
            session_id=f"S{index}",
            resources=SandboxJobResources(memory_mb=1),
        )
        for index in range(4)
    ]
    await asyncio.wait_for(all_waiting.wait(), timeout=2.0)
    before_tools = scheduler.stats()
    assert before_tools.active == 4
    assert before_tools.used_process_slots == 0
    assert before_tools.used_thread_slots == 0

    allow_tools.set()
    await asyncio.wait_for(one_tool_running.wait(), timeout=2.0)
    running_tool = scheduler.stats()
    assert running_tool.used_process_slots == 1
    assert running_tool.used_thread_slots == 1
    assert running_tool.peak_process_slots == 1
    assert running_tool.peak_thread_slots == 1
    assert peak_tool_active == 1

    release_tool.set()
    await asyncio.gather(*tickets)
    assert scheduler.stats().used_process_slots == 0
    assert scheduler.stats().used_thread_slots == 0
    await scheduler.close()


@pytest.mark.anyio
async def test_queued_cancellation_and_timeout_release_resources() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=1,
            max_pending_jobs=4,
            max_active_per_tenant=1,
            max_pending_per_tenant=2,
            max_sandboxes=1,
            max_memory_mb=8,
            max_process_slots=1,
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()
    queued_ran = False

    async def first_runner(execution):
        started.set()
        await release.wait()
        return "first"

    async def queued_runner(execution):
        nonlocal queued_ran
        queued_ran = True
        return execution.job_id

    first = scheduler.submit(
        first_runner,
        tenant_id="A",
        session_id="a1",
        resources=SandboxJobResources(memory_mb=1),
    )
    await started.wait()
    queued = scheduler.submit(
        queued_runner,
        tenant_id="A",
        session_id="a2",
        resources=SandboxJobResources(memory_mb=1),
    )
    assert scheduler.cancel(queued.job_id)
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert scheduler.stats().cancelled == 1
    release.set()
    await first
    assert queued_ran is False
    await scheduler.close()

    timeout_scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=1,
            max_pending_jobs=2,
            max_active_per_tenant=1,
            max_pending_per_tenant=1,
            max_sandboxes=1,
            max_memory_mb=4,
            max_process_slots=1,
        )
    )
    cleanup = asyncio.Event()

    async def hanging_runner(execution):
        try:
            async with execution.lease(process_slots=1):
                await asyncio.Event().wait()
        finally:
            cleanup.set()

    timed = timeout_scheduler.submit(
        hanging_runner,
        tenant_id="A",
        session_id="timeout",
        resources=SandboxJobResources(memory_mb=1),
        timeout_seconds=0.01,
    )
    with pytest.raises(SandboxJobTimeout):
        await timed
    await asyncio.wait_for(cleanup.wait(), timeout=1.0)
    await timeout_scheduler.await_idle()
    stats = timeout_scheduler.stats()
    assert stats.timed_out == 1
    assert stats.used_process_slots == 0
    assert stats.used_memory_mb == 0
    await timeout_scheduler.close()


@pytest.mark.anyio
async def test_resources_larger_than_capacity_are_rejected_before_queueing() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=1,
            max_pending_jobs=2,
            max_active_per_tenant=1,
            max_pending_per_tenant=1,
            max_sandboxes=1,
            max_memory_mb=16,
        )
    )

    async def runner(execution):
        return execution.job_id

    with pytest.raises(SandboxAdmissionRejected, match="memory_capacity_exceeded"):
        scheduler.submit(
            runner,
            tenant_id="A",
            session_id="memory",
            resources=SandboxJobResources(memory_mb=17),
        )
    with pytest.raises(SandboxAdmissionRejected, match="sandbox_capacity_exceeded"):
        scheduler.submit(
            runner,
            tenant_id="A",
            session_id="sandbox",
            resources=SandboxJobResources(sandbox_slots=2),
        )
    await scheduler.close()


@pytest.mark.anyio
async def test_close_cancels_active_and_queued_work_and_releases_capacity() -> None:
    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=1,
            max_pending_jobs=4,
            max_active_per_tenant=1,
            max_pending_per_tenant=2,
            max_sandboxes=1,
            max_memory_mb=2,
        )
    )
    started = asyncio.Event()
    cleanup = asyncio.Event()

    async def active_runner(execution):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()

    async def queued_runner(execution):
        return execution.job_id

    active = scheduler.submit(
        active_runner,
        tenant_id="A",
        session_id="active",
        resources=SandboxJobResources(memory_mb=1),
    )
    await started.wait()
    queued = scheduler.submit(
        queued_runner,
        tenant_id="A",
        session_id="queued",
        resources=SandboxJobResources(memory_mb=1),
    )

    await scheduler.close()
    await asyncio.wait_for(cleanup.wait(), timeout=1.0)
    with pytest.raises(asyncio.CancelledError):
        await active
    with pytest.raises(asyncio.CancelledError):
        await queued

    stats = scheduler.stats()
    assert stats.active == 0
    assert stats.pending == 0
    assert stats.used_sandboxes == 0
    assert stats.used_memory_mb == 0
