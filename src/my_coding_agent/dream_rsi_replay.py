"""Optional fixed-reference DREAM-RSI replay, with no online exploration.

Inputs must already contain scored discovery-tree nodes. A Session transcript is
not such a tree. Only the pinned upstream baseline is supported: Python process
isolation and a deadline are not a sandbox for arbitrary policy code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .dream_rsi_contract import (
    _integer,
    _number,
    _unique_object,
    validate_trace,
    effective_parameters,
    policy_identity,
    verify_leaf,
)

SOURCE_COMMIT = "8ec35bd2538220517ce1bfca4282db21b8c518db"
SOURCE_URL = "https://github.com/google-research/dream-rsi"
MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_NODES = 100_000
_WORKER = Path(__file__).with_name("_dream_rsi_worker.py")


def _worker_environment() -> dict[str, str]:
    # No HOME/USERPROFILE, provider keys, proxy auth, PYTHONPATH or credential files.
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PATH"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return result


def _unix_group_alive(pgid: int, *, timeout_seconds: float) -> bool:
    """Observe executable group members; a zombie cannot run or retain children.

    killpg() only queues a signal. In particular, reaping the root after its
    normal exit says nothing about whether a descendant has consumed SIGKILL.
    """
    if sys.platform.startswith("linux"):
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            raise RuntimeError("cannot inspect replay process group") from None
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                # comm may contain spaces and parentheses; fields after the last
                # ')' start with state, ppid, pgrp. No argv or environment read.
                fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in (b"Z", b"X"):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError):
                # Unrelated /proc entries can be hidden; inability to inspect an
                # entry belonging to this group must not become cleanup success.
                try:
                    if os.getpgid(int(entry.name)) == pgid:
                        raise RuntimeError("cannot inspect replay process group member") from None
                except ProcessLookupError:
                    pass
        return False
    # A portable, conservative fallback. An unavailable/malformed process table
    # fails closed instead of treating a successful kill() as exit confirmation.
    try:
        result = subprocess.run(
            ["ps", "-eo", "pgid=,stat="],
            env=_worker_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=min(1.0, timeout_seconds),
        )
        for line in result.stdout.splitlines():
            group, state = line.split()
            if int(group) == pgid and state[:1] not in (b"Z", b"X"):
                return True
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError("cannot verify replay process group cleanup on this platform") from None


def _stop_unix_group(process, *, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise RuntimeError("replay process group cleanup deadline exceeded") from None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("replay process group cleanup deadline exceeded")
        if not _unix_group_alive(process.pid, timeout_seconds=remaining):
            return
        time.sleep(min(0.01, remaining))


def _stop_tree(process):
    if os.name == "nt":
        if process.poll() is None:
            taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
            result = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if result.returncode and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("worker tree cleanup failed")
    else:
        _stop_unix_group(process)
        return
    process.wait(timeout=5)


class _WindowsJob:
    """Kill-on-close job; workers wait for assignment before importing the SDK."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user", ctypes.c_int64),
                ("kernel", ctypes.c_int64),
                ("period_user", ctypes.c_int64),
                ("period_kernel", ctypes.c_int64),
                ("faults", wintypes.DWORD),
                ("processes", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        self.ctypes, self.Accounting = ctypes, Accounting
        self.kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError("cannot create replay process containment")
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("cannot assign replay process containment")

    def close(self):
        if not self.handle:
            return
        handle, self.handle = self.handle, None
        try:
            if not self.kernel.TerminateJobObject(handle, 1):
                raise RuntimeError("cannot terminate replay process tree")
            deadline = time.monotonic() + 5
            while True:
                accounting = self.Accounting()
                if not self.kernel.QueryInformationJobObject(
                    handle, 1, self.ctypes.byref(accounting), self.ctypes.sizeof(accounting), None
                ):
                    raise RuntimeError("cannot verify replay process tree cleanup")
                if accounting.active == 0:
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("replay process tree cleanup deadline exceeded")
                time.sleep(0.01)
        finally:
            self.kernel.CloseHandle(handle)


def _run_worker(command, *, cwd: Path, timeout_seconds: float) -> int:
    """Run only our trusted worker; deadline plus at most 5s tree-cleanup allowance."""
    options = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=_worker_environment(),
        stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )
    job = None
    cleanup_started = False
    try:
        if os.name == "nt":
            job = _WindowsJob(process)
            if process.stdin is not None:
                try:
                    process.stdin.write(b"1")
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        cleanup_started = True
        if job is not None:
            job.close()
            process.wait(timeout=5)
        else:
            _stop_tree(process)
        raise TimeoutError("DREAM-RSI replay deadline exceeded") from None
    except BaseException:
        cleanup_started = True
        if job is not None:
            job.close()
            process.wait(timeout=5)
        else:
            _stop_tree(process)
        raise
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if job is not None:
            job.close()
        if os.name != "nt" and not cleanup_started:
            _stop_tree(process)


