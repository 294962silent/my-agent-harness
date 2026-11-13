"""Exploratory recovery r1 after collection failure and before the first test.

Offline only. This module never constructs a model client. It preserves the
five-trial denominator and rejects publication when validation is incomplete.
Only a new recovery directory and additive authorized campaign commit are used.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

from experiments.context_rsi.live_runner import Campaign, _bytes, _exclusive_json, _sha, _sync_directory, verify_plan
from my_coding_agent.dream_rsi_contract import policy_identity, validate_trace
from my_coding_agent import dream_rsi_optimizer as optimizer
from my_coding_agent.rsi_runtime import TaskSplits, export_dataset

RECOVERY_ID = "exploratory_recovery_r1_after_collection_before_first_test"
PUBLICATION_STATUS = "validation_incomplete_rejected_for_publication"


class RecoveryError(RuntimeError):
    """Immutable recovery prerequisites failed; do not search or replay models."""


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _inventory(root, *, skip=()):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise RecoveryError("recovery material cannot contain link paths")
        if path.is_file() and not path.name.endswith(".lock"):
            name = path.relative_to(root).as_posix()
            if name not in skip:
                result[name] = _sha(path.read_bytes())
    return result


def _script_hash():
    return _sha(Path(__file__).read_bytes())


def _sdk_fingerprint():
    spec = importlib.util.find_spec("dream_rsi")
    if spec is None or spec.origin is None or importlib.metadata.version("dream-rsi") != "0.1.0":
        raise RecoveryError("pinned optional Dream RSI 0.1.0 required")
    root = Path(spec.origin).parent
    return {"version": "0.1.0", "files": {p.relative_to(root).as_posix(): _sha(p.read_bytes())
            for p in sorted(root.rglob("*.py"))}}


def _collection(campaign):
    campaign.assert_execution_pin()
    plan, tasks = verify_plan(campaign)
    rows = campaign.events()
    summary = campaign.summary()
    slots = [s for s in plan["slots"] if s["phase"] == "collection"]
    if (len(plan["slots"]) != 53 or len(slots) != 5 or summary["blocked"]
            or summary["pending_requests"] or summary["unknown_requests"] or summary["held_tokens"]):
        raise RecoveryError("requires five clean collection trials and zero unsettled usage")
    expected = {"HumanEval/7", "HumanEval/11", "HumanEval/27", "HumanEval/32", "HumanEval/45"}
    if {s["task_id"] for s in slots} != expected or set(summary["trials"]) != {s["trial_id"] for s in slots}:
        raise RecoveryError("collection identities differ or a test/unplanned trial started")
    starts = [r for r in rows if r["event"] == "optimizer_started"]
    if (len(starts) != 1 or starts[0]["collection_trials"] != [s["trial_id"] for s in slots]
            or any(r["event"] == "optimizer_finished" for r in rows)
            or (campaign.root / "research-policy.json").exists()
            or (campaign.root / "optimizer").exists() or (campaign.root / "private-registry").exists()):
        raise RecoveryError("requires the single interrupted export, no policy or SDK search artifacts")
    if plan["optimization"]["budgets"] != [1, 2, 4] or plan["optimization"]["max_candidates"] != 4:
        raise RecoveryError("frozen candidate grid or action budgets differ")
    records = campaign._state(rows)[1]
    coverage, available = [], []
    for slot in slots:
        directory = campaign.root / "trials" / slot["trial_id"]
        terminal = [r for r in rows if r["event"] == "trial_finished" and r["trial_id"] == slot["trial_id"]]
        trial_start = [r for r in rows if r["event"] == "trial_started" and r["trial_id"] == slot["trial_id"]]
        if len(trial_start) != 1 or any(trial_start[0]["metadata"].get(k) != slot[k] for k in ("task_id", "split", "arm", "seed")):
            raise RecoveryError("collection start metadata differs from its frozen slot")
        final_path, report_path = directory / "final-evaluation.json", directory / "result.json"
        if (len(terminal) != 1 or terminal[0]["status"] != "completed"
                or _sha(final_path.read_bytes()) != terminal[0]["evidence"]["final_evaluation_sha256"]):
            raise RecoveryError("collection terminal/final evidence seal differs")
        final, report = _read(final_path), _read(report_path)
        if (_sha(report_path.read_bytes()) != final["runtime_result_sha256"]
                or report["task_id"] != slot["task_id"] or report["split"] != slot["split"]
                or final["task_id"] != slot["task_id"] or final["split"] != slot["split"]):
            raise RecoveryError("collection report/task/final binding differs")
        task_rows = [r for r in records.values() if r["trial_id"] == slot["trial_id"]]
        if (any(r["status"] != "settled" or r.get("policy_violation") for r in task_rows)
                or final["budget"]["requests"] != len(task_rows)
                or final["budget"]["used_tokens"] != sum(r["used_tokens"] for r in task_rows)):
            raise RecoveryError("collection usage does not match the durable journal")
        tasks[slot["task_id"]].verify()
        trace_path = directory / "trace.json"
        replayable = report["status"] == "completed" and report["trace_sha256"] is not None
        if replayable:
            if not trace_path.is_file() or _sha(trace_path.read_bytes()) != report["trace_sha256"]:
                raise RecoveryError("replayable trace seal differs")
            validate_trace(_read(trace_path))
            available.append(SimpleNamespace(report=report, trace_path=trace_path))
        elif trace_path.exists() or report["trace_sha256"] is not None:
            raise RecoveryError("unavailable trace must not be synthesized")
        coverage.append({"trial_id": slot["trial_id"], "task_id": slot["task_id"], "split": slot["split"],
            "host_terminal": terminal[0]["status"], "runtime_status": report["status"], "replayable": replayable,
            "original_final": final.get("final"), "original_final_success": final.get("final_success"),
            "original_selected_visible_score": report.get("selected_visible_score"),
            "original_selected_candidate": report.get("selected_candidate"),
            "original_attempts": report.get("attempts"), "usage": {"requests": len(task_rows),
                "used_tokens": sum(r["used_tokens"] for r in task_rows), "currency": None, "cost": None},
            "report_sha256": _sha(report_path.read_bytes()), "final_evaluation_sha256": _sha(final_path.read_bytes()),
            "trace_sha256": report["trace_sha256"]})
    train = [c for c in coverage if c["split"] == "train"]
    holdout = [c for c in coverage if c["split"] == "val"]
    missing = [c for c in coverage if not c["replayable"]]
    if (len(train) != 3 or not all(c["replayable"] for c in train)
            or len(holdout) != 2 or sum(c["replayable"] for c in holdout) != 1
            or len(missing) != 1 or missing[0]["task_id"] != "HumanEval/32"
            or missing[0]["runtime_status"] != "invalid_attempt"
            or missing[0]["original_final"] is not None or missing[0]["original_selected_candidate"] is not None
            or not missing[0]["original_attempts"]
            or missing[0]["original_attempts"][-1]["agent_terminal"]["stop_reason"] != "budget_exhausted"):
        raise RecoveryError("r1 requires complete 3/3 train and the explicit 1/2 available holdout")
    partial = campaign.root / "dataset"
    expected_partial = {f"trace-{i}.json": _sha(result.trace_path.read_bytes())
                        for i, result in enumerate(available[:3])}
    if not partial.is_dir() or _inventory(partial) != expected_partial:
        raise RecoveryError("original partial export differs; never repair or overwrite it")
    return plan, tasks, coverage, available, starts[0], summary


def inspect_recovery(campaign_root):
    """Read-only eligibility check; no SDK search, credential access or mutation."""
    campaign = Campaign(campaign_root)
    if any(r["event"] == "operator_recovery" for r in campaign.events()):
        raise RecoveryError("a recovery already started; only its sealed commit may be resumed")
    plan, _, coverage, _, marker, summary = _collection(campaign)
    return {"recovery_id": RECOVERY_ID, "script_sha256": _script_hash(), "coverage": coverage,
        "coverage_counts": {"train": [3, 3], "holdout": [1, 2], "test_started": 0},
        "original_optimizer_marker": {"sequence": marker["sequence"], "sha256": marker["sha256"]},
        "original_journal_head": summary["journal_sha256"], "requests": summary["requests"],
        "used_tokens": summary["used_tokens"], "currency": None, "cost": None,
        "action_budgets": plan["optimization"]["budgets"], "budget_unit": "recorded scored-tree node actions, not LLM calls/tokens",
        "publication_status": PUBLICATION_STATUS, "validation_gain": None, "incumbent_gain": None}


def _train_only_choice(result, max_candidates):
    choices = [{"role": "baseline", "parameters": None, "policy_hash": policy_identity(None)}, *optimizer.candidate_grid(max_candidates)]
    scores = result["train_scores"]
    if set(scores) != {c["role"] for c in choices}:
        raise RecoveryError("training score inventory differs")
    selected = min(choices, key=lambda p: (-scores[p["role"]], p["role"] != "baseline",
        (p["parameters"] or {}).get("max_width", 0), (p["parameters"] or {}).get("max_refine", 0)))
    if selected != result["selected"]:
        raise RecoveryError("SDK choice differs from independent train-only tie ordering")
    return selected


def _verify_unchanged(campaign, output, inputs):
    campaign.assert_execution_pin()
    verify_plan(campaign)
    if inputs["script_sha256"] != _script_hash() or inputs["sdk_fingerprint"] != _sdk_fingerprint():
        raise RecoveryError("recovery script or SDK changed")
    originals = inputs["original_files"]
    actual = _inventory(campaign.root, skip=("journal.jsonl", "research-policy.json"))
    if actual != {k: v for k, v in originals.items() if k != "journal.jsonl"}:
        raise RecoveryError("an original campaign artifact changed")
    before = (output / "original-journal.jsonl").read_bytes()
    if _sha(before) != originals["journal.jsonl"] or not campaign.path.read_bytes().startswith(before):
        raise RecoveryError("original journal prefix changed")
    tail = campaign.events()[inputs["original_event_count"]:]
    if any(row["event"] != "operator_recovery" or row.get("recovery_id") != RECOVERY_ID
           or row.get("output") != str(output) for row in tail):
        raise RecoveryError("unrelated journal activity after recovery began")
    started = [r for r in tail if r.get("phase") == "started"]
    if (len(started) != 1 or started[0]["input_manifest_sha256"] != _sha((output / "inputs.json").read_bytes())
            or started[0]["full_coverage_sha256"] != _sha((output / "full-collection-coverage.json").read_bytes())
            or inputs["campaign_root"] != str(campaign.root)):
        raise RecoveryError("recovery input/coverage differs from its original registered search")
    if campaign.summary()["blocked"] or len(campaign.summary()["trials"]) != 5:
        raise RecoveryError("new unknown or trial appeared during recovery")


def complete_commit(campaign_root, output):
    """Commit only a previously sealed candidate; never rerun optimization."""
    campaign, output = Campaign(campaign_root), Path(output).resolve()
    inputs, seal = _read(output / "inputs.json"), _read(output / "seal.json")
    _verify_unchanged(campaign, output, inputs)
    if _inventory(output, skip=("seal.json",)) != seal["files"]:
        raise RecoveryError("sealed recovery artifact changed")
    prepared = [r for r in campaign.events() if r["event"] == "operator_recovery" and r.get("phase") == "prepared"]
    if (len(prepared) != 1 or prepared[0]["seal_sha256"] != _sha((output / "seal.json").read_bytes())):
        raise RecoveryError("recovery lacks its unique prepared seal")
    policy = _read(output / "policy-candidate.json")
    path = campaign.root / "research-policy.json"
    if path.exists():
        if path.read_bytes() != _bytes(policy) + b"\n":
            raise RecoveryError("existing policy differs; never overwrite it")
    else:
        _exclusive_json(path, policy)
        _sync_directory(campaign.root)
    def check(trials, requests):
        if (set(trials) != set(inputs["collection_trial_ids"]) or any(t["status"] != "completed" for t in trials.values())
                or Campaign._blocked(requests)
                or any(r["event"] == "optimizer_finished" for r in campaign._read())):
            raise RecoveryError("campaign changed before final commit")
    campaign._append("optimizer_finished", check=check, policy_sha256=_sha(_bytes(policy)), policy=policy,
                     operator_recovery_id=RECOVERY_ID, recovery_seal_sha256=_sha((output / "seal.json").read_bytes()))
    return {"status": "committed", "policy": policy, "recovery_output": str(output)}


def recover(campaign_root, output):
    """One actual offline SDK search; never invoke this on a paid run implicitly."""
    campaign, output = Campaign(campaign_root), Path(output).resolve()
    if output.exists() or output.is_relative_to(campaign.root) or campaign.root.is_relative_to(output):
        raise RecoveryError("recovery output must be new and separate from the campaign")
    inspection = inspect_recovery(campaign.root)
    plan, tasks, coverage, available, _, _ = _collection(campaign)
    sdk = _sdk_fingerprint()
    output.mkdir(parents=True, exist_ok=False)
    inputs = {"recovery_id": RECOVERY_ID, "campaign_root": str(campaign.root), "script_sha256": _script_hash(),
        "sdk_fingerprint": sdk, "original_files": _inventory(campaign.root),
        "original_event_count": len(campaign.events()), "collection_trial_ids": [c["trial_id"] for c in coverage],
        "source_pin_sha256": _sha(_bytes(campaign.manifest["execution_source"])),
        "frozen_plan_sha256": _sha(_bytes(plan)), "task_manifest_sha256": plan["task_manifest_sha256"]}
    _exclusive_json(output / "inputs.json", inputs)
    _exclusive_json(output / "full-collection-coverage.json", inspection)
    with (output / "original-journal.jsonl").open("xb") as stream:
        stream.write(campaign.path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    splits = TaskSplits(*(tuple(t.task_id for t in tasks.values() if t.split == s) for s in ("train", "val", "test")))
    manifest = export_dataset(available, splits, output / "available-tree-projection")
    def start_check(trials, requests):
        if (any(r["event"] == "operator_recovery" for r in campaign._read())
                or set(trials) != set(inputs["collection_trial_ids"])
                or any(t["status"] != "completed" for t in trials.values()) or Campaign._blocked(requests)
                or _sha(campaign.path.read_bytes()) != inputs["original_files"]["journal.jsonl"]):
            raise RecoveryError("another recovery/trial started before search registration")
    campaign._append("operator_recovery", check=start_check, recovery_id=RECOVERY_ID, phase="started", output=str(output),
        input_manifest_sha256=_sha((output / "inputs.json").read_bytes()),
        full_coverage_sha256=_sha((output / "full-collection-coverage.json").read_bytes()),
        original_failure="operator-reported export_dataset: unverified trace or task split; no original SDK search",
        registration="after collection failure, before first test; exploratory r1, not original full validation")
    # A crash/error from here is an attempted recovery: another output/search is forbidden.
    result = optimizer.optimize(manifest, output / "optimizer", registry_path=output / "private-registry",
        budgets=tuple(plan["optimization"]["budgets"]), max_candidates=plan["optimization"]["max_candidates"],
        dry_run=True, max_workers=1, timeout_seconds=30, max_seconds=300)
    _exclusive_json(output / "sdk-return.json", result)
    projection = _read(manifest)
    if result["status"] != "completed" or result["kind"] != projection["kind"] or result["promoted"] is not False:
        raise RecoveryError("SDK search must finish as same-kind nonpublishing dry run")
    chosen = _train_only_choice(result, plan["optimization"]["max_candidates"])
    fallback = chosen["role"] == "baseline"
    parameters = {"max_width": 2, "max_refine": 1} if fallback else chosen["parameters"]
    policy = {"parameters": parameters, "policy_hash": policy_identity(parameters), "kind": result["kind"], "promoted": False,
        "selection": "single_train_selected_candidate", "selected_role": chosen["role"],
        "raw_sdk_selected": chosen,
        "selected_parameters": chosen["parameters"], "selected_sdk_policy_hash": chosen["policy_hash"],
        "C_condition": "reference_fallback_not_trained_intervention" if fallback else "train_selected_exploratory_intervention",
        "online_parameter_rule": "online_reference_fallback" if fallback else "freeze_train_selected_parameters",
        "publication_status": PUBLICATION_STATUS, "validation_gain": None, "incumbent_gain": None,
        "validation_coverage": {"replayable": 1, "total": 2, "unavailable": ["HumanEval/32"]},
        "research_rule": plan["optimization"]["research_C"], "recovery_id": RECOVERY_ID,
        "optimizer_summary_sha256": _sha((output / "sdk-return.json").read_bytes())}
    _exclusive_json(output / "policy-candidate.json", policy)
    _exclusive_json(output / "selection-audit.json", {"train_scores": result["train_scores"],
        "selected": chosen, "independent_train_only_sort_verified": True,
        "selector_source_sha256": _sha(inspect.getsource(optimizer._select).encode()),
        "partial_holdout_raw_diagnostic_only": result["holdout_scores"], "full_validation_available": False,
        "action_budgets": plan["optimization"]["budgets"], "budget_unit": inspection["budget_unit"],
        "baseline_fallback_is_not_claimed_equivalent_to_SDK_default": fallback})
    _verify_unchanged(campaign, output, inputs)
    for path in output.rglob("*"):
        if path.is_file() and not path.name.endswith(".lock"):
            with path.open("rb+") as stream:
                os.fsync(stream.fileno())
    for directory in sorted((p for p in output.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        _sync_directory(directory)
    _exclusive_json(output / "seal.json", {"files": _inventory(output), "input_manifest_sha256": _sha((output / "inputs.json").read_bytes())})
    _sync_directory(output)
    _sync_directory(output.parent)
    campaign._append("operator_recovery", recovery_id=RECOVERY_ID, phase="prepared", output=str(output),
        seal_sha256=_sha((output / "seal.json").read_bytes()), policy_sha256=_sha(_bytes(policy)))
    return complete_commit(campaign.root, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "recover", "complete-commit"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = inspect_recovery(args.campaign)
    elif args.output is None:
        parser.error("--output required for recovery/commit")
    else:
        result = (recover if args.command == "recover" else complete_commit)(args.campaign, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
