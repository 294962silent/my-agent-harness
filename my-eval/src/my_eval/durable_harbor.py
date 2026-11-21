"""Run real Harbor Trials through LongArc's durable SQLite scheduler."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Awaitable, Callable

from my_agent_core.docker_executor import DockerSandboxExecutor
from my_agent_core.durable_scheduler import (
    DurableJobRecord,
    DurableJobRequest,
    DurableRetryableError,
    DurableSandboxExecution,
    DurableSandboxScheduler,
    DurableSandboxWorker,
    DurableSchedulerLimits,
)
from my_agent_core.sandbox_scheduler import SandboxJobResources


TERMINAL_STATES = {"succeeded", "failed", "timed_out", "cancelled"}


@dataclass(frozen=True, slots=True)
class HarborTask:
    name: str
    path: Path
    instruction_sha256: str

    @classmethod
    def from_path(cls, path: Path) -> "HarborTask":
        task_path = path.resolve()
        if not (task_path / "task.toml").is_file():
            raise FileNotFoundError(f"not a Harbor task: {task_path}")
        instruction = task_path / "instruction.md"
        payload = instruction.read_bytes() if instruction.is_file() else b""
        return cls(
            name=task_path.name,
            path=task_path,
            instruction_sha256=hashlib.sha256(payload).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class HarborRunSummary:
    returncode: int
    reward: float | None
    passed: int
    total: int
    result_path: str | None
    log_path: str
    duration_sec: float
    input_tokens: int
    output_tokens: int
    cache_tokens: int
    error: str | None = None


class DurableHarborRunner:
    """Execute one claimed durable job as one real Harbor CLI Trial."""

    def __init__(
        self,
        *,
        harbor_binary: str,
        jobs_dir: Path,
        env_file: Path,
        model: str,
        environment_import: str = (
            "my_eval.harbor_environment:LongArcHarborEnvironment"
        ),
        agent_import: str = "my_eval.agent:MyAgentHarness",
        command_prefix: tuple[str, ...] = (),
        cwd: Path | None = None,
        terminate_grace_seconds: float = 10.0,
    ) -> None:
        if not harbor_binary:
            raise ValueError("harbor_binary must be non-empty")
        if not model:
            raise ValueError("model must be non-empty")
        self.harbor_binary = harbor_binary
        self.jobs_dir = jobs_dir.resolve()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.env_file = env_file.resolve()
        if not self.env_file.is_file():
            raise FileNotFoundError(self.env_file)
        self.model = model
        self.environment_import = environment_import
        self.agent_import = agent_import
        self.command_prefix = command_prefix
        self.cwd = cwd.resolve() if cwd else None
        self.terminate_grace_seconds = terminate_grace_seconds
        self._docker = DockerSandboxExecutor()

    def _job_name(self, execution: DurableSandboxExecution) -> str:
        raw = f"longarc-{execution.job_id[:16]}-{execution.fencing_token}"
        return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)

    def build_command(
        self,
        execution: DurableSandboxExecution,
        task: HarborTask,
    ) -> tuple[str, ...]:
        job_name = self._job_name(execution)
        memory_mb = max(128, int(execution.memory_limit_mb))
        args = [
            self.harbor_binary,
            "run",
            "-p",
            str(task.path),
            "-a",
            self.agent_import,
            "--env",
            self.environment_import,
            "-m",
            self.model,
            "--env-file",
            str(self.env_file),
            "-n",
            "1",
            "--override-cpus",
            "1",
            "--override-memory-mb",
            str(memory_mb),
            "--yes",
            "-o",
            str(self.jobs_dir),
            "--job-name",
            job_name,
        ]
        return (*self.command_prefix, *args)

    async def __call__(
        self,
        execution: DurableSandboxExecution,
    ) -> dict[str, Any]:
        task_path = execution.claim.payload.get("task_path")
        if not isinstance(task_path, str) or not task_path:
            raise ValueError("durable Harbor payload requires task_path")
        task = HarborTask.from_path(Path(task_path))
        command = self.build_command(execution, task)
        job_name = self._job_name(execution)
        log_path = self.jobs_dir / f"{job_name}.log"
        started = time.perf_counter()
        env = os.environ.copy()
        env["LONGARC_JOB_ID"] = execution.job_id
        env["LONGARC_FENCING_TOKEN"] = str(execution.fencing_token)
        env["LONGARC_MEMORY_MB"] = str(execution.memory_limit_mb)
        process: asyncio.subprocess.Process | None = None
        with log_path.open("wb") as log_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.cwd) if self.cwd else None,
                    env=env,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                )
                returncode = await process.wait()
            except asyncio.CancelledError:
                if process is not None:
                    await self._terminate(process)
                await self._cleanup_job_containers(execution.job_id)
                raise
            except BaseException:
                if process is not None:
                    await self._terminate(process)
                await self._cleanup_job_containers(execution.job_id)
                raise

        result_path = self.jobs_dir / job_name / "result.json"
        parsed = self._parse_result(result_path)
        duration = time.perf_counter() - started
        summary = HarborRunSummary(
            returncode=returncode,
            reward=parsed["reward"],
            passed=parsed["passed"],
            total=parsed["total"],
            result_path=str(result_path) if result_path.is_file() else None,
            log_path=str(log_path),
            duration_sec=round(duration, 3),
            input_tokens=parsed["input_tokens"],
            output_tokens=parsed["output_tokens"],
            cache_tokens=parsed["cache_tokens"],
            error=parsed["error"],
        )
        if returncode != 0:
            await self._cleanup_job_containers(execution.job_id)
            text = log_path.read_text(encoding="utf-8", errors="replace")[-12_000:]
            if "429" in text or "rate limit" in text.lower():
                raise DurableRetryableError(
                    "Harbor model endpoint was rate limited",
                    retry_after_seconds=30.0,
                )
            raise RuntimeError(f"Harbor exited {returncode}; see {log_path}")
        if not result_path.is_file():
            await self._cleanup_job_containers(execution.job_id)
            raise RuntimeError(f"Harbor produced no result.json at {result_path}")
        if parsed["error"]:
            await self._cleanup_job_containers(execution.job_id)
            raise RuntimeError(
                f"Harbor trial exception: {parsed['error']}; see {result_path}"
            )
        return asdict(summary)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.terminate_grace_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _cleanup_job_containers(self, job_id: str) -> None:
        """Fail-closed cleanup using the label injected into Docker creation."""
        try:
            listed = await self._docker.command_runner(
                (
                    self._docker.docker_binary,
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=longarc.job={job_id}",
                ),
                timeout=self._docker.cleanup_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return
        if listed.returncode != 0:
            return
        for container_id in listed.stdout.splitlines():
            value = container_id.strip()
            if re.fullmatch(r"[a-f0-9]{12,64}", value):
                await self._docker.cleanup(value)

    async def managed_container_count(self) -> int:
        result = await self._docker.command_runner(
            (
                self._docker.docker_binary,
                "ps",
                "--filter",
                "label=longarc.managed=true",
                "--format",
                "{{.ID}}",
            ),
            timeout=self._docker.cleanup_timeout,
        )
        if result.returncode != 0:
            return 0
        return len(
            [
                line
                for line in result.stdout.splitlines()
                if re.fullmatch(r"[a-f0-9]{12,64}", line.strip())
            ]
        )

    @staticmethod
    def _parse_result(path: Path) -> dict[str, Any]:
        empty = {
            "reward": None,
            "passed": 0,
            "total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "error": None,
        }
        if not path.is_file():
            return empty
        payload = json.loads(path.read_text(encoding="utf-8"))
        stats = payload.get("stats") or {}
        evals = stats.get("evals") or {}
        means: list[float] = []
        passed = 0
        total = 0
        errors: list[str] = []
        for entry in evals.values():
            for metric in entry.get("metrics") or []:
                value = metric.get("mean")
                if isinstance(value, (int, float)):
                    means.append(float(value))
            reward_stats = entry.get("reward_stats") or {}
            reward_groups = reward_stats.get("reward", reward_stats)
            if not isinstance(reward_groups, dict):
                reward_groups = {}
            for reward, trial_names in reward_groups.items():
                count = len(trial_names) if isinstance(trial_names, list) else 0
                total += count
                try:
                    if float(reward) >= 1.0:
                        passed += count
                except (TypeError, ValueError):
                    pass
            for name, count in (entry.get("exception_stats") or {}).items():
                exception_count = len(count) if isinstance(count, list) else int(count)
                total += exception_count
                errors.append(f"{name}:{exception_count}")
        if total == 0 and isinstance(stats.get("n_total_trials"), int):
            total = int(stats["n_total_trials"])
        return {
            "reward": (
                sum(means) / len(means)
                if means
                else (passed / total if total else None)
            ),
            "passed": passed,
            "total": total,
            "input_tokens": int(stats.get("n_input_tokens") or 0),
            "output_tokens": int(stats.get("n_output_tokens") or 0),
            "cache_tokens": int(stats.get("n_cache_tokens") or 0),
            "error": "; ".join(errors) or None,
        }


async def run_durable_harbor(
    tasks: list[HarborTask],
    *,
    scheduler: DurableSandboxScheduler,
    runner: DurableHarborRunner,
    concurrency: int,
    memory_mb: int,
    timeout_seconds: float | None,
    tenant_id: str = "longarc-harbor",
    idempotency_prefix: str = "harbor",
    poll_seconds: float = 0.25,
    container_probe: Callable[[], Awaitable[int]] | None = None,
) -> dict[str, Any]:
    records: list[DurableJobRecord] = []
    for index, task in enumerate(tasks):
        digest = hashlib.sha256(
            f"{task.path}:{task.instruction_sha256}".encode()
        ).hexdigest()[:20]
        record = await asyncio.to_thread(
            scheduler.submit,
            DurableJobRequest(
                tenant_id=tenant_id,
                session_id=f"{task.name}-{index}-{digest}",
                idempotency_key=f"{idempotency_prefix}:{index}:{digest}",
                payload={"task_path": str(task.path)},
                resources=SandboxJobResources(memory_mb=memory_mb),
                timeout_seconds=timeout_seconds,
                max_attempts=1,
                priority=0,
            ),
        )
        records.append(record)

    stop = asyncio.Event()
    workers = [
        DurableSandboxWorker(scheduler, worker_id=f"longarc-worker-{index}")
        for index in range(concurrency)
    ]
    worker_tasks = [
        asyncio.create_task(worker.run_forever(runner, stop=stop))
        for worker in workers
    ]
    peak_active = 0
    peak_used_sandboxes = 0
    peak_used_memory_mb = 0
    peak_containers = 0
    container_samples = 0
    next_container_sample = 0.0
    try:
        while True:
            for worker_task in worker_tasks:
                if worker_task.done():
                    worker_task.result()
                    raise RuntimeError("durable Harbor worker stopped unexpectedly")
            current = await asyncio.to_thread(
                scheduler.get_jobs,
                [record.job_id for record in records],
            )
            stats = scheduler.stats()
            peak_active = max(peak_active, stats.active)
            peak_used_sandboxes = max(
                peak_used_sandboxes,
                stats.used_sandboxes,
            )
            peak_used_memory_mb = max(
                peak_used_memory_mb,
                stats.used_memory_mb,
            )
            now = time.monotonic()
            if container_probe is not None and now >= next_container_sample:
                count = await container_probe()
                peak_containers = max(peak_containers, count)
                container_samples += 1
                next_container_sample = now + 1.0
            if all(job is not None and job.state in TERMINAL_STATES for job in current):
                break
            await asyncio.sleep(poll_seconds)
    finally:
        stop.set()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    final_records = await asyncio.to_thread(
        scheduler.get_jobs,
        [record.job_id for record in records],
    )
    jobs = [record for record in final_records if record is not None]
    successful_results = [
        record.result
        for record in jobs
        if record.state == "succeeded" and isinstance(record.result, dict)
    ]
    counts = {
        state: sum(record.state == state for record in jobs)
        for state in TERMINAL_STATES
    }
    return {
        "jobs": [
            {
                "job_id": record.job_id,
                "session_id": record.session_id,
                "state": record.state,
                "attempt": record.attempt,
                "error": record.last_error,
                "result": record.result,
            }
            for record in jobs
        ],
        "counts": counts,
        "harbor_reward_sum": sum(
            float(result.get("reward") or 0.0) for result in successful_results
        ),
        "harbor_passed": sum(
            int(result.get("passed") or 0) for result in successful_results
        ),
        "harbor_trials": sum(
            int(result.get("total") or 0) for result in successful_results
        ),
        "input_tokens": sum(
            int(result.get("input_tokens") or 0)
            for result in successful_results
        ),
        "output_tokens": sum(
            int(result.get("output_tokens") or 0)
            for result in successful_results
        ),
        "cache_tokens": sum(
            int(result.get("cache_tokens") or 0)
            for result in successful_results
        ),
        "stats": asdict(scheduler.stats()),
        "peak_active": peak_active,
        "peak_used_sandboxes": peak_used_sandboxes,
        "peak_used_memory_mb": peak_used_memory_mb,
        "peak_containers": peak_containers,
        "container_samples": container_samples,
    }


def discover_tasks(
    task_paths: list[Path],
    *,
    task_root: Path | None,
    repeat_task: Path | None,
    repeat_count: int,
) -> list[HarborTask]:
    paths = [path for path in task_paths]
    if task_root is not None:
        paths.extend(
            sorted(
                path
                for path in task_root.iterdir()
                if path.is_dir() and (path / "task.toml").is_file()
            )
        )
    tasks = [HarborTask.from_path(path) for path in paths]
    if repeat_task is not None:
        if repeat_count <= 0:
            raise ValueError("repeat_count must be positive")
        repeated = HarborTask.from_path(repeat_task)
        tasks.extend(repeated for _ in range(repeat_count))
    if not tasks:
        raise ValueError("provide --task, --task-root, or --repeat-task")
    return tasks


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real Harbor Trials behind LongArc durable scheduling"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--harbor-binary", default="harbor")
    parser.add_argument("--model", required=True)
    parser.add_argument("--task", type=Path, action="append", default=[])
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--repeat-task", type=Path)
    parser.add_argument("--repeat-count", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--tenant", default="longarc-harbor")
    parser.add_argument("--idempotency-prefix", default="harbor")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if os.name != "nt" and str(args.db).startswith("/mnt/"):
        print(
            "warning: SQLite is on a WSL 9p mount; use a Linux ext4 path "
            "for multi-worker WAL workloads",
            file=sys.stderr,
        )
    tasks = discover_tasks(
        args.task,
        task_root=args.task_root,
        repeat_task=args.repeat_task,
        repeat_count=args.repeat_count,
    )
    limits = DurableSchedulerLimits(
        max_active_jobs=args.concurrency,
        max_pending_jobs=max(256, len(tasks) + args.concurrency),
        max_active_per_tenant=args.concurrency,
        max_pending_per_tenant=max(256, len(tasks) + args.concurrency),
        max_sandboxes=args.concurrency,
        max_memory_mb=max(512, args.memory_mb * args.concurrency),
        max_cpu_slots=args.concurrency,
        max_process_slots=args.concurrency,
        max_thread_slots=max(32, args.concurrency * 4),
        global_rpm=10_000,
        global_tpm=100_000_000,
        tenant_rpm=10_000,
        tenant_tpm=100_000_000,
        lease_ttl_seconds=45.0,
    )
    scheduler = DurableSandboxScheduler(args.db, limits)
    runner = DurableHarborRunner(
        harbor_binary=args.harbor_binary,
        jobs_dir=args.jobs_dir,
        env_file=args.env_file,
        model=args.model,
        cwd=args.cwd,
    )
    result = await run_durable_harbor(
        tasks,
        scheduler=scheduler,
        runner=runner,
        concurrency=args.concurrency,
        memory_mb=args.memory_mb,
        timeout_seconds=args.timeout_seconds,
        tenant_id=args.tenant,
        idempotency_prefix=args.idempotency_prefix,
        container_probe=runner.managed_container_count,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = asyncio.run(_main_async(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["counts"]["succeeded"] == len(result["jobs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DurableHarborRunner",
    "HarborRunSummary",
    "HarborTask",
    "discover_tasks",
    "run_durable_harbor",
]
