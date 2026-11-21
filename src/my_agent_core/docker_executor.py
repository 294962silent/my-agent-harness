"""Docker lifecycle executor with explicit CGroup limits and confirmed cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DockerMount:
    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class DockerRunSpec:
    image: str
    command: tuple[str, ...]
    cpus: float = 1.0
    pids_limit: int = 64
    read_only_root: bool = True
    network: str = "none"
    user: str = "65534:65534"
    workdir: str = "/workspace"
    environment: dict[str, str] = field(default_factory=dict)
    mounts: tuple[DockerMount, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("image must be a non-empty string")
        if not self.command or any(
            not isinstance(arg, str) for arg in self.command
        ):
            raise ValueError("command must be a non-empty tuple of strings")
        if (
            isinstance(self.cpus, bool)
            or not isinstance(self.cpus, (int, float))
            or not math.isfinite(self.cpus)
            or self.cpus <= 0
        ):
            raise ValueError("cpus must be a finite positive number")
        if type(self.pids_limit) is not int or self.pids_limit <= 0:
            raise ValueError("pids_limit must be a positive integer")
        if type(self.read_only_root) is not bool:
            raise ValueError("read_only_root must be a boolean")
        if self.network != "none" and not re.fullmatch(
            r"[A-Za-z0-9_.-]+",
            self.network,
        ):
            raise ValueError("network contains unsupported characters")
        if not self.workdir.startswith("/"):
            raise ValueError("workdir must be an absolute container path")
        for key, value in self.environment.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment key: {key!r}")
            if not isinstance(value, str):
                raise ValueError("environment values must be strings")
        for key, value in self.labels.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                raise ValueError(f"invalid label key: {key!r}")
            if not isinstance(value, str):
                raise ValueError("label values must be strings")
        for mount in self.mounts:
            if not mount.target.startswith("/"):
                raise ValueError("mount targets must be absolute container paths")


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class DockerRunResult:
    container_id: str
    exit_code: int
    stdout: str
    stderr: str
    cleanup_confirmed: bool


class DockerOperationTimeout(TimeoutError):
    def __init__(self, operation: str, timeout: float) -> None:
        self.operation = operation
        self.timeout = timeout
        super().__init__(f"Docker {operation} timed out after {timeout:g}s")


class DockerCleanupError(RuntimeError):
    def __init__(self, container_id: str, details: str) -> None:
        self.container_id = container_id
        self.details = details
        super().__init__(
            f"could not confirm cleanup for container {container_id!r}: "
            f"{details}"
        )


class CommandRunner(Protocol):
    async def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run one command and always reap the local CLI process on timeout."""

    async def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except asyncio.TimeoutError:
            await self._terminate(process)
            raise
        return CommandResult(
            argv=argv,
            returncode=int(process.returncode or 0),
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


@runtime_checkable
class SandboxExecutionHandle(Protocol):
    job_id: str
    fencing_token: int
    memory_limit_mb: int

    def lease(
        self,
        *,
        cpu_slots: int = 0,
        process_slots: int = 0,
        thread_slots: int = 0,
    ) -> Any: ...


class DockerSandboxExecutor:
    """Create/start/wait/remove one bounded Docker container."""

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        command_runner: CommandRunner | None = None,
        operation_timeout: float = 30.0,
        cleanup_timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_base_seconds: float = 0.2,
        sleeper: Any = asyncio.sleep,
    ) -> None:
        if not docker_binary:
            raise ValueError("docker_binary must be non-empty")
        self.docker_binary = docker_binary
        self.command_runner = command_runner or SubprocessCommandRunner()
        self.operation_timeout = self._positive(
            "operation_timeout",
            operation_timeout,
        )
        self.cleanup_timeout = self._positive(
            "cleanup_timeout",
            cleanup_timeout,
        )
        if type(retry_attempts) is not int or retry_attempts <= 0:
            raise ValueError("retry_attempts must be a positive integer")
        self.retry_attempts = retry_attempts
        self.retry_base_seconds = self._positive(
            "retry_base_seconds",
            retry_base_seconds,
        )
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")
        self._sleep = sleeper

    async def create_container(
        self,
        execution: SandboxExecutionHandle,
        spec: DockerRunSpec,
        *,
        container_name: str | None = None,
    ) -> str:
        """Create one long-lived container without waiting for it to exit.

        Harbor needs the same container for agent tools and verifier commands.
        This primitive keeps resource-limit construction in this executor while
        ``run`` remains the convenience path for one-shot containers.
        """
        resolved_name = container_name or self._container_name(execution)
        create_argv = self.build_create_argv(
            execution,
            spec,
            container_name=resolved_name,
        )
        created = await self._create_with_retry(create_argv)
        if created.returncode != 0:
            raise RuntimeError(
                "docker create failed: "
                f"{created.stderr.strip() or created.stdout.strip()}"
            )
        container_id = self._validate_container_id(created.stdout.strip())
        if not container_id:
            raise RuntimeError("docker create returned no container ID")
        return container_id

    async def start_container(self, container_id: str) -> CommandResult:
        container_id = self._validate_container_id(container_id)
        result = await self._run_docker(
            ("start", container_id),
            operation="start",
        )
        if result.returncode != 0:
            raise RuntimeError("docker start failed: " + result.stderr.strip())
        return result

    async def wait_container(self, container_id: str) -> int:
        container_id = self._validate_container_id(container_id)
        waited = await self._run_docker(
            ("wait", container_id),
            operation="wait",
        )
        if waited.returncode != 0:
            raise RuntimeError("docker wait failed: " + waited.stderr.strip())
        try:
            return int(waited.stdout.strip() or 0)
        except ValueError as exc:
            raise RuntimeError("docker wait returned a non-integer exit code") from exc

    async def container_logs(self, container_id: str) -> CommandResult:
        container_id = self._validate_container_id(container_id)
        return await self._run_docker(
            ("logs", container_id),
            operation="logs",
        )

    async def exec_container(
        self,
        container_id: str,
        command: tuple[str, ...],
        *,
        cwd: str | None = None,
        environment: dict[str, str] | None = None,
        user: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Execute a command in a live container with the executor's timeout."""
        container_id = self._validate_container_id(container_id)
        if not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("command must be a non-empty tuple of strings")
        args: list[str] = ["exec"]
        if cwd:
            args.extend(("-w", cwd))
        if user:
            args.extend(("-u", user))
        for key, value in sorted((environment or {}).items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment key: {key!r}")
            args.extend(("-e", f"{key}={value}"))
        args.append(container_id)
        args.extend(command)
        resolved_timeout = (
            self.operation_timeout if timeout is None else self._positive("timeout", timeout)
        )
        try:
            return await self.command_runner(
                (self.docker_binary, *args),
                timeout=resolved_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DockerOperationTimeout("exec", resolved_timeout) from exc

    async def copy_to_container(
        self,
        container_id: str,
        source: Path,
        target: str,
        *,
        contents_only: bool = False,
    ) -> CommandResult:
        container_id = self._validate_container_id(container_id)
        if not target.startswith("/"):
            raise ValueError("container target must be absolute")
        source_text = str(source.resolve())
        if contents_only:
            if not source.is_dir():
                raise ValueError("contents_only requires a directory source")
            source_text = source_text.rstrip("/") + "/."
        return await self._run_docker(
            ("cp", source_text, f"{container_id}:{target}"),
            operation="copy",
        )

    async def copy_from_container(
        self,
        container_id: str,
        source: str,
        target: Path,
    ) -> CommandResult:
        container_id = self._validate_container_id(container_id)
        if not source.startswith("/"):
            raise ValueError("container source must be absolute")
        target.parent.mkdir(parents=True, exist_ok=True)
        return await self._run_docker(
            ("cp", f"{container_id}:{source}", str(target.resolve())),
            operation="copy",
        )

    async def build_image(
        self,
        *,
        tag: str,
        context: Path,
        dockerfile: Path | None = None,
        build_args: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if not re.fullmatch(r"[A-Za-z0-9_.:/@-]+", tag):
            raise ValueError("invalid image tag")
        if not context.is_dir():
            raise ValueError("build context must be a directory")
        args: list[str] = ["build", "--tag", tag]
        if dockerfile is not None:
            args.extend(("--file", str(dockerfile.resolve())))
        for key, value in sorted((build_args or {}).items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid build argument key: {key!r}")
            args.extend(("--build-arg", f"{key}={value}"))
        args.append(str(context.resolve()))
        resolved_timeout = (
            self.operation_timeout if timeout is None else self._positive("timeout", timeout)
        )
        try:
            result = await self.command_runner(
                (self.docker_binary, *args),
                timeout=resolved_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DockerOperationTimeout("build", resolved_timeout) from exc
        if result.returncode != 0:
            raise RuntimeError(
                "docker build failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    async def run(
        self,
        execution: SandboxExecutionHandle,
        spec: DockerRunSpec,
    ) -> DockerRunResult:
        async with execution.lease(
            cpu_slots=1,
            process_slots=1,
            thread_slots=0,
        ):
            container_name = self._container_name(execution)
            create_argv = self.build_create_argv(
                execution,
                spec,
                container_name=container_name,
            )
            created = await self._create_with_retry(create_argv)
            if created.returncode != 0:
                raise RuntimeError(
                    "docker create failed: "
                    f"{created.stderr.strip() or created.stdout.strip()}"
                )
            container_id = self._validate_container_id(created.stdout.strip())
            if not container_id:
                raise RuntimeError("docker create returned no container ID")
            try:
                start = await self._run_docker(
                    ("start", container_id),
                    operation="start",
                )
                if start.returncode != 0:
                    raise RuntimeError(
                        "docker start failed: " + start.stderr.strip()
                    )
                waited = await self._run_docker(
                    ("wait", container_id),
                    operation="wait",
                )
                if waited.returncode != 0:
                    raise RuntimeError(
                        "docker wait failed: " + waited.stderr.strip()
                    )
                exit_code = int(waited.stdout.strip() or 0)
                logs = await self._run_docker(
                    ("logs", container_id),
                    operation="logs",
                )
                cleanup_confirmed = await self.cleanup(container_id)
                if not cleanup_confirmed:
                    raise DockerCleanupError(
                        container_id,
                        "container still appears after remove",
                    )
                return DockerRunResult(
                    container_id=container_id,
                    exit_code=exit_code,
                    stdout=logs.stdout,
                    stderr=logs.stderr,
                    cleanup_confirmed=True,
                )
            except BaseException as original:
                try:
                    cleanup_confirmed = await self.cleanup(container_id)
                except DockerCleanupError as cleanup_error:
                    raise cleanup_error from original
                if not cleanup_confirmed:
                    raise DockerCleanupError(
                        container_id,
                        "container still appears after remove",
                    ) from original
                raise

    def build_create_argv(
        self,
        execution: SandboxExecutionHandle,
        spec: DockerRunSpec,
        *,
        container_name: str,
    ) -> tuple[str, ...]:
        if execution.memory_limit_mb <= 0:
            raise ValueError("execution.memory_limit_mb must be positive")
        args = [
            self.docker_binary,
            "create",
            "--name",
            container_name,
            "--memory",
            f"{execution.memory_limit_mb}m",
            "--memory-swap",
            f"{execution.memory_limit_mb}m",
            "--cpus",
            f"{float(spec.cpus):g}",
            "--pids-limit",
            str(spec.pids_limit),
            "--network",
            spec.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            spec.user,
            "--workdir",
            spec.workdir,
        ]
        if spec.read_only_root:
            args.append("--read-only")
        for key, value in sorted(spec.environment.items()):
            args.extend(("--env", f"{key}={value}"))
        for mount in spec.mounts:
            mount_spec = (
                f"type=bind,src={mount.source.resolve()},dst={mount.target}"
            )
            if mount.read_only:
                mount_spec += ",readonly"
            args.extend(("--mount", mount_spec))
        for key, value in sorted(spec.labels.items()):
            args.extend(("--label", f"{key}={value}"))
        args.extend(("--", spec.image))
        args.extend(spec.command)
        return tuple(args)

    async def cleanup(self, container_id: str) -> bool:
        details: list[str] = []
        for operation, argv in (
            (
                "stop",
                (self.docker_binary, "stop", "--time", "1", container_id),
            ),
            ("remove", (self.docker_binary, "rm", "-f", container_id)),
        ):
            try:
                result = await self.command_runner(
                    argv,
                    timeout=self.cleanup_timeout,
                )
                if result.returncode != 0:
                    details.append(
                        f"{operation}: {result.stderr.strip() or result.returncode}"
                    )
            except (asyncio.TimeoutError, OSError) as exc:
                details.append(f"{operation}: {type(exc).__name__}: {exc}")

        try:
            remaining = await self.command_runner(
                (
                    self.docker_binary,
                    "ps",
                    "-a",
                    "-q",
                    "--filter",
                    f"id={container_id}",
                ),
                timeout=self.cleanup_timeout,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise DockerCleanupError(
                container_id,
                "; ".join(
                    [*details, f"verify: {type(exc).__name__}: {exc}"]
                ),
            ) from exc
        if remaining.returncode != 0:
            raise DockerCleanupError(
                container_id,
                "; ".join([*details, f"verify: {remaining.stderr.strip()}"]),
            )
        return not remaining.stdout.strip()

    async def _create_with_retry(
        self,
        argv: tuple[str, ...],
    ) -> CommandResult:
        last_result: CommandResult | None = None
        for attempt in range(1, self.retry_attempts + 1):
            result = await self.command_runner(
                argv,
                timeout=self.operation_timeout,
            )
            last_result = result
            if result.returncode == 0:
                return result
            retryable = (
                "429" in result.stderr
                or "toomanyrequests" in result.stderr.lower()
            )
            if not retryable or attempt >= self.retry_attempts:
                return result
            await self._sleep(self.retry_base_seconds * (2 ** (attempt - 1)))
        assert last_result is not None
        return last_result

    async def _run_docker(
        self,
        operation_args: tuple[str, ...],
        *,
        operation: str,
    ) -> CommandResult:
        argv = (self.docker_binary, *operation_args)
        try:
            return await self.command_runner(
                argv,
                timeout=self.operation_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DockerOperationTimeout(
                operation,
                self.operation_timeout,
            ) from exc

    @staticmethod
    def _container_name(execution: SandboxExecutionHandle) -> str:
        raw = f"longarc-{execution.job_id[:12]}-{execution.fencing_token}"
        return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)

    @staticmethod
    def _validate_container_id(value: str) -> str:
        if value and not re.fullmatch(r"[a-f0-9]{12,64}", value):
            raise RuntimeError("docker create returned an invalid container ID")
        return value

    @staticmethod
    def _positive(name: str, value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")
        return float(value)


__all__ = [
    "CommandResult",
    "CommandRunner",
    "DockerCleanupError",
    "DockerMount",
    "DockerOperationTimeout",
    "DockerRunResult",
    "DockerRunSpec",
    "DockerSandboxExecutor",
    "SandboxExecutionHandle",
    "SubprocessCommandRunner",
]
