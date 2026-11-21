"""Stress the SQLite-backed scheduler with multiple logical worker instances."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from my_agent_core.durable_scheduler import (  # noqa: E402
    DurableJobRequest,
    DurableSandboxScheduler,
    DurableSandboxWorker,
    DurableSchedulerLimits,
)
from my_agent_core.sandbox_scheduler import (  # noqa: E402
    SandboxJobResources,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--active", type=int, default=8)
    parser.add_argument("--tenants", type=int, default=5)
    parser.add_argument("--payload-mb", type=int, default=1)
    parser.add_argument("--run-seconds", type=float, default=0.2)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, int | float]:
    if min(args.jobs, args.workers, args.active, args.tenants) <= 0:
        raise ValueError("jobs/workers/active/tenants must be positive")
    if args.active > args.jobs:
        raise ValueError("active cannot exceed jobs")
    if args.payload_mb <= 0 or args.run_seconds <= 0:
        raise ValueError("payload-mb and run-seconds must be positive")

    temp_dir = tempfile.TemporaryDirectory(prefix="longarc-durable-stress-")
    root = Path(temp_dir.name)
    db = root / "scheduler.sqlite"
    limits = DurableSchedulerLimits(
        max_active_jobs=args.active,
        max_pending_jobs=max(32, args.jobs * 2),
        max_active_per_tenant=max(1, args.active // args.tenants + 1),
        max_pending_per_tenant=max(32, args.jobs),
        max_sandboxes=args.active,
        max_memory_mb=args.active * args.payload_mb,
        max_process_slots=args.workers * args.active,
        max_thread_slots=args.workers * args.active,
        global_rpm=100_000,
        global_tpm=1_000_000_000,
        tenant_rpm=100_000,
        tenant_tpm=1_000_000_000,
        lease_ttl_seconds=2,
    )
    schedulers = [
        DurableSandboxScheduler(db, limits)
        for _ in range(args.workers)
    ]
    for index in range(args.jobs):
        schedulers[0].submit(
            DurableJobRequest(
                tenant_id=f"tenant-{index % args.tenants}",
                session_id=f"session-{index}",
                idempotency_key=f"job-{index}",
                payload={"index": index},
                resources=SandboxJobResources(
                    memory_mb=args.payload_mb,
                ),
            )
        )

    stop = asyncio.Event()
    active = 0
    peak_active = 0
    completed = 0

    async def runner(execution):
        nonlocal active, peak_active, completed
        payload = bytearray(args.payload_mb * 1024 * 1024)
        active += 1
        peak_active = max(peak_active, active)
        try:
            async with execution.lease(
                cpu_slots=1,
                process_slots=1,
                thread_slots=1,
            ):
                await asyncio.sleep(args.run_seconds)
                completed += 1
                return {
                    "index": execution.claim.payload["index"],
                    "bytes": len(payload),
                }
        finally:
            active -= 1

    async def worker_loop(worker):
        while not stop.is_set():
            result = await worker.run_once(runner)
            if result is None:
                await asyncio.sleep(0.01)

    tracemalloc.start()
    workers = [
        DurableSandboxWorker(
            scheduler,
            worker_id=f"worker-{index}",
            poll_seconds=0.01,
        )
        for index, scheduler in enumerate(schedulers)
    ]
    tasks = [
        asyncio.create_task(worker_loop(worker))
        for worker in workers
    ]
    while True:
        stats = schedulers[0].stats()
        if (
            stats.succeeded
            + stats.failed
            + stats.timed_out
            + stats.cancelled
            >= args.jobs
        ):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.gather(*tasks)
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    final = schedulers[0].stats()
    result = {
        "jobs": args.jobs,
        "workers": args.workers,
        "peak_active": peak_active,
        "completed": completed,
        "succeeded": final.succeeded,
        "failed": final.failed,
        "queued": final.queued,
        "active_resources": final.active,
        "used_sandboxes": final.used_sandboxes,
        "used_memory_mb": final.used_memory_mb,
        "traced_current_bytes": traced_current,
        "traced_peak_bytes": traced_peak,
    }
    temp_dir.cleanup()
    return result


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), sort_keys=True))


if __name__ == "__main__":
    main()
