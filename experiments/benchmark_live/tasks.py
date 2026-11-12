"""Official Harbor task input boundary; verifier/solution are never prompt input."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path


def agent_instruction(task: str | Path) -> str:
    return (Path(task) / "instruction.md").read_text(encoding="utf-8")


def task_fingerprint(task: str | Path) -> str:
    task = Path(task)
    digest = hashlib.sha256()
    for relative in ("instruction.md", "task.toml"):
        digest.update(relative.encode())
        digest.update((task / relative).read_bytes())
    environment = task / "environment"
    if environment.exists():
        for path in sorted(p for p in environment.rglob("*") if p.is_file()):
            digest.update(path.relative_to(task).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def oracle_gate(task: str | Path, result_path: str | Path) -> dict:
    task = Path(task)
    result_path = Path(result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    config = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    task_name = (config.get("task") or {}).get("name", task.name)
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if (
        result.get("task_name") != task_name
        or result.get("exception_info")
        or rewards.get("reward") != 1
        or (result.get("agent_info") or {}).get("name") != "oracle"
        or not result.get("task_checksum")
        or not result.get("finished_at")
    ):
        raise ValueError("official oracle trial did not pass for this task; no model calls allowed")
    return {
        "status": "oracle_passed",
        "task_name": task_name,
        "task_fingerprint": task_fingerprint(task),
        "oracle_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "oracle_result": str(result_path.resolve()),
        "harbor_task_checksum": result["task_checksum"],
    }


def parse_trial_result(result_path: str | Path) -> dict:
    """Keep infrastructure errors separate from official verifier scores."""
    path = Path(result_path)
    result = json.loads(path.read_text(encoding="utf-8"))
    agent = result.get("agent_result") or {}
    metadata = agent.get("metadata") or {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exception = result.get("exception_info")
    if exception:
        execution = result.get("agent_execution") or {}
        category = "environment_error" if not execution.get("started_at") else "execution_or_verifier_error"
    elif reward is None:
        category = "unscored"
    else:
        category = "scored"
    return {
        "task_name": result.get("task_name"),
        "category": category,
        "official_reward": reward if category == "scored" else None,
        "run_status": metadata.get("status"),
        "input_tokens": agent.get("n_input_tokens"),
        "output_tokens": agent.get("n_output_tokens"),
        "cache_tokens": agent.get("n_cache_tokens"),
        "cost_usd": agent.get("cost_usd"),
        "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
