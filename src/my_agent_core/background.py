"""后台异步任务执行器与孤儿进程防御调度引擎。"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from my_agent_core.env_filter import filter_env

if TYPE_CHECKING:
    from my_agent_core.message_queue import (  # pyright: ignore[reportMissingImports]
        MessageQueue,
    )


Owner = tuple[str, int]
CompletionCallback = Callable[[str, Owner | None], None]


@dataclass
class BackgroundJob:
    """单个后台作业状态。"""

    id: str
    description: str
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    result: str | None = None
    exit_code: int | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    started_at: float = field(default_factory=time.time)
    _owner: Owner | None = None
    _completion_callback: CompletionCallback | None = None


def _kill_process_tree(proc: asyncio.subprocess.Process | None) -> None:
    """递归杀死子进程及其整个子进程树，防止孤儿进程遗留。"""
    if proc is None or proc.returncode is not None or not proc.pid:
        return
    with contextlib.suppress(Exception):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


class BackgroundRunner:
    """管理后台异步作业生命周期并自动向 MessageQueue 投递完成通知。"""

    def __init__(self, message_queue: MessageQueue) -> None:
        self.message_queue = message_queue
        self.jobs: dict[str, BackgroundJob] = {}
        self._counter = 0
        self._capture_owner: Callable[[], Owner | None] | None = None
        self._on_complete: CompletionCallback | None = None
        atexit.register(self._sync_cleanup)

    def set_delivery_hooks(
        self,
        *,
        capture_owner: Callable[[], Owner | None] | None,
        on_complete: CompletionCallback | None,
    ) -> None:
        """设置新任务启动时冻结 owner 与完成投递回调的 hooks。"""
        self._capture_owner = capture_owner
        self._on_complete = on_complete

    def _sync_cleanup(self) -> None:
        """进程退出时强制清理所有存活的子进程树，杜绝孤儿进程。"""
        for job in self.jobs.values():
            if job.status == "running" and job.process and job.process.returncode is None:
                _kill_process_tree(job.process)

    async def run_process(self, command: str, cwd: Path | str, description: str = "") -> str:
        """异步启动操作系统后台子进程，立即返回 job_id。"""
        capture_owner = self._capture_owner
        on_complete = self._on_complete
        owner = capture_owner() if capture_owner is not None else None
        self._counter += 1
        job_id = f"bg_{self._counter:06x}"
        job = BackgroundJob(
            id=job_id,
            description=description or command,
            status="running",
            _owner=owner,
            _completion_callback=on_complete,
        )
        self.jobs[job_id] = job

        async def _worker() -> None:
            proc = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(cwd),
                    env=filter_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name != "nt",
                )
                job.process = proc
                stdout, stderr = await proc.communicate()
                output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
                job.exit_code = proc.returncode
                job.result = output[:20000] if output else "(no output)"
                job.status = "completed" if proc.returncode == 0 else "failed"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.result = "Background task cancelled"
                job.exit_code = -1
                if proc is not None:
                    _kill_process_tree(proc)
                return
            except Exception as e:
                job.status = "failed"
                job.result = str(e)
                job.exit_code = 1
            finally:
                if job.status == "cancelled" and proc is not None:
                    _kill_process_tree(proc)

            notification = (
                f'<task_notification id="{job.id}">\n'
                f"Background task {job.id} ({job.description}) {job.status} (exit code {job.exit_code}):\n"
                f"{job.result}\n"
                f"</task_notification>"
            )
            if job._completion_callback is None:
                self.message_queue.add_followup(notification)
            else:
                job._completion_callback(notification, job._owner)

        job.task = asyncio.create_task(_worker())
        return job_id

    async def cancel_all(self, job_ids: Sequence[str] | None = None) -> None:
        """取消指定的运行中任务；省略快照时保持原有 cancel-all 行为。"""
        ids = tuple(self.jobs) if job_ids is None else tuple(job_ids)
        for job_id in ids:
            job = self.jobs.get(job_id)
            if job is None:
                continue
            if job.status == "running":
                job.status = "cancelled"
                _kill_process_tree(job.process)
                if job.task and not job.task.done():
                    job.task.cancel()
                if job.process:
                    with contextlib.suppress(Exception):
                        await job.process.wait()
