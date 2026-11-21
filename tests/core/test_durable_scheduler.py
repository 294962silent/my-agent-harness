from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import time

import pytest

from my_agent_core.docker_executor import DockerSandboxExecutor
from my_agent_core.durable_scheduler import (
    DurableAdmissionRejected,
    DurableIdempotencyConflict,
    DurableJobRequest,
    DurableRetryableError,
    DurableSandboxScheduler,
    DurableSandboxWorker,
    DurableSchedulerLimits,
)
from my_agent_core.sandbox_scheduler import SandboxJobResources


def test_durable_types_are_exported_from_core_package() -> None:
    from my_agent_core import DurableSandboxScheduler as ExportedScheduler
    from my_agent_core import DockerSandboxExecutor as ExportedDockerExecutor

    assert ExportedScheduler is DurableSandboxScheduler
    assert ExportedDockerExecutor is DockerSandboxExecutor


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limits(**overrides) -> DurableSchedulerLimits:
    values = {
        "max_active_jobs": 2,
        "max_pending_jobs": 16,
        "max_active_per_tenant": 1,
        "max_pending_per_tenant": 8,
        "max_sandboxes": 2,
        "max_memory_mb": 16,
        "max_cpu_slots": 2,
        "max_process_slots": 2,
        "max_thread_slots": 4,
        "global_rpm": 100,
        "global_tpm": 10_000,
        "tenant_rpm": 100,
        "tenant_tpm": 10_000,
        "lease_ttl_seconds": 1.0,
        "retry_base_seconds": 0.01,
        "retry_max_seconds": 0.1,
    }
    values.update(overrides)
    return DurableSchedulerLimits(**values)


def _request(
    key: str,
    *,
    tenant: str = "A",
    session: str | None = None,
    memory_mb: int = 1,
) -> DurableJobRequest:
    return DurableJobRequest(
        tenant_id=tenant,
        session_id=session or f"session-{key}",
        idempotency_key=key,
        payload={"task": key},
        resources=SandboxJobResources(memory_mb=memory_mb),
    )


