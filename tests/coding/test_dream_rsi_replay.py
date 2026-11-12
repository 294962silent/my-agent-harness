"""Offline replay contracts, not a task-quality or model benchmark."""

import copy
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest


def scored_tree():
    def score(value):
        return {"evaluated": True, "combined_score": value, "n_valid": 1, "n_total": 1}

    return {
        "version": 1,
        "workspace": "synthetic-scored-contract",
        "exported_at": "2026-11-12T00:00:00+00:00",
        "n_problems": 1,
        "baseline": score(1.0),
        "nodes": [
            {
                "id": "b0_a0",
                "branch": 0,
                "attempt": 0,
                "parent_id": None,
                "intent": "",
                "tags": [],
                "path": "not-accessed/b0_a0",
                "status": "done",
                "raw": score(2.0),
            },
            {
                "id": "b0_a1",
                "branch": 0,
                "attempt": 1,
                "parent_id": "b0_a0",
                "intent": "",
                "tags": [],
                "path": "not-accessed/b0_a1",
                "status": "done",
                "raw": score(3.0),
            },
            {
                "id": "b1_a0",
                "branch": 1,
                "attempt": 0,
                "parent_id": None,
                "intent": "",
                "tags": [],
                "path": "not-accessed/b1_a0",
                "status": "done",
                "raw": score(1.5),
            },
        ],
    }


def test_accepts_scored_upstream_v1_tree_without_reading_node_paths():
    from my_coding_agent.dream_rsi_replay import validate_trace

    value = scored_tree()
    assert validate_trace(value) == {"nodes": 3, "branches": 2, "max_attempt": 1}
    assert value == scored_tree()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(version=2),
        lambda x: x["baseline"].update(evaluated=False),
        lambda x: x["nodes"][0]["raw"].update(evaluated=False),
        lambda x: x["nodes"][0]["raw"].update(combined_score=None),
        lambda x: x["nodes"][0]["raw"].update(combined_score=float("nan")),
        lambda x: x["nodes"][0]["raw"].update(combined_score=True),
        lambda x: x["nodes"][0]["raw"].update(combined_score=10**500),
        lambda x: x["nodes"][1].update(parent_id="b1_a0"),
        lambda x: x["nodes"][1].update(parent_id="b0_a1"),
        lambda x: x["nodes"].append(copy.deepcopy(x["nodes"][0])),
        lambda x: x["nodes"][0].update(status="pending"),
        lambda x: x["nodes"][0].update(branch=-1),
    ],
)
def test_rejects_unscored_or_malformed_tree(mutation):
    from my_coding_agent.dream_rsi_replay import validate_trace

    value = scored_tree()
    mutation(value)
    with pytest.raises(ValueError):
        validate_trace(value)


def test_bad_input_and_existing_output_never_launch_worker(tmp_path, monkeypatch):
    from my_coding_agent import dream_rsi_replay as adapter

    def forbidden(*args, **kwargs):
        raise AssertionError("worker launched for invalid input")

    monkeypatch.setattr(adapter, "_run_worker", forbidden)
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(scored_tree()), encoding="utf-8")
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        adapter.replay_trace(source, output, budget=2)
    source.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.replay_trace(source, tmp_path / "never-created", budget=2)
    assert not (tmp_path / "never-created").exists()


def test_worker_environment_does_not_inherit_credentials_home_or_pythonpath(monkeypatch):
    from my_coding_agent.dream_rsi_replay import _worker_environment

    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "PRIVATE")
    monkeypatch.setenv("PYTHONPATH", "PRIVATE")
    monkeypatch.setenv("HOME", "PRIVATE")
    env = _worker_environment()
    assert "PRIVATE" not in repr(env)
    assert "HOME" not in env and "USERPROFILE" not in env


def test_timeout_stops_started_worker_and_descendant(tmp_path, monkeypatch):
    from my_coding_agent import dream_rsi_replay as adapter
    import time

    # Start the short synthetic deadline only after its PID marker is visible;
    # this tests cleanup, not how fast the host can start an interpreter.
    script = tmp_path / "blocking.py"
    script.write_text(
        "import subprocess,sys,time,pathlib\n"
        "if sys.platform=='win32': sys.stdin.buffer.read(1)\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    pid_path = tmp_path / "child.pid"
    real_popen = adapter.subprocess.Popen

    def controlled_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        real_wait = process.wait
        first_wait = True

        def wait(timeout=None):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                deadline = time.monotonic() + 15
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert pid_path.exists(), "controlled worker did not reach its ready marker"
                return real_wait(timeout=0.05)
            return real_wait(timeout=timeout)

        process.wait = wait
        return process

    monkeypatch.setattr(adapter.subprocess, "Popen", controlled_popen)
    with pytest.raises(TimeoutError):
        adapter._run_worker([sys.executable, "-I", str(script)], cwd=tmp_path, timeout_seconds=15)
    assert pid_path.exists()
    pid = int(pid_path.read_text())
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            kernel.CloseHandle(ctypes.c_void_p(handle))
            assert code.value != 259
    else:
        # A killed child may briefly be a zombie while init reaps it.
        proc = Path(f"/proc/{pid}/stat")
        assert not proc.exists() or proc.read_text().split()[2] == "Z"


def test_worker_normal_exit_also_cleans_descendants(tmp_path):
    from my_coding_agent.dream_rsi_replay import _run_worker

    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess,sys,pathlib\n"
        "if sys.platform=='win32': sys.stdin.buffer.read(1)\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    assert _run_worker([sys.executable, "-I", str(script)], cwd=tmp_path, timeout_seconds=10) == 0
    pid = int((tmp_path / "child.pid").read_text())
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        try:
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                assert code.value != 259
        finally:
            if handle:
                kernel.CloseHandle(ctypes.c_void_p(handle))
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        proc = Path(f"/proc/{pid}/stat")
        assert not proc.exists() or proc.read_text().split()[2] == "Z"
