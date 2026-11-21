from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from my_agent_core.durable_scheduler import (
    DurableSandboxScheduler,
    DurableSchedulerLimits,
)
from my_eval.durable_harbor import (
    DurableHarborRunner,
    HarborTask,
    run_durable_harbor,
)


def _task(tmp_path: Path, name: str) -> HarborTask:
    path = tmp_path / name
    path.mkdir()
    (path / "task.toml").write_text('schema_version = "1.4"\n', encoding="utf-8")
    (path / "instruction.md").write_text(f"task {name}\n", encoding="utf-8")
    return HarborTask.from_path(path)


def test_harbor_command_uses_real_agent_and_longarc_environment(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "live.env"
    env_file.write_text("DEFAULT_MODEL=deepseek/test\n", encoding="utf-8")
    runner = DurableHarborRunner(
        harbor_binary="harbor",
        jobs_dir=tmp_path / "jobs",
        env_file=env_file,
        model="deepseek/deepseek-v4.1-flash",
        command_prefix=("wsl.exe", "-d", "Ubuntu-20.04", "--"),
    )
    task = _task(tmp_path, "one")

    class Execution:
        job_id = "a" * 32
        fencing_token = 4
        memory_limit_mb = 768
        claim = type("Claim", (), {"payload": {"task_path": str(task.path)}})()

    command = runner.build_command(Execution(), task)  # type: ignore[arg-type]
    assert command[:4] == ("wsl.exe", "-d", "Ubuntu-20.04", "--")
    assert "my_eval.agent:MyAgentHarness" in command
    assert (
        "my_eval.harbor_environment:LongArcHarborEnvironment" in command
    )
    assert ("--override-memory-mb", "768") == command[
        command.index("--override-memory-mb") :
        command.index("--override-memory-mb") + 2
    ]
    assert ("-n", "1") == command[
        command.index("-n") : command.index("-n") + 2
    ]


def test_parse_harbor_result_reads_nested_reward_stats(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(
        """
{
  "stats": {
    "n_input_tokens": 120,
    "n_output_tokens": 30,
    "n_cache_tokens": 80,
    "evals": {
      "agent__model__task": {
        "metrics": [{"mean": 1.0}],
        "reward_stats": {"reward": {"1.0": ["trial-a", "trial-b"]}},
        "exception_stats": {}
      }
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    parsed = DurableHarborRunner._parse_result(path)

    assert parsed == {
        "reward": 1.0,
        "passed": 2,
        "total": 2,
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_tokens": 80,
        "error": None,
    }


@pytest.mark.anyio
async def test_campaign_runs_real_runner_contract_through_durable_workers(
    tmp_path: Path,
) -> None:
    scheduler = DurableSandboxScheduler(
        tmp_path / "jobs.sqlite",
        DurableSchedulerLimits(
            max_active_jobs=3,
            max_pending_jobs=32,
            max_active_per_tenant=3,
            max_pending_per_tenant=32,
            max_sandboxes=3,
            max_memory_mb=1536,
            max_cpu_slots=3,
            max_process_slots=3,
            max_thread_slots=16,
            global_rpm=1000,
            global_tpm=100000,
            tenant_rpm=1000,
            tenant_tpm=100000,
            lease_ttl_seconds=5.0,
        ),
    )
    active = 0
    peak = 0
    container_counts = iter((1, 3, 2))

    async def real_contract(execution):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return {
                "returncode": 0,
                "reward": 1.0,
                "passed": 1,
                "total": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_tokens": 5,
                "duration_sec": 0.02,
                "error": None,
            }
        finally:
            active -= 1

    tasks = [_task(tmp_path, f"task-{index}") for index in range(12)]

    async def probe() -> int:
        return next(container_counts, 0)

    result = await run_durable_harbor(
        tasks,
        scheduler=scheduler,
        runner=real_contract,  # type: ignore[arg-type]
        concurrency=3,
        memory_mb=256,
        timeout_seconds=5,
        poll_seconds=0.01,
        container_probe=probe,
    )

    assert result["counts"]["succeeded"] == 12
    assert result["harbor_passed"] == 12
    assert result["harbor_trials"] == 12
    assert result["input_tokens"] == 120
    assert peak <= 3
    assert result["peak_containers"] >= 1
    assert result["container_samples"] >= 1
    assert scheduler.stats().active == 0
    assert scheduler.stats().used_memory_mb == 0
