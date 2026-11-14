"""Windows paid host to the sole owned WSL seccomp CUDA embedding process.

No credential, auth file, gold or provider client is passed to Linux. The native
Mem0 SDK, durable ledger and Langfuse owner stay in the Windows host environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading

from my_agent_core.memory_qwen import QwenEmbeddingClient
from my_agent_core._memory_qwen_worker import verify_model_files


def wsl_path(value):
    value = str(value).replace("\\", "/")
    if len(value) > 2 and value[1:3] == ":/" and value[0].isalpha():
        return "/mnt/" + value[0].lower() + "/" + value[3:]
    if value.startswith("/"):
        return value
    raise ValueError("absolute Windows or Linux path required")


def same_owned_process(expected, actual):
    return bool(
        actual
        and expected.get("worker") is True
        and actual.get("worker") is True
        and expected.get("pid") == actual.get("pid")
        and expected.get("start") == actual.get("start")
    )


class WindowsQwenClient(QwenEmbeddingClient):
    """Reuse the tested bounded vector wire protocol, with precise Linux teardown."""

    def __init__(self, config):
        if os.name != "nt":
            raise RuntimeError("Windows embedding launcher requires Windows host")
        if config["max_length"] != 4096 or config["batch_size"] != 1:
            raise ValueError("frozen embedding bounds differ")
        cuda_visible_devices = str(config.get("cuda_visible_devices", ""))
        if not cuda_visible_devices.isdigit():
            raise ValueError("an explicit nonnegative CUDA device index is required")
        verify_model_files(config["model_dir"])
        self.config, self.client = config, self
        self._lock, self._closed, self._broken = threading.RLock(), False, False
        self._sequence, self._sender, self._worker_handle = 0, None, None
        self._worker_exit_confirmed, self._owned = False, None
        self.worker = wsl_path(Path(__file__).with_name("qwen_seccomp_worker.py").resolve())
        env = {
            k: v
            for k, v in os.environ.items()
            if not re.search(
                r"API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|OPENAI|ANTHROPIC|DEEPSEEK|LIVE_BENCH|PROXY", k, re.I
            )
            and k not in ("PYTHONPATH", "PYTHONHOME", "WSLENV")
        }
        self.worker_environment_keys = tuple(sorted(env))
        command = [
            "wsl.exe",
            "-d",
            config.get("wsl_distribution", "Ubuntu-20.04"),
            "--exec",
            "/usr/bin/env",
            "-i",
            "HOME=/home/mll",
            "PATH=/usr/bin:/bin",
            "HF_HUB_OFFLINE=1",
            "TRANSFORMERS_OFFLINE=1",
            "HF_HUB_DISABLE_TELEMETRY=1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN=1",
            "MEM0_TELEMETRY=false",
            "DO_NOT_TRACK=1",
            "CUDA_VISIBLE_DEVICES=" + cuda_visible_devices,
            "TOKENIZERS_PARALLELISM=false",
            config["python_executable"],
            "-I",
            "-X",
            "utf8",
            "-u",
            self.worker,
            "--model-dir",
            wsl_path(config["model_dir"]),
            "--device",
            "cuda:0",
            "--batch-size",
            "1",
            "--max-length",
            "4096",
            "--ownership-handshake",
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self._responses = queue.Queue(maxsize=8)
        self._reader = threading.Thread(target=self._read, name="study-qwen-wsl-reader", daemon=True)
        self._reader.start()
        try:
            ownership = self._receive(30)
            if ownership.get("owned") is not True or type(ownership.get("pid")) is not int:
                raise RuntimeError("Linux worker ownership handshake missing")
            self._owned = {"pid": ownership["pid"], "start": str(ownership["start_ticks"]), "worker": True}
            ready = self._receive(config["startup_timeout"])
            if (
                ready.get("ready") is not True
                or ready.get("pid") != self._owned["pid"]
                or ready.get("dimensions") != 1024
                or ready.get("device") != "cuda:0"
                or ready.get("network_guard") != "linux_seccomp_no_non_unix_socket_or_connect"
            ):
                raise RuntimeError("required CUDA/seccomp readiness differs")
            self.status = {
                **ready,
                "profile": "local_qwen_gpu_wsl_stdio",
                "worker_pid": ready["pid"],
                "launcher_pid": self.pid,
                "credentials_in_worker": False,
                "cuda_visible_devices": cuda_visible_devices,
            }
        except BaseException:
            self.close()
            raise

    def _identity(self):
        if self._owned is None:
            return None
        script = (
            "import pathlib,json,sys; p=pathlib.Path('/proc')/sys.argv[1]; "
            "v=None if not p.exists() else {'pid':int(sys.argv[1]),'start':p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19],"
            "'worker':sys.argv[2] in p.joinpath('cmdline').read_bytes().decode().split(chr(0))}; print(json.dumps(v))"
        )
        result = subprocess.run(
            [
                "wsl.exe",
                "-d",
                self.config.get("wsl_distribution", "Ubuntu-20.04"),
                "--exec",
                "/usr/bin/python3",
                "-I",
                "-c",
                script,
                str(self._owned["pid"]),
                self.worker,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return json.loads(result.stdout)

    @property
    def process_exited(self):
        return self._process.poll() is not None and self._worker_exit_confirmed

    def close(self):
        if self._closed and self._worker_exit_confirmed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None and not self._process.stdin.closed:
                self._process.stdin.close()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                actual = self._identity()
                if not same_owned_process(self._owned or {}, actual):
                    raise RuntimeError("refusing to signal an unverified Linux process") from None
                subprocess.run(
                    [
                        "wsl.exe",
                        "-d",
                        self.config.get("wsl_distribution", "Ubuntu-20.04"),
                        "--exec",
                        "/bin/kill",
                        "-KILL",
                        str(self._owned["pid"]),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self._process.wait(timeout=10)
            actual = self._identity()
            if actual is not None and same_owned_process(self._owned or {}, actual):
                raise RuntimeError("owned Linux CUDA worker remains alive")
            self._worker_exit_confirmed = True
        finally:
            for thread in (self._sender, self._reader):
                if thread is not None and threading.current_thread() is not thread:
                    thread.join(timeout=2)
                    if thread.is_alive():
                        raise RuntimeError("owned embedding pipe thread did not terminate")
            if self._process.stdout is not None:
                self._process.stdout.close()