def test_submit_is_idempotent_bounded_and_rejects_key_reuse(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = DurableSandboxScheduler(
        tmp_path / "jobs.sqlite",
        _limits(max_pending_jobs=1, max_pending_per_tenant=1),
        clock=clock,
    )

    first = scheduler.submit(_request("one"))
    duplicate = scheduler.submit(_request("one"))
    assert duplicate.job_id == first.job_id

    with pytest.raises(DurableIdempotencyConflict):
        scheduler.submit(
            DurableJobRequest(
                tenant_id="A",
                session_id="session-different",
                idempotency_key="one",
                payload={"task": "changed"},
                resources=SandboxJobResources(memory_mb=1),
            )
        )

    with pytest.raises(DurableAdmissionRejected, match="global_queue_full"):
        scheduler.submit(_request("two"))

    assert scheduler.job_by_idempotency_key("one").job_id == first.job_id


def test_get_jobs_preserves_request_order_and_missing_values(
    tmp_path: Path,
) -> None:
    scheduler = DurableSandboxScheduler(tmp_path / "batch.sqlite", _limits())
    first = scheduler.submit(_request("one"))
    second = scheduler.submit(_request("two"))

    records = scheduler.get_jobs(["missing", second.job_id, first.job_id])

    assert records[0] is None
    assert records[1] is not None and records[1].job_id == second.job_id
    assert records[2] is not None and records[2].job_id == first.job_id


def test_global_claims_round_robin_and_share_capacity(tmp_path: Path) -> None:
    clock = FakeClock()
    db = tmp_path / "shared.sqlite"
    worker_a = DurableSandboxScheduler(db, _limits(), clock=clock)
    worker_b = DurableSandboxScheduler(db, _limits(), clock=clock)
    for key, tenant in (("a1", "A"), ("a2", "A"), ("b1", "B")):
        worker_a.submit(_request(key, tenant=tenant))

    claim_a = worker_a.claim("worker-a")
    claim_b = worker_b.claim("worker-b")
    assert claim_a is not None and claim_a.tenant_id == "A"
    assert claim_b is not None and claim_b.tenant_id == "B"
    assert worker_b.claim("worker-b-2") is None
    assert worker_a.stats().active == 2
    assert worker_a.stats().used_sandboxes == 2
    assert worker_a.stats().used_memory_mb == 2

    assert worker_a.finish(claim_a, "succeeded", result={"ok": True})
    next_claim = worker_b.claim("worker-b")
    assert next_claim is not None
    assert next_claim.tenant_id == "A"
    assert next_claim.fencing_token > claim_a.fencing_token
    assert worker_b.finish(claim_b, "succeeded", result={"ok": True})
    assert worker_a.finish(next_claim, "succeeded", result={"ok": True})


def test_same_session_is_never_claimed_twice(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = DurableSandboxScheduler(
        tmp_path / "session.sqlite",
        _limits(max_active_jobs=2, max_sandboxes=2, max_active_per_tenant=2),
        clock=clock,
    )
    first = scheduler.submit(_request("one", session="same"))
    scheduler.submit(_request("two", session="same"))
    other = scheduler.submit(_request("other", tenant="B"))

    claim_one = scheduler.claim("worker-a")
    claim_two = scheduler.claim("worker-b")
    assert claim_one is not None and claim_one.session_id == "same"
    assert claim_two is not None and claim_two.session_id == other.session_id
    assert scheduler.claim("worker-c") is None

    scheduler.finish(claim_one, "succeeded", result=True)
    scheduler.finish(claim_two, "succeeded", result=True)
    same_again = scheduler.claim("worker-c")
    assert same_again is not None
    assert same_again.job_id != first.job_id


def test_expired_worker_lease_is_requeued_and_stale_fence_is_rejected(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    scheduler = DurableSandboxScheduler(
        tmp_path / "lease.sqlite",
        _limits(
            max_active_jobs=1,
            max_sandboxes=1,
            lease_ttl_seconds=0.1,
            retry_base_seconds=0.01,
        ),
        clock=clock,
    )
    job = scheduler.submit(
        DurableJobRequest(
            tenant_id="A",
            session_id="session",
            idempotency_key="lease",
            payload={"task": "lease"},
            resources=SandboxJobResources(memory_mb=1),
            max_attempts=2,
        )
    )
    stale = scheduler.claim("worker-old")
    assert stale is not None
    clock.advance(0.11)
    assert scheduler.recover_expired() == 1
    assert scheduler.get_job(job.job_id).last_error == "lease_expired"
    clock.advance(0.02)

    fresh = scheduler.claim("worker-new")
    assert fresh is not None
    assert fresh.fencing_token > stale.fencing_token
    assert fresh.attempt == 2
    assert scheduler.finish(stale, "succeeded", result="stale") is False
    assert scheduler.finish(fresh, "succeeded", result="fresh") is True
    final = scheduler.get_job(job.job_id)
    assert final.state == "succeeded"
    assert final.result == "fresh"


def test_child_process_crash_is_recovered_after_lease_expiry(
    tmp_path: Path,
) -> None:
    db = tmp_path / "process-crash.sqlite"
    scheduler = DurableSandboxScheduler(
        db,
        _limits(
            max_active_jobs=1,
            max_sandboxes=1,
            lease_ttl_seconds=0.1,
            retry_base_seconds=0.01,
        ),
    )
    job = scheduler.submit(
        DurableJobRequest(
            tenant_id="A",
            session_id="process-crash",
            idempotency_key="process-crash",
            payload={"task": "process-crash"},
            resources=SandboxJobResources(memory_mb=1),
            max_attempts=2,
        )
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    child_code = """
import sys
sys.path.insert(0, sys.argv[2])
from my_agent_core.durable_scheduler import DurableSandboxScheduler, DurableSchedulerLimits
limits = DurableSchedulerLimits(
    max_active_jobs=1,
    max_pending_jobs=16,
    max_active_per_tenant=1,
    max_pending_per_tenant=8,
    max_sandboxes=1,
    max_memory_mb=16,
    max_cpu_slots=2,
    max_process_slots=2,
    max_thread_slots=4,
    global_rpm=100,
    global_tpm=10_000,
    tenant_rpm=100,
    tenant_tpm=10_000,
    lease_ttl_seconds=0.1,
    retry_base_seconds=0.01,
    retry_max_seconds=0.1,
)
claim = DurableSandboxScheduler(sys.argv[1], limits).claim("child-worker")
if claim is None:
    raise SystemExit(2)
print(claim.fencing_token)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(db), str(source_root)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0
    child_token = int(child.stdout.strip())

    time.sleep(0.12)
    assert scheduler.recover_expired() == 1
    time.sleep(0.02)
    recovered = scheduler.claim("parent-worker")
    assert recovered is not None
    assert recovered.job_id == job.job_id
    assert recovered.attempt == 2
    assert recovered.fencing_token > child_token
    assert scheduler.finish(
        recovered,
        "succeeded",
        result={"recovered": True},
    )


def test_global_and_tenant_rpm_tpm_are_atomic(tmp_path: Path) -> None:
    clock = FakeClock(now=120)
    db = tmp_path / "rate.sqlite"
    limits = _limits(
        global_rpm=3,
        global_tpm=100,
        tenant_rpm=2,
        tenant_tpm=80,
    )
    scheduler = DurableSandboxScheduler(db, limits, clock=clock)
    second_worker = DurableSandboxScheduler(db, limits, clock=clock)

    assert scheduler.reserve_model_request("A", 30).allowed is True
    assert second_worker.reserve_model_request("A", 30).allowed is True
    tenant_denied = scheduler.reserve_model_request("A", 1)
    assert tenant_denied.allowed is False
    assert scheduler.reserve_model_request("B", 30).allowed is True
    global_denied = scheduler.reserve_model_request("B", 1)
    assert global_denied.allowed is False

    clock.advance(61)
    assert scheduler.reserve_model_request("A", 30).allowed is True


@pytest.mark.anyio
async def test_concurrent_sqlite_claims_never_exceed_global_capacity(
    tmp_path: Path,
) -> None:
    db = tmp_path / "concurrent.sqlite"
    limits = _limits(
        max_active_jobs=2,
        max_pending_jobs=64,
        max_pending_per_tenant=16,
        max_active_per_tenant=2,
        max_sandboxes=2,
        max_memory_mb=2,
    )
    first = DurableSandboxScheduler(db, limits)
    second = DurableSandboxScheduler(db, limits)
    for index in range(20):
        first.submit(
            DurableJobRequest(
                tenant_id=f"tenant-{index % 4}",
                session_id=f"session-{index}",
                idempotency_key=f"job-{index}",
                payload={"index": index},
                resources=SandboxJobResources(memory_mb=1),
            )
        )

    attempts = await asyncio.gather(
        *(
            asyncio.to_thread(
                (first if index % 2 == 0 else second).claim,
                f"worker-{index}",
            )
            for index in range(20)
        )
    )
    claims = [claim for claim in attempts if claim is not None]
    assert len(claims) == 2
    assert len({claim.job_id for claim in claims}) == 2
    assert len({claim.session_id for claim in claims}) == 2
    assert first.stats().active == 2
    assert first.stats().used_memory_mb == 2


@pytest.mark.anyio
async def test_worker_completes_retries_and_honors_cancel(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = DurableSandboxScheduler(
        tmp_path / "worker.sqlite",
        _limits(lease_ttl_seconds=0.03),
        clock=clock,
    )
    first_job = scheduler.submit(
        DurableJobRequest(
            tenant_id="A",
            session_id="retry",
            idempotency_key="retry",
            payload={"task": "retry"},
            resources=SandboxJobResources(memory_mb=1),
            max_attempts=2,
        )
    )
    attempts = 0

    async def retrying_runner(execution):
        nonlocal attempts
        attempts += 1
        async with execution.lease(process_slots=1, thread_slots=1):
            if attempts == 1:
                raise DurableRetryableError(
                    "429 rate limited",
                    retry_after_seconds=0.001,
                )
            return {"attempts": attempts}

    worker = DurableSandboxWorker(
        scheduler,
        worker_id="worker-a",
        poll_seconds=0.001,
    )
    first = await worker.run_once(retrying_runner)
    assert first is not None and first.state == "queued"
    await asyncio.sleep(0.01)
    clock.advance(0.02)
    second = await worker.run_once(retrying_runner)
    assert second is not None and second.state == "succeeded"
    assert attempts == 2
    assert scheduler.get_job(first_job.job_id).result == {"attempts": 2}

    cancel_job = scheduler.submit(
        DurableJobRequest(
            tenant_id="A",
            session_id="cancel",
            idempotency_key="cancel",
            payload={"task": "cancel"},
            resources=SandboxJobResources(memory_mb=1),
        )
    )
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def cancellable_runner(execution):
        started.set()
        try:
            async with execution.lease(process_slots=1):
                await asyncio.Event().wait()
        finally:
            cleaned.set()

    run_task = asyncio.create_task(worker.run_once(cancellable_runner))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert scheduler.request_cancel(cancel_job.job_id)
    cancelled = await asyncio.wait_for(run_task, timeout=1.0)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert cleaned.is_set()
    assert scheduler.get_job(cancel_job.job_id).state == "cancelled"


@pytest.mark.anyio
async def test_worker_requeues_provider_rate_limit_for_next_minute(
    tmp_path: Path,
) -> None:
    clock = FakeClock(now=120)
    scheduler = DurableSandboxScheduler(
        tmp_path / "model-429.sqlite",
        _limits(
            global_rpm=10,
            tenant_rpm=1,
            global_tpm=1_000,
            tenant_tpm=1_000,
            lease_ttl_seconds=0.03,
        ),
        clock=clock,
    )
    assert scheduler.reserve_model_request("A", 1).allowed
    job = scheduler.submit(
        DurableJobRequest(
            tenant_id="A",
            session_id="rate",
            idempotency_key="rate",
            payload={"task": "rate"},
            resources=SandboxJobResources(memory_mb=1),
            max_attempts=2,
        )
    )
    worker = DurableSandboxWorker(
        scheduler,
        worker_id="worker-rate",
        poll_seconds=0.001,
    )

    async def rate_limited_runner(execution):
        execution.reserve_model_request(1)
        return "ok"

    first = await worker.run_once(rate_limited_runner)
    assert first is not None and first.state == "queued"
    record = scheduler.get_job(job.job_id)
    assert "rate limit exceeded" in record.last_error
    assert record.available_at > clock.now

    clock.advance(61)
    second = await worker.run_once(rate_limited_runner)
    assert second is not None and second.state == "succeeded"
    assert scheduler.get_job(job.job_id).result == "ok"
