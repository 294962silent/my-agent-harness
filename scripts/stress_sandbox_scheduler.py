"""Reproduce the bounded 100-job sandbox scheduler stress test."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tracemalloc

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from my_agent_core.sandbox_scheduler import (  # noqa: E402
    SandboxJobResources,
    SandboxScheduler,
    SandboxSchedulerLimits,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--active", type=int, default=4)
    parser.add_argument("--tenants", type=int, default=5)
    parser.add_argument("--payload-mb", type=int, default=1)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, int]:
    if min(args.jobs, args.active, args.tenants, args.payload_mb) <= 0:
        raise ValueError("all stress arguments must be positive")
    if args.active > args.jobs:
        raise ValueError("active cannot exceed jobs")

    scheduler = SandboxScheduler(
        SandboxSchedulerLimits(
            max_active_jobs=args.active,
            max_pending_jobs=max(16, args.jobs * 2),
            max_active_per_tenant=max(1, args.active // args.tenants + 1),
            max_pending_per_tenant=max(8, args.jobs),
            max_sandboxes=args.active,
            max_memory_mb=args.active * args.payload_mb,
        )
    )
    saturated = asyncio.Event()
    release = asyncio.Event()
    active_payload_bytes = 0

    async def runner(execution):
        nonlocal active_payload_bytes
        payload = bytearray(args.payload_mb * 1024 * 1024)
        active_payload_bytes += len(payload)
        if active_payload_bytes >= args.active * args.payload_mb * 1024 * 1024:
            saturated.set()
        try:
            await release.wait()
            return execution.job_id
        finally:
            active_payload_bytes -= len(payload)

    tracemalloc.start()
    tickets = [
        scheduler.submit(
            runner,
            tenant_id=f"tenant-{index % args.tenants}",
            session_id=f"session-{index}",
            resources=SandboxJobResources(memory_mb=args.payload_mb),
        )
        for index in range(args.jobs)
    ]
    await asyncio.wait_for(saturated.wait(), timeout=5)
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    snapshot = scheduler.stats()
    snapshot_payload_bytes = active_payload_bytes
    peak_tasks = len(
        [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("sandbox-job:")
        ]
    )

    release.set()
    await asyncio.gather(*tickets)
    await scheduler.await_idle()
    await scheduler.close()
    tracemalloc.stop()

    final = scheduler.stats()
    return {
        "jobs": args.jobs,
        "active": snapshot.active,
        "pending": snapshot.pending,
        "peak_active": snapshot.peak_active,
        "sandbox_tasks": peak_tasks,
        "active_payload_bytes": snapshot_payload_bytes,
        "traced_current_bytes": traced_current,
        "traced_peak_bytes": traced_peak,
        "completed": final.completed,
        "final_active": final.active,
        "final_pending": final.pending,
        "final_memory_mb": final.used_memory_mb,
    }


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), sort_keys=True))


if __name__ == "__main__":
    main()
