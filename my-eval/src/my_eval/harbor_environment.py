"""Harbor Docker environment backed by LongArc's bounded container executor.

Harbor owns Trial lifecycle and verification. This adapter keeps the concrete
container lifecycle behind :class:`DockerSandboxExecutor`, so Harbor jobs use
LongArc's resource limits, cleanup checks, and cancellation behavior instead of
silently exercising a separate test-only runner.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import require_agent_environment_definition
from harbor.models.task.config import NetworkMode, TaskOS

from my_agent_core.docker_executor import (
    DockerMount,
    DockerOperationTimeout,
    DockerRunSpec,
    DockerSandboxExecutor,
)


@dataclass(frozen=True, slots=True)
class _ContainerHandle:
    job_id: str
    fencing_token: int
    memory_limit_mb: int


def _mount_value(mount: Any, name: str, default: Any = None) -> Any:
    if isinstance(mount, dict):
        return mount.get(name, default)
    return getattr(mount, name, default)


class LongArcHarborEnvironment(BaseEnvironment):
    """Single-container Harbor environment using LongArc's Docker executor."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._container_id: str | None = None
        self._image: str | None = None
        self._executor = DockerSandboxExecutor(
            docker_binary=os.environ.get("LONGARC_DOCKER_BINARY", "docker"),
            operation_timeout=float(
                os.environ.get("LONGARC_DOCKER_OPERATION_TIMEOUT", "120")
            ),
            cleanup_timeout=float(
                os.environ.get("LONGARC_DOCKER_CLEANUP_TIMEOUT", "20")
            ),
        )
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "longarc-docker"

    @classmethod
    def preflight(cls) -> None:
        binary = os.environ.get("LONGARC_DOCKER_BINARY", "docker")
        try:
            result = subprocess.run(
                [binary, "info"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"LongArc Docker preflight failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise SystemExit(f"LongArc Docker preflight failed: {detail}")

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            stream=False,
            disable_internet=True,
            mounted=bool(getattr(self, "_mounts", ())),
            windows=False,
            docker_compose=False,
            gpus=False,
        )

    def _validate_definition(self) -> None:
        if self.task_env_config.os != TaskOS.LINUX:
            raise ValueError("LongArcHarborEnvironment supports Linux containers only")
        if self.extra_docker_compose_paths or (
            self.environment_dir / "docker-compose.yaml"
        ).exists():
            raise ValueError(
                "LongArcHarborEnvironment supports one bounded container per Trial; "
                "compose tasks must use Harbor's Docker environment"
            )
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )

    @property
    def _workdir(self) -> str:
        return self.task_env_config.workdir or "/"

    @property
    def _container_name(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", self.session_id)[:48]
        digest = hashlib.sha256(self.session_id.encode()).hexdigest()[:10]
        return f"longarc-harbor-{safe}-{digest}"

    def _container_handle(self) -> _ContainerHandle:
        return _ContainerHandle(
            job_id=self.session_id,
            fencing_token=1,
            memory_limit_mb=int(self._effective_memory_mb or 512),
        )

    def _mounts_for_container(self) -> tuple[DockerMount, ...]:
        mounts: list[DockerMount] = []
        for mount in self._mounts:
            source = _mount_value(mount, "source")
            target = _mount_value(mount, "target")
            if not source or not target:
                continue
            source_path = Path(str(source))
            if not source_path.exists():
                continue
            read_only = bool(
                _mount_value(
                    mount,
                    "read_only",
                    _mount_value(mount, "readOnly", False),
                )
            )
            mounts.append(
                DockerMount(
                    source=source_path,
                    target=str(target),
                    read_only=read_only,
                )
            )
        return tuple(mounts)

    async def _resolve_image(self, force_build: bool) -> str:
        docker_image = self.task_env_config.docker_image
        dockerfile = self.environment_dir / "Dockerfile"
        if docker_image and not force_build:
            return docker_image
        if docker_image and not dockerfile.exists():
            return docker_image
        if not dockerfile.exists():
            raise FileNotFoundError(
                f"no docker_image or Dockerfile for {self.environment_dir}"
            )
        digest = hashlib.sha256(
            (
                str(self.environment_dir.resolve())
                + ":"
                + str(dockerfile.stat().st_mtime_ns)
            ).encode()
        ).hexdigest()[:16]
        tag = f"longarc-harbor:{digest}"
        await self._executor.build_image(
            tag=tag,
            context=self.environment_dir,
            dockerfile=dockerfile,
            timeout=float(self.task_env_config.build_timeout_sec),
        )
        return tag

    def _network_name(self) -> str:
        if self._network_policy.network_mode == NetworkMode.NO_NETWORK:
            return "none"
        if self._network_policy.network_mode == NetworkMode.PUBLIC:
            return "bridge"
        raise ValueError(
            "LongArcHarborEnvironment does not implement allowlist networking; "
            "use Harbor Docker or choose network_mode=no-network/public"
        )

    async def _ensure_container_dir(self, path: str) -> None:
        assert self._container_id is not None
        result = await self._executor.exec_container(
            self._container_id,
            ("mkdir", "-p", path),
            user="root",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"could not create container directory {path!r}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def start(self, force_build: bool) -> None:
        image = await self._resolve_image(force_build)
        self._image = image
        spec = DockerRunSpec(
            image=image,
            command=("tail", "-f", "/dev/null"),
            cpus=float(self._effective_cpus or 1),
            pids_limit=64,
            read_only_root=False,
            network=self._network_name(),
            user="0:0",
            workdir="/",
            environment=self._startup_env(),
            mounts=self._mounts_for_container(),
            labels={
                "longarc.managed": "true",
                "longarc.environment": self.environment_name,
                "longarc.job": os.environ.get("LONGARC_JOB_ID", ""),
                "longarc.fencing_token": os.environ.get(
                    "LONGARC_FENCING_TOKEN",
                    "unknown",
                ),
            },
        )
        self._container_id = await self._executor.create_container(
            self._container_handle(),
            spec,
            container_name=self._container_name,
        )
        await self._executor.start_container(self._container_id)
        await self._ensure_container_dir(self._workdir)
        for target in self._mount_targets(writable_only=True):
            await self._ensure_container_dir(target)
        await self._upload_environment_dir_after_start()

    async def stop(self, delete: bool) -> None:
        container_id = self._container_id
        self._container_id = None
        if container_id is None:
            return
        if not delete:
            result = await self._executor.command_runner(
                (self._executor.docker_binary, "stop", "--time", "1", container_id),
                timeout=self._executor.cleanup_timeout,
            )
            if result.returncode != 0:
                raise RuntimeError("docker stop failed: " + result.stderr.strip())
            return
        cleanup_confirmed = await self._executor.cleanup(container_id)
        if not cleanup_confirmed:
            raise RuntimeError(
                f"container {container_id!r} remained after stop/remove"
            )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        assert self._container_id is not None
        target = Path(target_path)
        if not target_path.endswith("/"):
            await self._ensure_container_dir(target.parent.as_posix())
        result = await self._executor.copy_to_container(
            self._container_id,
            Path(source_path),
            target_path,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "docker cp upload failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        assert self._container_id is not None
        await self._ensure_container_dir(target_dir)
        source = Path(source_dir) / "."
        result = await self._executor.copy_to_container(
            self._container_id,
            source,
            target_dir,
            contents_only=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "docker cp directory upload failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        assert self._container_id is not None
        result = await self._executor.copy_from_container(
            self._container_id,
            source_path,
            Path(target_path),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "docker cp download failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        assert self._container_id is not None
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        result = await self._executor.copy_from_container(
            self._container_id,
            source_dir.rstrip("/") + "/.",
            target,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "docker cp directory download failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        assert self._container_id is not None
        resolved_user = self._resolve_user(user)
        user_text = None if resolved_user is None else str(resolved_user)
        try:
            result = await self._executor.exec_container(
                self._container_id,
                ("sh", "-lc", command),
                cwd=cwd or self._workdir,
                environment=self._merge_env(env),
                user=user_text,
                timeout=float(timeout_sec) if timeout_sec is not None else None,
            )
        except (asyncio.TimeoutError, DockerOperationTimeout):
            await self.stop(delete=True)
            raise
        callback = self._output_callback()
        if callback is not None:
            if result.stdout:
                await callback(result.stdout, "stdout")
            if result.stderr:
                await callback(result.stderr, "stderr")
        return ExecResult(
            stdout=result.stdout or None,
            stderr=result.stderr or None,
            return_code=result.returncode,
        )


__all__ = ["LongArcHarborEnvironment"]
