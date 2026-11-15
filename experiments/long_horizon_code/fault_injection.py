"""Deterministic Phase A fault injections for long-horizon Harness mechanisms.

This experiment compares minimal failing baselines with production Harness
seams. It uses synthetic faults and never calls a paid model API. A scenario is
marked blocked whenever its required host boundary cannot be verified.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from my_agent_core.artifacts import ArtifactStore
from my_agent_core.context import ContextManager
from my_agent_core.context_compaction import CompactionPolicy
from my_agent_core.memory import MemoryStore
from my_agent_llm import Message
from my_coding_agent.dream_rsi_contract import (
    SOURCE_COMMIT,
    canonical as policy_canonical,
    checksum,
    policy_identity,
)
from my_coding_agent.dream_rsi_registry import PolicyRegistry
from my_coding_agent.mutation_queue import FileMutationQueue
from my_coding_agent.research_evaluator import (
    NumericPredictionEvaluator,
    ResearchBlocked,
    resolve_docker,
)
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.research_task import ResearchTask
from my_coding_agent.rsi_branch import inventory
from my_coding_agent.rsi_runtime import ParentBudget

SCHEMA_VERSION = 1
DEFAULT_REPEAT = 20
HOST_SCORING_IMAGE = "sha256:896edfbcbf0c29ad15681617a8886cb9871b7f24c5fde4606114c8ee6bfe4c70"
SCENARIO_NAMES = (
    "context_budget_artifact_readback",
    "memory_revision_scope",
    "checkpoint_resume",
    "concurrent_file_mutation",
    "controlled_policy_promotion",
    "host_scoring_sandbox",
    "longitudinal_repo_repair",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _reason_counts(reasons: list[str | None]) -> dict[str, int]:
    return dict(sorted(Counter(reason or "none" for reason in reasons).items()))


def _trim_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n[...unpersisted output clipped...]\n" + text[-tail:]


def _finalize(
    *,
    scenario: str,
    repeat: int,
    baseline_flags: list[bool],
    enhanced_flags: list[bool],
    baseline_reasons: list[str | None],
    enhanced_reasons: list[str | None],
    signatures: list[str],
    first_evidence: dict[str, Any],
    first_metrics: dict[str, Any],
    implementation_level: str,
    scope: str,
    status: str = "completed",
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    if len(baseline_flags) != repeat or len(enhanced_flags) != repeat:
        raise ValueError("scenario did not execute the requested repeat count")
    baseline_count = sum(baseline_flags)
    enhanced_count = sum(enhanced_flags)
    return {
        "scenario": scenario,
        "status": status,
        "baseline_pass": baseline_count,
        "baseline_total": repeat,
        "baseline_pass_rate": baseline_count / repeat,
        "enhanced_pass": enhanced_count,
        "enhanced_total": repeat,
        "enhanced_pass_rate": enhanced_count / repeat,
        "failure_reason": {
            "baseline": next((reason for reason in baseline_reasons if reason), None),
            "enhanced": next((reason for reason in enhanced_reasons if reason), None),
        },
        "failure_classification": {
            "baseline": _reason_counts(baseline_reasons),
            "enhanced": _reason_counts(enhanced_reasons),
        },
        "evidence": {
            "first_observation": first_evidence,
            "deterministic": len(set(signatures)) == 1,
            "distinct_signatures": len(set(signatures)),
            "signature_sha256": _sha256("\n".join(signatures).encode("utf-8")),
        },
        "metrics": {
            "repetitions": repeat,
            "first_observation": first_metrics,
        },
        "implementation_level": implementation_level,
        "scope": scope,
        "blocked_reason": blocked_reason,
    }


class _NoSummaryLLM:
    """The layered projection should fit before any model call in scenario 1."""

    async def achat(self, **_kwargs):
        raise AssertionError("unexpected summarizer call in deterministic artifact scenario")


async def _artifact_repeat(root: Path, index: int) -> dict[str, Any]:
    run_root = root / f"run-{index}"
    workspace = run_root / "workspace"
    workspace.mkdir(parents=True)
    marker = "CRITICAL_EVIDENCE: patch settings/retry_limit.py line 42 from 0 to 3"
    large_output = "diagnostic noise line\n" * 2500 + marker + "\npost-diagnostic noise\n" * 800

    baseline_context = _trim_output(large_output, 900)
    baseline_patch = marker in baseline_context

    store = ArtifactStore(workspace)
    context = ContextManager(
        budget=3000,
        llm=_NoSummaryLLM(),
        keep_recent_tokens=100,
        artifact_store=store,
        compaction_policy=CompactionPolicy(mode="layered"),
    )
    messages = [
        Message(role="system", content="You are a deterministic coding harness."),
        Message(role="user", content="Repair the settings bug and preserve the decisive diagnostic."),
        Message(role="assistant", content="Running the diagnostic command."),
        Message(
            role="tool",
            content=large_output,
            metadata={"tool_name": "shell", "tool_call_id": "large-diagnostic"},
        ),
    ]
    projected = await context.prepare(messages)
    artifact_id = projected[-1].metadata.get("artifact_id")
    if not artifact_id:
        raise AssertionError("layered ContextManager did not advertise an artifact handle")
    readback = store.search(artifact_id, "CRITICAL_EVIDENCE")
    trusted_readback = Message(
        role="tool",
        content=json.dumps(readback, ensure_ascii=False, sort_keys=True),
        metadata={
            "tool_name": "artifact_search",
            "tool_call_id": "artifact-search-critical",
            "artifact_bounded": True,
        },
    )
    enhanced_context_messages = await context.prepare([*messages, trusted_readback])
    enhanced_context = "\n".join(
        message.content for message in enhanced_context_messages if isinstance(message.content, str)
    )
    enhanced_patch = marker in enhanced_context and bool(readback["hits"])
    original_preserved = messages[3].content == large_output
    signature = _sha256(
        json.dumps(
            {
                "baseline_patch": baseline_patch,
                "enhanced_patch": enhanced_patch,
                "hits": readback["hits"],
                "preview_chars": len(projected[-1].content),
                "artifact_digest": artifact_id.rsplit(":", 1)[-1],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_patch,
        "enhanced_pass": enhanced_patch,
        "baseline_reason": None if baseline_patch else "critical_evidence_evicted_without_artifact",
        "enhanced_reason": None if enhanced_patch else "artifact_readback_missing",
        "signature": signature,
        "evidence": {
            "artifact_id": artifact_id,
            "critical_hit_lines": [hit["line"] for hit in readback["hits"]],
            "canonical_session_body_preserved": original_preserved,
            "baseline_context_chars": len(baseline_context),
            "enhanced_context_chars": len(enhanced_context),
        },
        "metrics": {
            "tool_output_chars": len(large_output),
            "preview_chars": len(projected[-1].content),
            "artifact_hits": len(readback["hits"]),
            "baseline_patch_success": baseline_patch,
            "enhanced_patch_success": enhanced_patch,
        },
    }


async def run_context_budget_artifact_readback(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-context-fault-") as temporary:
        for index in range(repeat):
            result = await _artifact_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="context_budget_artifact_readback",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production ArtifactStore plus layered ContextManager",
        scope="Real artifact persistence and context projection; synthetic large tool output and scripted baseline clipping; no provider call.",
    )


class _FakeSemanticIndex:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    async def search(self, *, scope: str, query: str, records: list[dict[str, str]]) -> list[dict[str, str]]:
        del scope, query, records
        return list(self.rows)

    async def aclose(self, timeout: float = 0.1) -> bool:
        del timeout
        return True


async def _memory_repeat(root: Path, index: int) -> dict[str, Any]:
    memory_dir = root / f"memory-{index}"
    semantic = _FakeSemanticIndex()
    store = MemoryStore(memory_dir, memory_index=semantic)
    store.load_from_disk()
    store.remember("deployment_region", "us-east-1", valid_from="2026-01-01", source="old-plan")
    old_scope, old_records = store._semantic_records()
    old_identity, old_record = next(iter(old_records.items()))
    store.remember("deployment_region", "eu-west-1", valid_from="2026-02-01", source="new-plan")
    current_scope, current_records = store._semantic_records()
    current_record = current_records[old_identity]
    semantic.rows = [
        dict(old_record, scope="stale-scope"),
        dict(current_record, scope="other-workspace"),
    ]

    baseline_value = "us-east-1"
    baseline_pass = baseline_value == "eu-west-1"
    retrieved = await store.aretrieve("deployment_region")
    authoritative = json.loads(store.read_full("deployment_region"))["value"]
    enhanced_value = authoritative if retrieved and authoritative in retrieved else None
    enhanced_pass = retrieved is not None and "eu-west-1" in retrieved and "us-east-1" not in retrieved
    signature = _sha256(
        json.dumps(
            {
                "baseline_value": baseline_value,
                "enhanced_value": enhanced_value,
                "old_scope_matches": old_scope != current_scope,
                "candidate_scopes": [row["scope"] for row in semantic.rows],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": None if baseline_pass else "stale_candidate_used_without_revision_scope_check",
        "enhanced_reason": None if enhanced_pass else "authoritative_current_fact_not_returned",
        "signature": signature,
        "evidence": {
            "baseline_value": baseline_value,
            "enhanced_value": enhanced_value,
            "enhanced_retrieval": retrieved,
            "stale_candidate_revision_matches_current": old_record["revision"] == current_record["revision"],
            "cross_scope_candidate_used": semantic.rows[1]["scope"] == current_scope,
        },
        "metrics": {
            "candidate_count": len(semantic.rows),
            "candidate_rejections_expected": 2,
            "authoritative_value_is_current": enhanced_value == "eu-west-1",
        },
    }


async def run_memory_revision_scope(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-memory-fault-") as temporary:
        for index in range(repeat):
            result = await _memory_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="memory_revision_scope",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production MemoryStore.aretrieve with fake semantic candidate index",
        scope="Real local facts, revisions and scope checks; synthetic stale/cross-scope semantic candidates; no vector service call.",
    )


def _prepare_research_task(root: Path) -> ResearchTask:
    root.mkdir(parents=True, exist_ok=True)
    csv_path = root / "input.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["id", "x1", "x2", "target"])
        writer.writeheader()
        for index in range(5):
            writer.writerow({"id": f"r{index}", "x1": index, "x2": index * 2, "target": index * 3})
    return ResearchTask.prepare(
        csv_path,
        root / "task",
        target="target",
        id_column="id",
        seed=23,
        dev_fraction=0.2,
        final_fraction=0.2,
    )


async def _checkpoint_repeat(root: Path, index: int) -> dict[str, Any]:
    run_root = root / f"run-{index}"
    run_root.mkdir(parents=True)
    task = _prepare_research_task(run_root)
    campaign = ResearchCampaign.prepare(
        task,
        run_root / "campaign",
        image="sha256:" + "0" * 64,
        rounds=2,
        limits={
            "max_requests": 10,
            "max_tokens": 1000,
            "max_seconds": 10,
            "max_evaluations": 2,
            "max_output_tokens": 128,
        },
    )
    units = ["analysis", "patch"]
    campaign.append("work_unit_committed", {"unit": units[0], "result_sha256": _sha256(b"analysis")})

    ephemeral_completed: set[str] = set()
    ephemeral_worklog: list[str] = []
    ephemeral_completed.add(units[0])
    ephemeral_worklog.append(units[0])
    ephemeral_completed = set()  # deterministic process reconstruction with no durable state
    for unit in units:
        ephemeral_worklog.append(unit)
        ephemeral_completed.add(unit)
    baseline_replayed = len(ephemeral_worklog) != len(set(ephemeral_worklog))
    baseline_pass = bool(not baseline_replayed and ephemeral_completed == set(units))

    resumed = ResearchCampaign.load(campaign.root)
    resumed_completed = {
        event["payload"]["unit"]
        for event in resumed.events()
        if event["kind"] == "work_unit_committed"
        if event["payload"].get("unit") in units
    }
    resumed_worklog: list[str] = []
    for unit in units:
        if unit in resumed_completed:
            continue
        resumed_worklog.append(unit)
        resumed.append("work_unit_committed", {"unit": unit, "result_sha256": _sha256(unit.encode("utf-8"))})
        resumed_completed.add(unit)
    final = ResearchCampaign.load(campaign.root)
    enhanced_replayed = bool(set(resumed_worklog) & {"analysis"})
    completed_events = [event for event in final.events() if event["kind"] == "work_unit_committed"]
    enhanced_pass = bool(not enhanced_replayed and resumed_completed == set(units) and len(completed_events) == 2)
    events = final.events()
    signature = _sha256(
        json.dumps(
            {
                "baseline_worklog": ephemeral_worklog,
                "enhanced_worklog": resumed_worklog,
                "event_kinds": [event["kind"] for event in events],
                "committed_units": sorted(resumed_completed),
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": None if baseline_pass else "in_memory_state_lost_after_reconstruction",
        "enhanced_reason": None if enhanced_pass else "journal_resume_missing_or_replayed_work",
        "signature": signature,
        "evidence": {
            "campaign_manifest_sha256": final.manifest_sha256,
            "journal_sequence": [event["sequence"] for event in events],
            "event_kinds": [event["kind"] for event in events],
            "baseline_worklog": ephemeral_worklog,
            "enhanced_worklog": resumed_worklog,
        },
        "metrics": {
            "baseline_replayed_units": len(ephemeral_worklog) - len(set(ephemeral_worklog)),
            "enhanced_replayed_units": 1 if enhanced_replayed else 0,
            "journal_events": len(events),
            "committed_units": len(resumed_completed),
        },
    }


async def run_checkpoint_resume(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-checkpoint-fault-") as temporary:
        for index in range(repeat):
            result = await _checkpoint_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="checkpoint_resume",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production ResearchTask and ResearchCampaign journal/load integration fixture",
        scope="Real campaign plan plus hash-chained fsynced journal reload; synthetic two-unit work executes no model or Docker round.",
    )


async def _baseline_concurrent_write(path: Path) -> tuple[list[str], int]:
    barrier = asyncio.Barrier(2)

    async def worker(token: str) -> None:
        values = json.loads(path.read_text(encoding="utf-8"))
        await barrier.wait()
        values.append(token)
        path.write_text(json.dumps(values), encoding="utf-8")

    await asyncio.gather(worker("left"), worker("right"))
    return json.loads(path.read_text(encoding="utf-8")), 2


async def _enhanced_concurrent_write(path: Path) -> tuple[list[str], int]:
    queue = FileMutationQueue()

    async def worker(token: str) -> None:
        async with queue.acquire(path):
            values = json.loads(path.read_text(encoding="utf-8"))
            await asyncio.sleep(0)
            values.append(token)
            path.write_text(json.dumps(values), encoding="utf-8")

    await asyncio.gather(worker("left"), worker("right"))
    return json.loads(path.read_text(encoding="utf-8")), 2


async def _concurrent_repeat(root: Path, index: int) -> dict[str, Any]:
    baseline_path = root / f"baseline-{index}.json"
    enhanced_path = root / f"enhanced-{index}.json"
    baseline_path.write_text("[]", encoding="utf-8")
    enhanced_path.write_text("[]", encoding="utf-8")
    baseline_values, baseline_writes = await _baseline_concurrent_write(baseline_path)
    enhanced_values, enhanced_writes = await _enhanced_concurrent_write(enhanced_path)
    expected = {"left", "right"}
    baseline_pass = set(baseline_values) == expected and len(baseline_values) == 2
    enhanced_pass = set(enhanced_values) == expected and len(enhanced_values) == 2
    baseline_lost = len(expected - set(baseline_values))
    enhanced_lost = len(expected - set(enhanced_values))
    signature = _sha256(
        json.dumps(
            {
                "baseline_values": baseline_values,
                "enhanced_values": enhanced_values,
                "baseline_writes": baseline_writes,
                "enhanced_writes": enhanced_writes,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": None if baseline_pass else "read_modify_write_race_lost_update",
        "enhanced_reason": None if enhanced_pass else "mutation_queue_failed_to_serialize",
        "signature": signature,
        "evidence": {
            "baseline_final_file": str(baseline_path),
            "enhanced_final_file": str(enhanced_path),
            "baseline_values": baseline_values,
            "enhanced_values": enhanced_values,
        },
        "metrics": {
            "baseline_writes": baseline_writes,
            "enhanced_writes": enhanced_writes,
            "baseline_lost_updates": baseline_lost,
            "enhanced_lost_updates": enhanced_lost,
        },
    }


async def run_concurrent_file_mutation(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-mutation-fault-") as temporary:
        for index in range(repeat):
            result = await _concurrent_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="concurrent_file_mutation",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production FileMutationQueue.acquire",
        scope="Real same-path async mutation queue; synthetic barrier makes the unprotected lost-update race deterministic.",
    )


def _policy_document(parameters: dict[str, int], label: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "parameters": parameters,
        "policy_hash": policy_identity(parameters),
        "receipt": {
            "evidence_sha256": _sha256(f"{label}:evidence".encode("utf-8")),
            "plan_sha256": _sha256(f"{label}:plan".encode("utf-8")),
            "summary_sha256": _sha256(f"{label}:summary".encode("utf-8")),
        },
    }


def _install_policy(registry: PolicyRegistry, parameters: dict[str, int], label: str) -> str:
    raw = policy_canonical(_policy_document(parameters, label))
    version = checksum(raw)
    _atomic_bytes(registry.root / "versions" / f"{version}.json", raw)
    return version


def _seed_registry(
    registry: PolicyRegistry,
    previous: dict[str, int],
    active: dict[str, int],
) -> tuple[str, str]:
    previous_version = _install_policy(registry, previous, "previous")
    active_version = _install_policy(registry, active, "active")
    pointers = {
        "schema_version": 1,
        "active": active_version,
        "previous": previous_version,
        "revision": uuid4().hex,
    }
    _atomic_bytes(registry.root / "pointers.json", policy_canonical(pointers))
    return previous_version, active_version


async def _policy_repeat(root: Path, index: int) -> dict[str, Any]:
    baseline_registry = PolicyRegistry(root / f"baseline-registry-{index}")
    enhanced_registry = PolicyRegistry(root / f"enhanced-registry-{index}")
    previous = {"max_width": 1, "max_refine": 0}
    incumbent = {"max_width": 2, "max_refine": 0}
    candidate = {"max_width": 4, "max_refine": 2}
    scores = {
        "train": {"baseline": 0.58, "incumbent": 0.70, "candidate": 0.96},
        "holdout": {"baseline": 0.60, "incumbent": 0.65, "candidate": 0.58},
    }
    min_gain = 0.01

    _seed_registry(baseline_registry, previous, incumbent)
    candidate_version = _install_policy(baseline_registry, candidate, "train-only-candidate")
    baseline_pointers = baseline_registry._pointers()
    _atomic_bytes(
        baseline_registry.root / "pointers.json",
        policy_canonical(
            {
                "schema_version": 1,
                "active": candidate_version,
                "previous": baseline_pointers["active"],
                "revision": uuid4().hex,
            }
        ),
    )
    baseline_active = baseline_registry.snapshot()
    baseline_pass = baseline_active["parameters"] == incumbent

    previous_version, incumbent_version = _seed_registry(enhanced_registry, previous, incumbent)
    before = enhanced_registry.snapshot()
    strict_train = scores["train"]["candidate"] > scores["train"]["incumbent"] + min_gain
    strict_holdout = (
        scores["holdout"]["candidate"] > scores["holdout"]["baseline"] + min_gain
        and scores["holdout"]["candidate"] > scores["holdout"]["incumbent"] + min_gain
    )
    accepted = strict_train and strict_holdout
    if accepted:
        raise AssertionError("fixture unexpectedly passed strict holdout")
    after_rejection = enhanced_registry.snapshot()
    incumbent_retained = after_rejection["token"] == before["token"]
    rollback = enhanced_registry.rollback()
    rollback_ok = rollback["version"] == previous_version
    enhanced_pass = bool(not accepted and incumbent_retained and rollback_ok)
    signature = _sha256(
        json.dumps(
            {
                "baseline_active": baseline_active["policy_hash"],
                "enhanced_active_after_rejection": after_rejection["policy_hash"],
                "rollback_active": rollback["policy_hash"],
                "strict_train": strict_train,
                "strict_holdout": strict_holdout,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": None if baseline_pass else "train_only_overfit_promoted",
        "enhanced_reason": None if enhanced_pass else "holdout_gate_or_registry_rollback_failed",
        "signature": signature,
        "evidence": {
            "baseline_active_policy_hash": baseline_active["policy_hash"],
            "incumbent_policy_hash": policy_identity(incumbent),
            "candidate_policy_hash": policy_identity(candidate),
            "enhanced_active_after_rejection": after_rejection["policy_hash"],
            "rollback_active_version": rollback["version"],
            "expected_previous_version": previous_version,
            "incumbent_version": incumbent_version,
        },
        "metrics": {
            "train_gain": scores["train"]["candidate"] - scores["train"]["incumbent"],
            "holdout_gain_vs_baseline": scores["holdout"]["candidate"] - scores["holdout"]["baseline"],
            "holdout_gain_vs_incumbent": scores["holdout"]["candidate"] - scores["holdout"]["incumbent"],
            "min_gain": min_gain,
            "incumbent_retained": incumbent_retained,
            "rollback_success": rollback_ok,
        },
    }


async def run_controlled_policy_promotion(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-policy-fault-") as temporary:
        for index in range(repeat):
            result = await _policy_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="controlled_policy_promotion",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production PolicyRegistry snapshot and rollback plus strict holdout rule seam",
        scope="Real immutable registry and rollback mechanics; synthetic score vectors and deterministic strict-gain rule; full DREAM-RSI optimizer not run.",
    )


def _evaluation_budget() -> ParentBudget:
    return ParentBudget(
        max_requests=1,
        max_tokens=1000,
        max_seconds=60,
        max_evaluations=1,
        max_output_tokens=128,
    )


def _candidate_workspace(root: Path, name: str, code: str) -> Path:
    workspace = root / name
    workspace.mkdir()
    (workspace / "train.py").write_text(code, encoding="utf-8")
    return workspace


def _new_evaluation_output(output_root: Path, before: set[Path]) -> Path:
    after = {path for path in output_root.iterdir() if path.is_dir()}
    created = sorted(after - before)
    if len(created) != 1:
        raise AssertionError(f"expected one evaluation directory, found {len(created)}")
    return created[0]


async def _host_scoring_repeat(root: Path, index: int) -> dict[str, Any]:
    run_root = root / f"run-{index}"
    task = _prepare_research_task(run_root)
    reported_score = 1.0
    (run_root / "candidate-report.json").write_text(
        json.dumps({"score": reported_score}, sort_keys=True),
        encoding="utf-8",
    )
    candidate_report = json.loads((run_root / "candidate-report.json").read_text(encoding="utf-8"))
    baseline_score = float(candidate_report["score"])
    baseline_pass = False  # Blind self-report acceptance violates the desired host-authority invariant.

    docker_prefix = resolve_docker()
    output_root = run_root / "evaluator-output"
    output_root.mkdir(parents=True)
    evaluator = NumericPredictionEvaluator(
        task,
        image=HOST_SCORING_IMAGE,
        output_dir=output_root,
        timeout=30,
        cpus=1.0,
        memory_mb=256,
        pids_limit=32,
        docker_command=docker_prefix,
    )
    source_code = (task.source_dir / "train.py").read_text(encoding="utf-8")
    valid_workspace = _candidate_workspace(run_root, "valid-candidate", source_code)
    before_valid = {path for path in output_root.iterdir() if path.is_dir()}
    valid_result = await evaluator.evaluate(valid_workspace, _evaluation_budget(), None)
    valid_directory = _new_evaluation_output(output_root, before_valid)
    valid_output_files = set(inventory(valid_directory / "output"))

    escaped_code = source_code + (
        "\nwith open('/output/escaped.txt', 'w', encoding='utf-8') as stream:\n"
        "    stream.write('undeclared side effect')\n"
    )
    escaped_workspace = _candidate_workspace(run_root, "escaped-output-candidate", escaped_code)
    before_escaped = {path for path in output_root.iterdir() if path.is_dir()}
    escaped_result = await evaluator.evaluate(escaped_workspace, _evaluation_budget(), None)
    escaped_directory = _new_evaluation_output(output_root, before_escaped)
    escaped_output_files = set(inventory(escaped_directory / "output"))

    extra_workspace = _candidate_workspace(run_root, "extra-file-candidate", source_code)
    (extra_workspace / "escaped.txt").write_text("undeclared workspace artifact", encoding="utf-8")
    try:
        await evaluator.evaluate(extra_workspace, _evaluation_budget(), None)
    except ValueError as exc:
        extra_workspace_error = str(exc)
    else:
        extra_workspace_error = None

    host_score = valid_result.get("score")
    image_pin = valid_result.get("pins", {}).get("image_id")
    valid_accepted = bool(valid_result.get("valid") and host_score is not None and valid_output_files == {"predictions.csv"})
    escaped_rejected = bool(
        not escaped_result.get("valid")
        and escaped_result.get("feedback") == "candidate execution failed"
        and escaped_output_files == {"predictions.csv"}
    )
    extra_workspace_rejected = extra_workspace_error == "candidate workspace must contain only train.py"
    self_report_ignored = host_score is not None and baseline_score != host_score
    image_matches = image_pin == HOST_SCORING_IMAGE
    enhanced_pass = bool(
        valid_accepted
        and escaped_rejected
        and extra_workspace_rejected
        and self_report_ignored
        and image_matches
    )
    signature = _sha256(
        json.dumps(
            {
                "reported_score": reported_score,
                "host_score": host_score,
                "valid_accepted": valid_accepted,
                "valid_output_files": sorted(valid_output_files),
                "escaped_rejected": escaped_rejected,
                "escaped_feedback": escaped_result.get("feedback"),
                "escaped_output_files": sorted(escaped_output_files),
                "extra_workspace_error": extra_workspace_error,
                "image_matches": image_matches,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": "candidate_self_report_trusted",
        "enhanced_reason": None if enhanced_pass else "host_scoring_safety_invariant_failed",
        "signature": signature,
        "blocked": False,
        "blocked_reason": None,
        "evidence": {
            "docker_prefix": list(docker_prefix),
            "requested_image": HOST_SCORING_IMAGE,
            "verified_image_id": image_pin,
            "reported_score": reported_score,
            "host_recomputed_score": host_score,
            "valid_feedback": valid_result.get("feedback"),
            "valid_output_files": sorted(valid_output_files),
            "escaped_feedback": escaped_result.get("feedback"),
            "escaped_output_files": sorted(escaped_output_files),
            "extra_workspace_error": extra_workspace_error,
        },
        "metrics": {
            "container_runs": 2,
            "valid_candidate_accepted": valid_accepted,
            "escaped_output_rejected": escaped_rejected,
            "extra_workspace_rejected": extra_workspace_rejected,
            "self_report_ignored": self_report_ignored,
            "image_identity_verified": image_matches,
            "reported_minus_host_score": (
                reported_score - host_score if host_score is not None else None
            ),
        },
    }


async def run_host_scoring_sandbox(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    blocked_reason = None
    completed_container_runs = 0
    with tempfile.TemporaryDirectory(prefix="harness-host-score-fault-") as temporary:
        for index in range(repeat):
            try:
                result = await _host_scoring_repeat(Path(temporary), index)
            except (ResearchBlocked, asyncio.TimeoutError, OSError) as exc:
                blocked_reason = blocked_reason or f"{type(exc).__name__}: {exc}"
                baseline_flags.append(False)
                enhanced_flags.append(False)
                baseline_reasons.append("candidate_self_report_trusted")
                enhanced_reasons.append(f"blocked: {blocked_reason}")
                signatures.append(_sha256(f"blocked:{blocked_reason}".encode("utf-8")))
                if not first_evidence:
                    first_evidence = {"blocked_reason": blocked_reason}
                    first_metrics = {"container_runs": 0}
                continue
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            completed_container_runs += result["metrics"]["container_runs"]
            blocked_reason = blocked_reason or result["blocked_reason"]
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    report = _finalize(
        scenario="host_scoring_sandbox",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="production NumericPredictionEvaluator + DockerRunner + ResearchTask",
        scope="Real pinned-image WSL Docker execution with production network/file/resource policy; synthetic candidates and self-reported score.",
        status="blocked" if blocked_reason else "completed",
        blocked_reason=blocked_reason,
    )
    report["metrics"]["container_runs_total"] = completed_container_runs
    report["metrics"]["requested_container_runs"] = repeat * 2
    return report


def _create_integrated_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "app.py").write_text(
        "import json\n"
        "from pathlib import Path\n\n"
        "def effective_retry_limit(root):\n"
        "    return json.loads((Path(root) / 'config.json').read_text(encoding='utf-8'))['retry_limit']\n",
        encoding="utf-8",
    )
    (path / "owners.json").write_text("[]\n", encoding="utf-8")
    (path / "tests" / "test_repair.py").write_text(
        "import json\n"
        "import unittest\n"
        "from pathlib import Path\n"
        "from app import effective_retry_limit\n\n"
        "ROOT = Path(__file__).resolve().parents[1]\n\n"
        "class RepairTests(unittest.TestCase):\n"
        "    def test_config(self):\n"
        "        self.assertEqual(effective_retry_limit(ROOT), 7)\n\n"
        "    def test_region(self):\n"
        "        self.assertEqual((ROOT / 'region.txt').read_text(encoding='utf-8').strip(), 'ap-southeast-1')\n\n"
        "    def test_resume(self):\n"
        "        state = json.loads((ROOT / 'resume.json').read_text(encoding='utf-8'))\n"
        "        self.assertTrue(state['journal_resumed'])\n"
        "        self.assertEqual(state['replayed_units'], [])\n\n"
        "    def test_conflicting_mutations_merge(self):\n"
        "        owners = json.loads((ROOT / 'owners.json').read_text(encoding='utf-8'))\n"
        "        self.assertEqual(set(owners), {'left', 'right'})\n"
        "        self.assertEqual(len(owners), 2)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    return path


async def _diagnostic_config_stage(run_root: Path, repo: Path, *, enhanced: bool) -> dict[str, Any]:
    marker = "CRITICAL_EVIDENCE retry_limit=7"
    diagnostic = "temporary diagnostic noise\n" * 2400 + marker + "\npost-check noise\n" * 900
    baseline_visible = _trim_output(diagnostic, 900)
    if not enhanced:
        retry_limit = 4
        (repo / "config.json").write_text(json.dumps({"retry_limit": retry_limit}), encoding="utf-8")
        return {
            "pass": retry_limit == 7,
            "evidence": {
                "critical_evidence_visible": marker in baseline_visible,
                "visible_chars": len(baseline_visible),
            },
            "metrics": {"retry_limit": retry_limit, "diagnostic_chars": len(diagnostic)},
        }

    workspace = run_root / "artifact-workspace"
    workspace.mkdir(parents=True)
    store = ArtifactStore(workspace)
    context = ContextManager(
        budget=3000,
        llm=_NoSummaryLLM(),
        keep_recent_tokens=100,
        artifact_store=store,
        compaction_policy=CompactionPolicy(mode="layered"),
    )
    messages = [
        Message(role="system", content="Apply only host-verified repair evidence."),
        Message(role="user", content="Find the retry-limit repair evidence in the large diagnostic."),
        Message(role="assistant", content="Running the repository diagnostic."),
        Message(
            role="tool",
            content=diagnostic,
            metadata={"tool_name": "shell", "tool_call_id": "longitudinal-diagnostic"},
        ),
    ]
    projected = await context.prepare(messages)
    artifact_id = projected[-1].metadata.get("artifact_id")
    hits = store.search(artifact_id, "CRITICAL_EVIDENCE") if artifact_id else {"hits": []}
    retry_limit = 4
    if hits["hits"]:
        retry_limit = int(hits["hits"][0]["text"].rsplit("=", 1)[-1])
    (repo / "config.json").write_text(json.dumps({"retry_limit": retry_limit}), encoding="utf-8")
    return {
        "pass": retry_limit == 7,
        "evidence": {
            "artifact_id": artifact_id,
            "critical_hit_lines": [hit["line"] for hit in hits["hits"]],
            "canonical_output_preserved": messages[3].content == diagnostic,
        },
        "metrics": {"retry_limit": retry_limit, "artifact_hits": len(hits["hits"])},
    }


async def _regional_constraint_stage(run_root: Path, repo: Path, *, enhanced: bool) -> dict[str, Any]:
    if not enhanced:
        baseline_region = "legacy-region"
        (repo / "region.txt").write_text(baseline_region, encoding="utf-8")
        return {
            "pass": False,
            "evidence": {"selected_region": baseline_region},
            "metrics": {"trusted_candidates": 1},
        }

    semantic = _FakeSemanticIndex()
    store = MemoryStore(run_root / "memory", memory_index=semantic)
    store.load_from_disk()
    store.remember("deployment_region", "legacy-region", valid_from="2026-01-01", source="old-plan")
    _, old_records = store._semantic_records()
    identity, old_record = next(iter(old_records.items()))
    store.remember("deployment_region", "ap-southeast-1", valid_from="2026-03-01", source="new-plan")
    scope, current_records = store._semantic_records()
    current_record = current_records[identity]
    semantic.rows = [
        dict(old_record, scope="stale-scope"),
        dict(current_record, scope="other-workspace"),
    ]
    retrieved = await store.aretrieve("deployment_region")
    enhanced_region = "ap-southeast-1" if retrieved and "ap-southeast-1" in retrieved else "unresolved"
    (repo / "region.txt").write_text(enhanced_region, encoding="utf-8")
    return {
        "pass": enhanced_region == "ap-southeast-1",
        "evidence": {
            "selected_region": enhanced_region,
            "retrieved_text": retrieved,
            "current_scope": scope,
            "stale_revision_rejected": old_record["revision"] != current_record["revision"],
            "cross_scope_candidate_rejected": semantic.rows[1]["scope"] != scope,
        },
        "metrics": {"candidates_received": 2, "validated_current_facts": 1},
    }


def _campaign_fixture(run_root: Path) -> ResearchCampaign:
    task = _prepare_research_task(run_root / "task-fixture")
    return ResearchCampaign.prepare(
        task,
        run_root / "campaign",
        image="sha256:" + "0" * 64,
        rounds=2,
        limits={
            "max_requests": 10,
            "max_tokens": 1000,
            "max_seconds": 10,
            "max_evaluations": 2,
            "max_output_tokens": 128,
        },
    )


def _resume_stage(run_root: Path, repo: Path, *, enhanced: bool) -> dict[str, Any]:
    if not enhanced:
        baseline_worklog = ["stage1_config", "stage2_region"]
        baseline_completed = set()  # process reconstruction without durable journal
        for unit in ["stage1_config", "stage2_region", "stage3_resume"]:
            baseline_worklog.append(unit)
            baseline_completed.add(unit)
        replayed = baseline_worklog[2:]
        state = {
            "journal_resumed": False,
            "replayed_units": replayed,
            "completed_units": sorted(baseline_completed),
        }
        (repo / "resume.json").write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return {
            "pass": not replayed,
            "evidence": {"worklog": baseline_worklog, "state": state},
            "metrics": {"replayed_units": len(replayed)},
        }

    campaign = _campaign_fixture(run_root / "journal-fixture")
    campaign.append("work_unit_committed", {"unit": "stage1_config"})
    campaign.append("work_unit_committed", {"unit": "stage2_region"})
    resumed_campaign = ResearchCampaign.load(campaign.root)
    completed = {
        event["payload"]["unit"]
        for event in resumed_campaign.events()
        if event["kind"] == "work_unit_committed"
    }
    worklog = []
    if "stage3_resume" not in completed:
        worklog.append("stage3_resume")
        resumed_campaign.append("work_unit_committed", {"unit": "stage3_resume"})
        completed.add("stage3_resume")
    state = {
        "journal_resumed": True,
        "replayed_units": [],
        "completed_units": sorted(completed),
    }
    (repo / "resume.json").write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    verified = ResearchCampaign.load(resumed_campaign.root)
    passed = worklog == ["stage3_resume"] and completed == {
        "stage1_config", "stage2_region", "stage3_resume"
    }
    return {
        "pass": passed,
        "evidence": {
            "worklog_after_reconstruction": worklog,
            "state": state,
            "journal_events": [event["kind"] for event in verified.events()],
        },
        "metrics": {
            "replayed_units": 0,
            "journal_sequence": [event["sequence"] for event in verified.events()],
        },
    }


async def _conflicting_mutation_stage(run_root: Path, repo: Path, *, enhanced: bool) -> dict[str, Any]:
    path = run_root / "owners.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    if enhanced:
        owners, writes = await _enhanced_concurrent_write(path)
    else:
        owners, writes = await _baseline_concurrent_write(path)
    expected = {"left", "right"}
    passed = set(owners) == expected and len(owners) == 2
    (repo / "owners.json").write_text(json.dumps(owners, sort_keys=True), encoding="utf-8")
    return {
        "pass": passed,
        "evidence": {"owners": owners},
        "metrics": {"writes": writes, "lost_updates": len(expected - set(owners))},
    }


def _host_test_stage(repo: Path) -> dict[str, Any]:
    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(repo)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    test_result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(repo / "tests"), "-v"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return {
        "pass": compile_result.returncode == 0 and test_result.returncode == 0,
        "evidence": {
            "compile_returncode": compile_result.returncode,
            "test_returncode": test_result.returncode,
            "test_stdout_tail": test_result.stdout[-1600:],
            "test_stderr_tail": test_result.stderr[-800:],
        },
        "metrics": {"host_subprocesses": 2},
    }


def _stage_view(
    name: str,
    baseline: dict[str, Any],
    enhanced: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage": name,
        "baseline_pass": baseline["pass"],
        "enhanced_pass": enhanced["pass"],
        "evidence": {"baseline": baseline["evidence"], "enhanced": enhanced["evidence"]},
        "metrics": {"baseline": baseline["metrics"], "enhanced": enhanced["metrics"]},
    }


async def _longitudinal_repo_repeat(root: Path, index: int) -> dict[str, Any]:
    run_root = root / f"run-{index}"
    run_root.mkdir(parents=True)
    baseline_repo = _create_integrated_repo(run_root / "baseline-repo")
    enhanced_repo = _create_integrated_repo(run_root / "enhanced-repo")

    baseline_diagnostic = await _diagnostic_config_stage(
        run_root / "baseline-diagnostic", baseline_repo, enhanced=False
    )
    enhanced_diagnostic = await _diagnostic_config_stage(
        run_root / "enhanced-diagnostic", enhanced_repo, enhanced=True
    )
    stages = [_stage_view("diagnostic_and_config", baseline_diagnostic, enhanced_diagnostic)]

    baseline_regional = await _regional_constraint_stage(
        run_root / "baseline-memory", baseline_repo, enhanced=False
    )
    enhanced_regional = await _regional_constraint_stage(
        run_root / "enhanced-memory", enhanced_repo, enhanced=True
    )
    stages.append(_stage_view("regional_constraint_revision", baseline_regional, enhanced_regional))

    baseline_resume = _resume_stage(run_root / "baseline-resume", baseline_repo, enhanced=False)
    enhanced_resume = _resume_stage(run_root / "enhanced-resume", enhanced_repo, enhanced=True)
    stages.append(_stage_view("journal_resume_after_reconstruction", baseline_resume, enhanced_resume))

    baseline_mutations = await _conflicting_mutation_stage(
        run_root / "baseline-mutations", baseline_repo, enhanced=False
    )
    enhanced_mutations = await _conflicting_mutation_stage(
        run_root / "enhanced-mutations", enhanced_repo, enhanced=True
    )
    stages.append(_stage_view("conflicting_file_mutations", baseline_mutations, enhanced_mutations))

    baseline_host = _host_test_stage(baseline_repo)
    enhanced_host = _host_test_stage(enhanced_repo)
    baseline_pass = bool(
        all(stage["baseline_pass"] for stage in stages)
        and baseline_host["pass"]
    )
    enhanced_pass = bool(
        all(stage["enhanced_pass"] for stage in stages)
        and enhanced_host["pass"]
    )
    first_baseline_failure = next(
        (stage["stage"] for stage in stages if not stage["baseline_pass"]),
        None if baseline_host["pass"] else "host_test_compile",
    )
    first_enhanced_failure = next(
        (stage["stage"] for stage in stages if not stage["enhanced_pass"]),
        None if enhanced_host["pass"] else "host_test_compile",
    )
    baseline_reason = (
        None if baseline_pass else f"{first_baseline_failure or 'host_test_compile'}_failed"
    )
    enhanced_reason = (
        None if enhanced_pass else f"{first_enhanced_failure or 'host_test_compile'}_failed"
    )
    signature_data = {
        "stages": [
            [stage["stage"], stage["baseline_pass"], stage["enhanced_pass"]]
            for stage in stages
        ],
        "baseline_host_returncodes": [
            baseline_host["evidence"]["compile_returncode"],
            baseline_host["evidence"]["test_returncode"],
        ],
        "enhanced_host_returncodes": [
            enhanced_host["evidence"]["compile_returncode"],
            enhanced_host["evidence"]["test_returncode"],
        ],
    }
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": baseline_reason,
        "enhanced_reason": enhanced_reason,
        "signature": _sha256(json.dumps(signature_data, sort_keys=True).encode("utf-8")),
        "evidence": {
            "stages": stages,
            "host_validation": {"baseline": baseline_host, "enhanced": enhanced_host},
            "baseline_final_files": sorted(inventory(baseline_repo)),
            "enhanced_final_files": sorted(inventory(enhanced_repo)),
        },
        "metrics": {
            "stages": 4,
            "baseline_stage_passes": sum(stage["baseline_pass"] for stage in stages),
            "enhanced_stage_passes": sum(stage["enhanced_pass"] for stage in stages),
            "host_subprocesses": 4,
            "baseline_compile_and_tests_pass": baseline_host["pass"],
            "enhanced_compile_and_tests_pass": enhanced_host["pass"],
        },
    }


async def run_longitudinal_repo_repair(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    baseline_flags, enhanced_flags, baseline_reasons, enhanced_reasons, signatures = [], [], [], [], []
    first_evidence: dict[str, Any] = {}
    first_metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="harness-longitudinal-repair-") as temporary:
        for index in range(repeat):
            result = await _longitudinal_repo_repeat(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            signatures.append(result["signature"])
            if index == 0:
                first_evidence, first_metrics = result["evidence"], result["metrics"]
    return _finalize(
        scenario="longitudinal_repo_repair",
        repeat=repeat,
        baseline_flags=baseline_flags,
        enhanced_flags=enhanced_flags,
        baseline_reasons=baseline_reasons,
        enhanced_reasons=enhanced_reasons,
        signatures=signatures,
        first_evidence=first_evidence,
        first_metrics=first_metrics,
        implementation_level="scripted four-stage integration over production Harness primitives",
        scope="Temp Python repository plus fixed host compile/unit-test subprocesses; no model call and no claim of real model long-task behavior.",
    )


SCENARIOS: dict[str, Callable[[int], Any]] = {
    "context_budget_artifact_readback": run_context_budget_artifact_readback,
    "memory_revision_scope": run_memory_revision_scope,
    "checkpoint_resume": run_checkpoint_resume,
    "concurrent_file_mutation": run_concurrent_file_mutation,
    "controlled_policy_promotion": run_controlled_policy_promotion,
    "host_scoring_sandbox": run_host_scoring_sandbox,
    "longitudinal_repo_repair": run_longitudinal_repo_repair,
}


async def run_experiment(
    *,
    repeat: int = DEFAULT_REPEAT,
    scenario_names: list[str] | None = None,
) -> dict[str, Any]:
    if type(repeat) is not int or repeat < 1:
        raise ValueError("repeat must be a positive integer")
    selected = scenario_names or list(SCENARIO_NAMES)
    unknown = sorted(set(selected) - set(SCENARIO_NAMES))
    if unknown:
        raise ValueError(f"unknown scenarios: {', '.join(unknown)}")
    scenarios = []
    for name in selected:
        scenarios.append(await SCENARIOS[name](repeat))
    total = repeat * len(scenarios)
    completed = [item for item in scenarios if item["status"] == "completed"]
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": "A",
        "kind": "deterministic_fault_injection",
        "repeat": repeat,
        "scenario_count": len(scenarios),
        "summary": {
            "baseline_pass": sum(item["baseline_pass"] for item in scenarios),
            "baseline_total": total,
            "enhanced_pass": sum(item["enhanced_pass"] for item in scenarios),
            "enhanced_total": total,
            "completed_scenarios": len(completed),
            "blocked_scenarios": len(scenarios) - len(completed),
        },
        "scope": {
            "mode": "offline deterministic Phase A",
            "paid_model_calls": False,
            "real_components": [
                "ArtifactStore",
                "ContextManager",
                "MemoryStore",
                "ResearchCampaign",
                "FileMutationQueue",
                "PolicyRegistry",
                "ResearchTask",
                "NumericPredictionEvaluator",
                "DockerRunner",
            ],
            "synthetic_components": [
                "large tool output",
                "stale and cross-scope semantic candidates",
                "two-unit interrupted worklog",
                "same-file race barrier",
                "train/holdout score vectors",
                "forged candidate score and undeclared artifact",
                "four-stage temp repository repair script",
            ],
            "unsupported_claim": "This is module-level and scripted integration evidence, not a Terminal-Bench score or real-model long-task result.",
        },
        "scenarios": scenarios,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="deterministic repeats per scenario (default: 20)")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIO_NAMES,
        help="run only the named scenario; repeat the flag for multiple scenarios",
    )
    parser.add_argument("--output", type=Path, help="also atomically write the JSON report to this path")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = asyncio.run(run_experiment(repeat=args.repeat, scenario_names=args.scenario))
    except (ValueError, OSError) as exc:
        print(f"fault-injection error: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=None if args.compact else 2,
    )
    if args.output is not None:
        _atomic_bytes(args.output.expanduser(), (payload + "\n").encode("utf-8"))
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
