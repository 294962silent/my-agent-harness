"""Frozen task, arm and run contracts for quality comparisons."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
from typing import Any

PROTOCOL_ID = "long-horizon-quality-v1"
BENCHMARK_ID = "terminal-bench-2.1"
TERMINAL_BENCH_REVISION = "12b7565679ce5006230b820666353673a133db93"
CONFIRMATORY_TASK_COUNT = 89
DEFAULT_ARMS = ("react", "mea")
DEFAULT_TRIALS = 3
DEFAULT_SEED = 20261118


class ProtocolError(ValueError):
    """The frozen experiment contract is invalid or has been changed."""


@dataclass(frozen=True)
class ArmSpec:
    name: str
    execution: str
    manager: bool
    auditor: bool
    fresh_sessions: bool
    checkpoint_resume: bool
    immutable_paths: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "execution": self.execution,
            "manager": self.manager,
            "auditor": self.auditor,
            "fresh_sessions": self.fresh_sessions,
            "checkpoint_resume": self.checkpoint_resume,
            "immutable_paths": self.immutable_paths,
        }


ARM_SPECS = {
    "react": ArmSpec(
        name="react",
        execution="single_react",
        manager=False,
        auditor=False,
        fresh_sessions=False,
        checkpoint_resume=False,
        immutable_paths=False,
    ),
    "mea": ArmSpec(
        name="mea",
        execution="manager_executor_auditor",
        manager=True,
        auditor=True,
        fresh_sessions=True,
        checkpoint_resume=True,
        immutable_paths=True,
    ),
}


@dataclass(frozen=True)
class TrialSlot:
    task_id: str
    arm: str
    trial: int
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "trial": self.trial,
            "run_id": self.run_id,
        }


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_text(value: object, name: str, *, limit: int = 160) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be text")
    text = value.strip()
    if (
        not text
        or len(text) > limit
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        raise ProtocolError(f"{name} is not safe text")
    return text


def _normalize_tasks(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ProtocolError("task manifest tasks must be a nonempty list")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            task_id = _safe_text(item, f"tasks[{index}]")
            cohort = "unspecified"
        elif isinstance(item, Mapping):
            task_id = _safe_text(item.get("id"), f"tasks[{index}].id")
            cohort = _safe_text(
                item.get("cohort", "unspecified"),
                f"tasks[{index}].cohort",
            )
        else:
            raise ProtocolError(f"tasks[{index}] must be text or an object")
        if task_id in seen:
            raise ProtocolError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        tasks.append({"id": task_id, "cohort": cohort})
    return tasks


def _load_task_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProtocolError("task manifest is unreadable") from exc
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("task manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ProtocolError("unsupported task manifest")
    if value.get("benchmark") != BENCHMARK_ID:
        raise ProtocolError(f"benchmark must be {BENCHMARK_ID}")
    if value.get("revision") != TERMINAL_BENCH_REVISION:
        raise ProtocolError("Terminal-Bench revision differs from the frozen revision")
    tasks = _normalize_tasks(value.get("tasks"))
    normalized = {
        "schema_version": 1,
        "benchmark": BENCHMARK_ID,
        "revision": TERMINAL_BENCH_REVISION,
        "tasks": tasks,
    }
    return normalized, payload


def run_id_for(
    campaign_id: str,
    task_id: str,
    arm: str,
    trial: int,
) -> str:
    identity = {
        "protocol": PROTOCOL_ID,
        "campaign_id": campaign_id,
        "task_id": task_id,
        "arm": arm,
        "trial": trial,
    }
    return "lhq-" + digest(identity)[:32]


def build_slots(
    task_ids: Sequence[str],
    *,
    campaign_id: str,
    arms: Sequence[str] = DEFAULT_ARMS,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
) -> list[TrialSlot]:
    campaign = _safe_text(campaign_id, "campaign_id")
    if type(trials) is not int or trials < 3:
        raise ProtocolError("at least three trials per task and arm are required")
    if type(seed) is not int:
        raise ProtocolError("seed must be an integer")
    arm_names = tuple(dict.fromkeys(arms))
    if len(arm_names) != len(arms) or not arm_names:
        raise ProtocolError("arms must be unique and nonempty")
    if set(arm_names) != set(DEFAULT_ARMS):
        raise ProtocolError("quality comparison requires exactly react and mea arms")
    normalized_tasks = [_safe_text(task, "task_id") for task in task_ids]
    if not normalized_tasks or len(set(normalized_tasks)) != len(normalized_tasks):
        raise ProtocolError("task ids must be nonempty and unique")
    rng = random.Random(seed)
    task_order = list(normalized_tasks)
    rng.shuffle(task_order)
    slots: list[TrialSlot] = []
    for task_id in task_order:
        arm_order = list(arm_names)
        rng.shuffle(arm_order)
        for arm in arm_order:
            for trial in range(trials):
                slots.append(
                    TrialSlot(
                        task_id=task_id,
                        arm=arm,
                        trial=trial,
                        run_id=run_id_for(campaign, task_id, arm, trial),
                    )
                )
    if len({slot.run_id for slot in slots}) != len(slots):
        raise ProtocolError("run id collision")
    return slots


def _write_new(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def freeze_campaign(
    *,
    task_manifest: str | Path,
    output: str | Path,
    campaign_id: str,
    stage: str,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    manifest_path = Path(task_manifest).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    manifest, manifest_payload = _load_task_manifest(manifest_path)
    tasks = manifest["tasks"]
    if stage == "pilot":
        if not 1 <= len(tasks) <= 12:
            raise ProtocolError("pilot requires one to twelve tasks")
        claim_ceiling = "descriptive_only"
    elif stage == "confirmatory":
        if len(tasks) != CONFIRMATORY_TASK_COUNT:
            raise ProtocolError(
                f"confirmatory stage requires all {CONFIRMATORY_TASK_COUNT} tasks"
            )
        claim_ceiling = "quality_and_benchmark"
    else:
        raise ProtocolError("stage must be pilot or confirmatory")
    if output_path.exists():
        raise ProtocolError("freeze requires a new output directory")
    slots = build_slots(
        [task["id"] for task in tasks],
        campaign_id=campaign_id,
        trials=trials,
        seed=seed,
    )
    plan = {
        "schema_version": 1,
        "protocol": PROTOCOL_ID,
        "stage": stage,
        "claim_ceiling": claim_ceiling,
        "campaign_id": campaign_id,
        "benchmark": BENCHMARK_ID,
        "revision": TERMINAL_BENCH_REVISION,
        "task_manifest": str(manifest_path),
        "task_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "task_count": len(tasks),
        "tasks": tasks,
        "arms": [ARM_SPECS[name].to_dict() for name in DEFAULT_ARMS],
        "trials": trials,
        "seed": seed,
        "slots": [slot.to_dict() for slot in slots],
        "cache_policy": {
            "stable_system_prompt": True,
            "stable_tool_schema": True,
            "append_only_session_history": True,
            "system_prompt_sha256_required": True,
            "tool_schema_sha256_required": True,
            "prefix_change_reason_required": True,
            "first_request_per_session_expected_cold": True,
        },
        "claim_gates": {
            "quality_requires_complete_task_set": stage == "confirmatory",
            "quality_requires_official_verifier": True,
            "quality_requires_three_trials": True,
            "quality_requires_positive_cluster_bootstrap_ci": True,
            "quality_requires_exact_sign_test_p_below_0_05": True,
            "same_model_tools_and_budget": True,
        },
    }
    output_path.mkdir(parents=True)
    plan_path = output_path / "plan.json"
    plan_payload = (
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _write_new(plan_path, plan_payload)
    _write_new(
        output_path / "plan.sha256",
        (hashlib.sha256(plan_payload).hexdigest() + "\n").encode("ascii"),
    )
    return plan


def load_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    try:
        payload = plan_path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("plan is unreadable or invalid JSON") from exc
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL_ID:
        raise ProtocolError("unsupported quality protocol")
    sha_path = plan_path.with_name("plan.sha256")
    try:
        expected_sha = sha_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ProtocolError("plan.sha256 is missing or unreadable") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise ProtocolError("plan hash drifted from plan.sha256")
    if value.get("benchmark") != BENCHMARK_ID:
        raise ProtocolError("plan benchmark differs from the protocol")
    if value.get("revision") != TERMINAL_BENCH_REVISION:
        raise ProtocolError("plan benchmark revision differs from the protocol")
    slots = value.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ProtocolError("plan slots must be nonempty")
    expected: set[tuple[str, str, int]] = set()
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ProtocolError("plan tasks must be nonempty")
    task_ids: set[str] = set()
    for index, item in enumerate(tasks):
        if not isinstance(item, Mapping):
            raise ProtocolError(f"tasks[{index}] must be an object")
        task_id = _safe_text(item.get("id"), f"tasks[{index}].id")
        if task_id in task_ids:
            raise ProtocolError(f"duplicate plan task: {task_id}")
        task_ids.add(task_id)
    if value.get("task_count") != len(task_ids):
        raise ProtocolError("plan task_count differs from tasks")
    trials = value.get("trials")
    if type(trials) is not int or trials < 3:
        raise ProtocolError("plan trials must be at least three")
    campaign_id = _safe_text(value.get("campaign_id"), "campaign_id")
    for index, slot in enumerate(slots):
        if not isinstance(slot, Mapping):
            raise ProtocolError(f"slots[{index}] must be an object")
        task_id = _safe_text(slot.get("task_id"), "slot.task_id")
        arm = _safe_text(slot.get("arm"), "slot.arm")
        trial = slot.get("trial")
        run_id = _safe_text(slot.get("run_id"), "slot.run_id")
        if task_id not in task_ids or arm not in ARM_SPECS:
            raise ProtocolError(f"slots[{index}] is outside the frozen task/arm set")
        if type(trial) is not int or not 0 <= trial < trials:
            raise ProtocolError(f"slots[{index}].trial is invalid")
        if run_id != run_id_for(campaign_id, task_id, arm, trial):
            raise ProtocolError(f"slots[{index}].run_id differs from its identity")
        key = (task_id, arm, trial)
        if key in expected:
            raise ProtocolError(f"duplicate plan slot: {key}")
        expected.add(key)
    required = {
        (task_id, arm, trial)
        for task_id in task_ids
        for arm in DEFAULT_ARMS
        for trial in range(trials)
    }
    if expected != required:
        raise ProtocolError("plan does not contain every task/arm/trial slot")
    return value
