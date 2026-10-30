"""沙盒：在隔离环境里跑一次 agent 任务。

两种后端：
- **docker**（默认）：每个任务一个容器，workspace 挂到 /workspace，harness 代码只读挂到 /harness；
  带 CPU / 内存 / 超时限制，跑完即删（除非显式保留现场）。
- **host**：本地临时目录 + 子进程。Docker 不可用时的退路，隔离性弱，仅用于冒烟。

设计要点：
1. **一次任务一个容器**，容器名带 task_id 与 attempt，便于失败后排查；
2. **超时在双层设防**：容器内 `timeout` 命令 + 外层 `subprocess` 超时；
3. **结果结构化**：退出码、stdout/stderr 尾部、耗时、容器状态，统一成 `SandboxResult`。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_IMAGE = "my-agent-rl:py311"
FALLBACK_IMAGE = "python:3.11-slim"

Backend = Literal["docker", "host"]


@dataclass
class SandboxResult:
    """一次沙盒执行的结果。"""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    workspace: Path | None = None
    container: str | None = None
    timed_out: bool = False
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def tail(self, n: int = 2000) -> str:
        text = (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")
        return text[-n:]


class SandboxError(RuntimeError):
    """沙盒不可用或启动失败。"""


def docker_available() -> bool:
    """探测 docker CLI 与 daemon 是否同时就绪。"""
    exe = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if not Path(exe).exists() and not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            [exe, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _docker_exe() -> str:
    found = shutil.which("docker")
    if found:
        return found
    candidate = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if Path(candidate).exists():
        return candidate
    raise SandboxError("找不到 docker 可执行文件")


def image_exists(image: str) -> bool:
    exe = _docker_exe()
    proc = subprocess.run([exe, "image", "inspect", image], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


class Sandbox:
    """一个任务的隔离执行环境。

    用法：
        with Sandbox(workspace, task_id="t1-003", timeout_s=300) as box:
            result = box.run(["python", "-m", "pytest", "-q"])
    """

    def __init__(
        self,
        workspace: Path | str,
        task_id: str = "task",
        attempt: int = 0,
        backend: Backend = "docker",
        image: str = DEFAULT_IMAGE,
        timeout_s: int = 300,
        cpus: float = 1.0,
        memory: str = "2g",
        keep: bool = False,
        env: dict[str, str] | None = None,
        network: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.task_id = task_id
        self.attempt = attempt
        self.backend = backend
        self.image = image
        self.timeout_s = timeout_s
        self.cpus = cpus
        self.memory = memory
        self.keep = keep
        self.env = dict(env or {})
        self.network = network
        self.container: str | None = None
        self._tmp_workspace: tempfile.TemporaryDirectory | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def __enter__(self) -> "Sandbox":
        if self.backend == "docker":
            self._start_container()
        else:
            self._prepare_host()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self.container and not self.keep:
            exe = _docker_exe()
            subprocess.run([exe, "rm", "-f", self.container], capture_output=True, text=True, timeout=120)
            self.container = None
        if self._tmp_workspace is not None:
            self._tmp_workspace.cleanup()
            self._tmp_workspace = None

    # ── 后端实现 ──────────────────────────────────────────────────────────

    def _start_container(self) -> None:
        if not docker_available():
            raise SandboxError("docker daemon 不可用；改用 backend='host' 或先启动 Docker Desktop")
        exe = _docker_exe()
        image = self.image if image_exists(self.image) else FALLBACK_IMAGE
        # 容器名只允许 [a-zA-Z0-9_.-]：把任务 id 里的 @ / 空格等一律替换掉
        safe_task = "".join(ch if (ch.isalnum() or ch in "_.-") else "-" for ch in self.task_id)
        name = f"arl-{safe_task}-{self.attempt}-{uuid.uuid4().hex[:6]}".lower()[:60]

        args = [
            exe,
            "run",
            "-d",
            "--name",
            name,
            "--cpus",
            str(self.cpus),
            "--memory",
            self.memory,
            "-w",
            "/workspace",
            "-v",
            f"{self.workspace}:/workspace",
            "--label",
            f"arl.task={self.task_id}",
        ]
        if not self.network:
            args += ["--network", "none"]
        for key, value in self.env.items():
            args += ["-e", f"{key}={value}"]
        args += [image, "sleep", "infinity"]

        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise SandboxError(f"容器启动失败: {proc.stderr.strip()[:400]}")
        self.container = name

    def _prepare_host(self) -> None:
        """host 后端：把 workspace 复制到临时目录，避免污染原件。"""
        self._tmp_workspace = tempfile.TemporaryDirectory(prefix=f"arl-{self.task_id}-")
        target = Path(self._tmp_workspace.name) / "workspace"
        shutil.copytree(self.workspace, target, dirs_exist_ok=True)
        self._host_cwd = target

    def _run_docker(self, cmd: list[str]) -> SandboxResult:
        exe = _docker_exe()
        assert self.container
        wrapped = ["timeout", str(self.timeout_s), *cmd]
        started = time.time()
        try:
            proc = subprocess.run(
                [exe, "exec", self.container, *wrapped],
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 30,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.time() - started
            timed_out = proc.returncode in (124, 137)
            return SandboxResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=elapsed,
                workspace=self.workspace,
                container=self.container,
                timed_out=timed_out,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr="外层超时",
                duration_s=time.time() - started,
                workspace=self.workspace,
                container=self.container,
                timed_out=True,
            )

    def _run_host(self, cmd: list[str]) -> SandboxResult:
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self._host_cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                encoding="utf-8",
                errors="replace",
                env={**dict(**{k: v for k, v in __import__("os").environ.items()}), **self.env},
            )
            return SandboxResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=time.time() - started,
                workspace=self._host_cwd,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr="超时",
                duration_s=time.time() - started,
                workspace=self._host_cwd,
                timed_out=True,
            )

    # ── 对外接口 ──────────────────────────────────────────────────────────

    def run(self, cmd: list[str] | str) -> SandboxResult:
        """在沙盒里执行一条命令。字符串会按 shell 方式执行。"""
        argv = ["sh", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
        if self.backend == "docker":
            return self._run_docker(argv)
        return self._run_host(argv)

    def read(self, relpath: str) -> str:
        """读取 workspace 内文件（容器通过 docker exec cat）。"""
        target = self.workspace / relpath if self.backend == "docker" else self._host_cwd / relpath  # type: ignore[attr-defined]
        return target.read_text(encoding="utf-8", errors="replace")

    def write(self, relpath: str, content: str) -> None:
        base = self.workspace if self.backend == "docker" else self._host_cwd  # type: ignore[attr-defined]
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
