from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from my_agent_core.docker_executor import (
    CommandResult,
    DockerCleanupError,
    DockerMount,
    DockerOperationTimeout,
    DockerRunSpec,
    DockerSandboxExecutor,
)


class FakeExecution:
    job_id = "abcdef1234567890"
    fencing_token = 7
    memory_limit_mb = 512

    @asynccontextmanager
    async def lease(self, **_kwargs):
        yield self


class ScriptedRunner:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def __call__(self, argv: tuple[str, ...], *, timeout: float):
        self.calls.append((argv, timeout))
        result = self.handler(argv)
        if isinstance(result, BaseException):
            raise result
        return result


def _spec() -> DockerRunSpec:
    return DockerRunSpec(
        image="python:3.11-slim",
        command=("python", "-c", "print('ok')"),
        cpus=1.5,
        pids_limit=32,
        environment={"MODE": "test"},
        mounts=(
            DockerMount(
                source=Path(".").resolve(),
                target="/workspace",
            ),
        ),
        labels={"job": "abcdef1234567890"},
    )


def test_docker_create_args_apply_hard_cgroup_limits(tmp_path: Path) -> None:
    executor = DockerSandboxExecutor(
        command_runner=ScriptedRunner(lambda _argv: CommandResult((), 0)),
    )
    spec = DockerRunSpec(
        image="python:3.11-slim",
        command=("python", "-V"),
        cpus=2,
        pids_limit=48,
        environment={"MODE": "test"},
        mounts=(DockerMount(source=tmp_path, target="/workspace"),),
    )

    argv = executor.build_create_argv(
        FakeExecution(),
        spec,
        container_name="longarc-test-7",
    )

    assert ("--memory", "512m") == argv[argv.index("--memory") : argv.index("--memory") + 2]
    assert (
        "--memory-swap",
        "512m",
    ) == argv[argv.index("--memory-swap") : argv.index("--memory-swap") + 2]
    assert ("--cpus", "2") == argv[argv.index("--cpus") : argv.index("--cpus") + 2]
    assert (
        "--pids-limit",
        "48",
    ) == argv[argv.index("--pids-limit") : argv.index("--pids-limit") + 2]
    assert ("--network", "none") == argv[argv.index("--network") : argv.index("--network") + 2]
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "no-new-privileges" in argv
    assert any(part.startswith("type=bind,src=") for part in argv)


@pytest.mark.anyio
async def test_docker_create_retries_429_then_confirms_cleanup() -> None:
    create_attempts = 0

    def handler(argv):
        nonlocal create_attempts
        if "create" in argv:
            create_attempts += 1
            if create_attempts == 1:
                return CommandResult(argv, 125, stderr="429 Too Many Requests")
            return CommandResult(argv, 0, stdout="abcdef123456\n")
        if "start" in argv:
            return CommandResult(argv, 0, stdout="abcdef123456\n")
        if "wait" in argv:
            return CommandResult(argv, 0, stdout="0\n")
        if "logs" in argv:
            return CommandResult(argv, 0, stdout="ok\n")
        if "stop" in argv or "rm" in argv:
            return CommandResult(argv, 0)
        if "ps" in argv:
            return CommandResult(argv, 0, stdout="")
        raise AssertionError(argv)

    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    runner = ScriptedRunner(handler)
    executor = DockerSandboxExecutor(
        command_runner=runner,
        retry_base_seconds=0.25,
        sleeper=sleeper,
    )

    result = await executor.run(FakeExecution(), _spec())

    assert create_attempts == 2
    assert sleeps == [0.25]
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.cleanup_confirmed is True
    assert any("rm" in argv for argv, _ in runner.calls)
    assert any("ps" in argv for argv, _ in runner.calls)


@pytest.mark.anyio
async def test_docker_hang_attempts_stop_remove_and_verify() -> None:
    def handler(argv):
        if "create" in argv:
            return CommandResult(argv, 0, stdout="abcdef123457\n")
        if "start" in argv:
            return asyncio.TimeoutError()
        if "stop" in argv or "rm" in argv:
            return CommandResult(argv, 0)
        if "ps" in argv:
            return CommandResult(argv, 0, stdout="")
        raise AssertionError(argv)

    runner = ScriptedRunner(handler)
    executor = DockerSandboxExecutor(
        command_runner=runner,
        operation_timeout=0.2,
        cleanup_timeout=0.2,
    )

    with pytest.raises(DockerOperationTimeout, match="start"):
        await executor.run(FakeExecution(), _spec())

    commands = [argv for argv, _ in runner.calls]
    assert any("stop" in argv for argv in commands)
    assert any("rm" in argv for argv in commands)
    assert any("ps" in argv for argv in commands)


@pytest.mark.anyio
async def test_docker_cleanup_fails_closed_if_container_remains() -> None:
    def handler(argv):
        if "create" in argv:
            return CommandResult(argv, 0, stdout="abcdef123458\n")
        if "start" in argv:
            return CommandResult(argv, 0)
        if "wait" in argv:
            return CommandResult(argv, 0, stdout="1\n")
        if "logs" in argv:
            return CommandResult(argv, 1, stderr="failed\n")
        if "stop" in argv or "rm" in argv:
            return CommandResult(argv, 0)
        if "ps" in argv:
            return CommandResult(argv, 0, stdout="abcdef123458\n")
        raise AssertionError(argv)

    executor = DockerSandboxExecutor(
        command_runner=ScriptedRunner(handler),
    )

    with pytest.raises(DockerCleanupError, match="could not confirm cleanup"):
        await executor.run(FakeExecution(), _spec())


@pytest.mark.anyio
async def test_docker_cancellation_cleans_container() -> None:
    started = asyncio.Event()
    calls: list[tuple[str, ...]] = []

    class BlockingRunner:
        async def __call__(self, argv, *, timeout):
            del timeout
            calls.append(argv)
            if "create" in argv:
                return CommandResult(argv, 0, stdout="abcdef123459\n")
            if "start" in argv:
                started.set()
                await asyncio.Event().wait()
            if "stop" in argv or "rm" in argv:
                return CommandResult(argv, 0)
            if "ps" in argv:
                return CommandResult(argv, 0, stdout="")
            raise AssertionError(argv)

    executor = DockerSandboxExecutor(command_runner=BlockingRunner())
    task = asyncio.create_task(executor.run(FakeExecution(), _spec()))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any("stop" in argv for argv in calls)
    assert any("rm" in argv for argv in calls)
    assert any("ps" in argv for argv in calls)