def _write_json(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def replay_trace(
    trace_path: str | Path,
    output: str | Path,
    *,
    budget: int,
    max_workers: int = 1,
    timeout_seconds: float = 30,
    parameters: dict | None = None,
    registry_path: str | Path | None = None,
    use_active: bool = True,
) -> dict:
    """Replay an already scored tree with the upstream fixed baseline, offline.

    Output is new-only, including on failure/timeout. The baseline has no beta
    control and this API does not expose one.
    """
    destination = Path(output).resolve()
    if destination.exists():
        raise FileExistsError("replay output already exists")
    if not _integer(budget, minimum=1) or budget > MAX_NODES:
        raise ValueError("budget must be a bounded positive integer")
    if not _integer(max_workers, minimum=1) or max_workers > MAX_NODES:
        raise ValueError("max_workers must be a bounded positive integer")
    if not _number(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    with Path(trace_path).open("rb") as stream:
        raw = stream.read(MAX_TRACE_BYTES + 1)
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError("trace exceeds byte limit")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    shape = validate_trace(value)
    policy_version = "parameters-v1:" + policy_identity(parameters)
    selection_reason = "explicit_parameters" if parameters is not None else "explicit_baseline"
    if parameters is None and use_active:
        from .dream_rsi_registry import PolicyRegistry

        if registry_path is None:
            from .settings import load_settings

            registry_path = getattr(getattr(load_settings(cwd=None), "dream_optimization", None), "registry_path", None)
        selected = PolicyRegistry(registry_path).status()
        parameters = selected["parameters"]
        policy_version = selected.get("version") or "parameters-v1:" + policy_identity(None)
        selection_reason = selected["reason"]
    effective_parameters(parameters, shape)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "input-trace.json").write_bytes(raw)
    _write_json(
        destination / "request.json",
        {
            "budget": budget,
            "max_workers": max_workers,
            "shape": shape,
            "source_commit": SOURCE_COMMIT,
            "parameters": parameters,
            "policy_version": policy_version,
            "selection_reason": selection_reason,
        },
    )
    try:
        code = _run_worker(
            [sys.executable, "-I", str(_WORKER), str(destination)], cwd=destination, timeout_seconds=timeout_seconds
        )
        if code:
            if code == 3:
                raise RuntimeError(
                    "pinned dream-rsi SDK is missing or its source/version is unverified; run uv sync --extra dream-rsi"
                )
            raise RuntimeError("DREAM-RSI replay worker failed")
        summary = json.loads((destination / "summary.json").read_bytes())
        if summary.get("input_sha256") != hashlib.sha256(raw).hexdigest() or summary.get("status") != "completed":
            raise RuntimeError("replay worker output identity mismatch")
        verify_leaf(
            value, summary, json.loads((destination / "decisions.json").read_bytes()), parameters, budget, max_workers
        )
        _write_json(destination / "status.json", {"status": "completed"})
    except BaseException as exc:
        _write_json(
            destination / "status.json",
            {"status": "timeout" if isinstance(exc, TimeoutError) else "failed", "error_type": type(exc).__name__},
        )
        raise
    files = []
    for path in sorted(destination.iterdir()):
        if path.is_file():
            body = path.read_bytes()
            files.append({"file": path.name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
    _write_json(destination / "manifest.json", {"schema_version": 1, "source_commit": SOURCE_COMMIT, "files": files})
    return summary


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "optimize":
        from .dream_rsi_optimizer import main as optimize_main

        return optimize_main(argv[1:])
    if argv and argv[0] in ("status", "rollback"):
        from .dream_rsi_registry import main as registry_main

        return registry_main(argv)
    if argv and argv[0] == "replay":
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Commands: optimize (bounded parameter search), replay, status, rollback. Legacy --trace arguments remain valid.",
    )
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--budget", required=True, type=int)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--registry", type=Path)
    parser.add_argument(
        "--baseline", action="store_true", help="ignore active policy and replay the full-shape reference"
    )
    args = parser.parse_args(argv)
    try:
        summary = replay_trace(
            args.trace,
            args.output,
            budget=args.budget,
            max_workers=args.max_workers,
            timeout_seconds=args.timeout_seconds,
            registry_path=args.registry,
            use_active=not args.baseline,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": summary["status"], "output": str(args.output), "result": summary["result"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
