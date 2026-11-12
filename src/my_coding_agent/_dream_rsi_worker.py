"""Trusted isolated worker for the pinned upstream fixed-reference policy only."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import socket
import sys

sys.dont_write_bytecode = True
SOURCE_COMMIT = "8ec35bd2538220517ce1bfca4282db21b8c518db"
SOURCE_URL = "https://github.com/google-research/dream-rsi"


def _deny_network(*args, **kwargs):
    raise RuntimeError("network is disabled for offline DREAM-RSI replay")


def main() -> int:
    if os.name == "nt" and sys.stdin.buffer.read(1) != b"1":
        return 1
    root = Path(sys.argv[1]).resolve()
    try:
        dist = metadata.distribution("dream-rsi")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if dist.version != "0.1.0" or direct.get("vcs_info", {}).get("commit_id") != SOURCE_COMMIT:
            return 3
        if direct.get("url", "").removesuffix(".git") != SOURCE_URL:
            return 3
    except (metadata.PackageNotFoundError, ValueError):
        return 3
    # This guards accidental transport use; it is not an arbitrary-code sandbox.
    socket.socket = _deny_network
    socket.create_connection = _deny_network
    from dream_rsi.env import TraceEnv
    from dream_rsi.policy.baseline import ParallelRefineMethod

    request = json.loads((root / "request.json").read_bytes())
    raw = (root / "input-trace.json").read_bytes()
    config = {"max_width": request["shape"]["branches"], "max_refine": request["shape"]["max_attempt"]}
    env = TraceEnv.replay(root / "input-trace.json", max_workers=request["max_workers"])
    result = ParallelRefineMethod(config).solve(env, budget=request["budget"])
    episode = env.execution_trace()
    upstream_files = []
    for filename in ("dream_rsi/env/trace_env.py", "dream_rsi/policy/baseline.py", "dream_rsi/env/trace/raw.py"):
        body = Path(dist.locate_file(filename)).read_bytes()
        upstream_files.append({"file": filename, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
    summary = {
        "schema_version": 1,
        "status": "completed",
        "mode": "offline_replay",
        "policy": "baseline",
        "policy_class": "ParallelRefineMethod",
        "policy_kind": "fixed_reference",
        "upstream": {
            "distribution": "dream-rsi",
            "version": dist.version,
            "commit": SOURCE_COMMIT,
            "repository": SOURCE_URL,
            "files": upstream_files,
        },
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_bytes": len(raw),
        "budget_limit": request["budget"],
        "max_workers": request["max_workers"],
        "result": {
            "best": result.best,
            "budget": result.budget,
            "decision_rounds": result.decision_rounds,
            "effective_sequential_rounds": result.effective_sequential_rounds,
            "batch_sizes": list(result.batch_sizes),
        },
        "limits": [
            "Only the upstream fixed baseline is supported; no adaptive policy parameter is exposed.",
            "OptimalPolicy is an unimplemented upstream template, not a learned policy.",
            "Replay uses recorded outcomes only and does not estimate unseen branches.",
            "Input score provenance is caller-owned; structural validation does not authenticate scores.",
            "No online exploration, model call, evaluator execution or automatic policy improvement.",
            "Credential environment removed; network blocked; process isolation is not a security sandbox.",
        ],
    }
    for filename, value in (("summary.json", summary), ("decisions.json", episode)):
        with (root / filename).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Parent reports a safe failure code; never echo trace bodies or SDK errors.
        raise SystemExit(1) from None
