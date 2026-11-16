"""Synthetic sealed snapshots only; no model, official tests or candidate code."""

import hashlib
import json

import pytest

from experiments.context_rsi.analyze_live import AnalysisError, analyze_campaign, main


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


POLICY = "33030146a9b9f6f49b71087c77834e0f0542611ebf881ceb2d4c5370e996adb8"
BASELINE = digest(
    canonical({"schema_version": 1, "source_commit": "8ec35bd2538220517ce1bfca4282db21b8c518db", "parameters": None})
)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "campaign"
    limits = {"max_requests": 64, "max_tokens": 1000000, "max_seconds": 1800, "max_evaluations": 6}
    campaign = {
        "schema_version": 1,
        "campaign_id": "synthetic-fixture",
        "protocol_id": "fixture-v2",
        "attempt_max_turns": 12,
        "limits": limits,
        "model": {"model": "synthetic-no-provider"},
        "execution_source": {"fixture.py": "a" * 64},
    }
    slots = [
        {
            "trial_id": f"collect-{n}",
            "phase": "collection",
            "task_id": f"HumanEval/{n}",
            "split": "train" if n in (7, 11, 27) else "val",
            "arm": "B",
            "seed": 23,
        }
        for n in (7, 11, 27, 32, 45)
    ]
    slots += [
        {
            "trial_id": f"test-{n}-{seed}-{arm}",
            "phase": "test",
            "task_id": f"HumanEval/{n}",
            "split": "test",
            "arm": arm,
            "seed": seed,
        }
        for n in (1, 6, 9, 20, 33, 40)
        for seed in (23, 47)
        for arm in ("A", "B", "C", "random")
    ]
    plan = {
        "schema_version": 1,
        "slots": slots,
        "tasks": {
            s["task_id"]: {"split": s["split"], "source_sha": "b" * 64, "host_evaluator_sha": "c" * 64} for s in slots
        },
        "task_manifest_sha256": "d" * 64,
        "optimization": {"budgets": [1, 2, 4]},
    }
    policy = {
        "parameters": {"max_width": 2, "max_refine": 1},
        "policy_hash": POLICY,
        "raw_sdk_selected": {"role": "baseline", "parameters": None, "policy_hash": BASELINE},
        "selected_parameters": None,
        "selected_sdk_policy_hash": BASELINE,
        "C_condition": "reference_fallback_not_trained_intervention",
        "promoted": False,
        "publication_status": "validation_incomplete_rejected_for_publication",
        "validation_gain": None,
        "incumbent_gain": None,
        "validation_coverage": {"replayable": 1, "total": 2, "unavailable": ["HumanEval/32"]},
    }
    write(root / "campaign.json", campaign)
    write(root / "plan.json", plan)
    write(root / "research-policy.json", policy)
    rows = []
    manifest_sha = digest(canonical(campaign))

    def append(event, **values):
        row = {
            "sequence": len(rows),
            "previous": rows[-1]["sha256"] if rows else None,
            "manifest_sha256": manifest_sha,
            "event": event,
            **values,
        }
        row["sha256"] = digest(canonical(row))
        rows.append(row)

    append("campaign_frozen")
    append("plan_frozen", plan_sha256=digest(canonical(plan)))
    provenance = {
        "kind": "synthetic",
        "model": "synthetic-no-provider",
        "client_class": "fixture.Client",
        "provider_class": "fixture.Provider",
        "client_source_sha256": "e" * 64,
        "provider_source_sha256": "f" * 64,
    }
    for index, slot in enumerate(slots):
        if index == 5:
            append("optimizer_started", collection_trials=[s["trial_id"] for s in slots[:5]])
            append("optimizer_finished", policy=policy, policy_sha256=digest(canonical(policy)))
        frozen = {"parameters": {"max_width": 2, "max_refine": 1}, "policy_hash": POLICY, "registry_version": None}
        metadata = {
            "task_id": slot["task_id"],
            "split": slot["split"],
            "arm": slot["arm"],
            "seed": slot["seed"],
            "mode": "baseline" if slot["arm"] == "A" else "layered",
            "schedule": "random" if slot["arm"] == "random" else "rsi" if slot["arm"] == "C" else "fixed",
            "source_sha": "b" * 64,
            "host_evaluator_sha": "c" * 64,
            "policy": frozen,
        }
        append("trial_started", trial_id=slot["trial_id"], metadata=metadata)
        usage = {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
        record = {
            "request": 1,
            "purpose": "agent",
            "status": "pending",
            "reserved_tokens": 100,
            "used_tokens": None,
            "usage": None,
        }
        request_id = slot["trial_id"] + ":1"
        append("request_reserved", trial_id=slot["trial_id"], request_id=request_id, record=dict(record))
        record.update(status="settled", used_tokens=10, input_tokens=8, output_tokens=2, usage=usage)
        append(
            "request_settled", trial_id=slot["trial_id"], request_id=request_id, record=dict(record), raw_usage=usage
        )
        budget = {
            "requests": 1,
            "used_tokens": 10,
            "held_tokens": 0,
            "usage_complete": True,
            "blocked": False,
            "limits": limits,
            "evaluations": 2,
            "wall_seconds": 1.0,
        }
        actions = [
            {"event": event, "tool_name": "read", "tool_call_id": "fixture-id"}
            for event in ("ToolExecutionStart", "ToolExecutionEnd")
        ]
        result = {
            "task_id": slot["task_id"],
            "split": slot["split"],
            "arm": "host",
            "status": "completed",
            "budget": budget,
            "policy": frozen,
            "provenance": provenance,
            "provider_requests": [record],
            "attempts": [
                {
                    "valid": True,
                    "stage": "completed",
                    "agent_terminal": {"stop_reason": "end_turn", "iterations": 1},
                    "tool_schema_sha256": "1" * 64,
                    "actions": actions,
                    "context": {"mode": metadata["mode"]},
                    "context_activity": {"phase_fold_views": 1, "micro_groups": 2},
                }
            ],
            "planned_attempts": 4,
            "valid_attempts": 1,
            "invalid_attempts": 0,
            "not_dispatched_attempts": 3,
            "selected_candidate": "b0_a0",
            "selected_visible_score": 1.0,
            "trace_sha256": None,
            "cleanup_errors": [],
        }
        directory = root / "trials" / slot["trial_id"]
        write(directory / "result.json", result)
        final = {
            "trial_id": slot["trial_id"],
            "task_id": slot["task_id"],
            "split": slot["split"],
            "protocol_id": "fixture-v2",
            "attempt_max_turns": 12,
            "agent_task_status": "completed",
            "episode_execution_status": "completed",
            "execution_integrity": True,
            "selection_frozen": True,
            "final": {"valid": True, "score": 1.0, "exit_code": 0},
            "final_success": True,
            "provenance": provenance,
            "final_evaluator_id": "c" * 64,
            "budget": budget,
            "runtime_result_sha256": digest((directory / "result.json").read_bytes()),
        }
        write(directory / "final-evaluation.json", final)
        append(
            "trial_finished",
            trial_id=slot["trial_id"],
            status="completed",
            evidence={
                "final_evaluation_sha256": digest((directory / "final-evaluation.json").read_bytes()),
                "budget": budget,
            },
        )

    def save():
        previous = None
        for index, row in enumerate(rows):
            row.update(sequence=index, previous=previous)
            row["sha256"] = digest(canonical({k: v for k, v in row.items() if k != "sha256"}))
            previous = row["sha256"]
        (root / "journal.jsonl").write_bytes(b"".join(canonical(r) + b"\n" for r in rows))

    save()

    def reseal(slot_index):
        slot = slots[slot_index]
        directory = root / "trials" / slot["trial_id"]
        final = json.loads((directory / "final-evaluation.json").read_text())
        final["runtime_result_sha256"] = digest((directory / "result.json").read_bytes())
        write(directory / "final-evaluation.json", final)
        next(r for r in rows if r["event"] == "trial_finished" and r["trial_id"] == slot["trial_id"])["evidence"][
            "final_evaluation_sha256"
        ] = digest((directory / "final-evaluation.json").read_bytes())
        save()

    return root, slots, rows, save, reseal


def test_complete_pairing_and_fallback_cannot_be_learning_attribution(snapshot):
    root, _, _, _, _ = snapshot
    result = analyze_campaign(root)
    assert result["test"]["complete"]
    assert len(result["comparisons"]["paired_rows"]) == 12
    assert len(result["comparisons"]["task_aggregates"]) == 6
    assert result["C_interpretation"]["learning_contribution"] == "not_identifiable"
    assert result["costs"]["full_total"] is None  # auxiliary records unavailable, never hardcoded
    assert all(a["planned"] == 12 for a in result["test"]["by_arm"].values())


def test_unrun_slots_only_coverage_and_no_comparisons(snapshot):
    root, _, rows, save, _ = snapshot
    rows[:] = rows[
        : next(i for i, r in enumerate(rows) if r["event"] == "trial_started" and r["trial_id"].startswith("test-"))
    ]
    save()
    result = analyze_campaign(root)
    assert not result["test"]["complete"] and result["comparisons"]["paired_rows"] is None
    assert result["test"]["by_arm"]["A"]["planned"] == 12


def test_unavailable_final_is_not_imputed_as_zero(snapshot):
    root, slots, _, _, reseal = snapshot
    directory = root / "trials" / slots[5]["trial_id"]
    path = directory / "final-evaluation.json"
    value = json.loads(path.read_text())
    value.update(final=None, final_success=False)
    write(path, value)
    reseal(5)
    result = analyze_campaign(root)
    row = next(r for r in result["rows"] if r["trial_id"] == slots[5]["trial_id"])
    assert row["final_pass"] is None and row["final_valid"] is None and row["agent_success"] is False
    assert result["comparisons"]["final_grade_pairs"] is None


def test_deleted_sealed_final_is_rejected_not_zero(snapshot):
    root, slots, _, _, _ = snapshot
    (root / "trials" / slots[5]["trial_id"] / "final-evaluation.json").unlink()
    with pytest.raises(AnalysisError):
        analyze_campaign(root)


def test_tool_schema_mismatch_preserves_all_rows_but_blocks_pairing(snapshot):
    root, slots, _, _, reseal = snapshot
    p = root / "trials" / slots[5]["trial_id"] / "result.json"
    d = json.loads(p.read_text())
    d["attempts"][0]["tool_schema_sha256"] = "9" * 64
    write(p, d)
    reseal(5)
    result = analyze_campaign(root)
    assert len(result["rows"]) == 53 and result["comparisons"]["status"] == "schema_mismatch"
    assert result["comparisons"]["paired_rows"] is None


def test_cross_protocol_schema_rejected(snapshot):
    root, slots, _, _, reseal = snapshot
    p = root / "trials" / slots[5]["trial_id"] / "final-evaluation.json"
    d = json.loads(p.read_text())
    d["protocol_id"] = "other"
    write(p, d)
    reseal(5)
    with pytest.raises(AnalysisError, match="schema"):
        analyze_campaign(root)


def test_corrupt_hash_rejected(snapshot):
    root, slots, _, _, _ = snapshot
    p = root / "trials" / slots[5]["trial_id"] / "result.json"
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(AnalysisError, match="hash"):
        analyze_campaign(root)


def test_duplicate_slot_key_rejected(snapshot):
    root, _, _, _, _ = snapshot
    p = root / "plan.json"
    d = json.loads(p.read_text())
    d["slots"][-1] = dict(d["slots"][-2])
    write(p, d)
    with pytest.raises(AnalysisError):
        analyze_campaign(root)


def test_duplicate_json_key_rejected(snapshot):
    root, _, _, _, _ = snapshot
    p = root / "plan.json"
    p.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(AnalysisError, match="duplicate"):
        analyze_campaign(root)


def test_cache_missing_not_zero_and_context_values_are_snapshot_counts(snapshot):
    result = analyze_campaign(snapshot[0])
    row = result["rows"][0]
    assert row["usage"]["cache_read_field_coverage"] == 0 and row["usage"]["cache_read_tokens"] is None
    assert row["context_activity"]["micro_groups"] == 2
    assert (
        result["measurement_notes"]["context_activity_unit"] == "snapshot-observed views/groups, not operation counts"
    )


def test_cli_requires_complete_by_default_and_never_overwrites(snapshot, tmp_path):
    root, _, rows, save, _ = snapshot
    rows[:] = rows[:2]
    save()
    (root / "research-policy.json").unlink()
    out = tmp_path / "safe-output"
    assert main(["--root", str(root), "--output", str(out)]) == 2
    assert main(["--root", str(root), "--output", str(out), "--allow-incomplete"]) == 1
    assert main(["--root", str(root), "--output", str(tmp_path / "other"), "--allow-incomplete"]) == 0


def auxiliary_records(tmp_path):
    root = tmp_path / "old-calibration"
    campaign = {"schema_version": 1, "campaign_id": "old-synthetic-calibration"}
    write(root / "campaign.json", campaign)
    rows = []

    def append(event, **data):
        value = {
            "event": event,
            "sequence": len(rows),
            "previous": rows[-1]["sha256"] if rows else None,
            "manifest_sha256": digest(canonical(campaign)),
            **data,
        }
        value["sha256"] = digest(canonical(value))
        rows.append(value)

    append("campaign_frozen")
    append("trial_started", trial_id="calibrate", metadata={})
    for request in range(1, 5):
        record = {"request": request, "status": "pending", "reserved_tokens": 3000, "usage": None, "used_tokens": None}
        append("request_reserved", trial_id="calibrate", request_id=f"calibrate:{request}", record=dict(record))
        usage = {"prompt_tokens": 2000, "completion_tokens": 124, "total_tokens": 2124}
        record.update(status="settled", input_tokens=2000, output_tokens=124, used_tokens=2124, usage=usage)
        append(
            "request_settled", trial_id="calibrate", request_id=f"calibrate:{request}", record=record, raw_usage=usage
        )
    append("trial_finished", trial_id="calibrate", status="completed", evidence={})
    (root / "journal.jsonl").write_bytes(b"".join(canonical(row) + b"\n" for row in rows))
    connectivity = tmp_path / "connectivity" / "request.json"
    write(
        connectivity,
        {
            "status": "settled",
            "reserved_tokens": 100,
            "input_tokens": 41,
            "output_tokens": 14,
            "total_tokens": 55,
            "usage": {"prompt_tokens": 41, "completion_tokens": 14, "total_tokens": 55},
        },
    )
    return root, connectivity


def test_auxiliary_costs_are_separate_and_actual_not_hidden(snapshot, tmp_path):
    calibration, connectivity = auxiliary_records(tmp_path)
    result = analyze_campaign(snapshot[0], calibration_root=calibration, connectivity_path=connectivity)
    costs = result["costs"]
    assert costs["collection_5"]["requests"] == 5 and costs["test_48"]["requests"] == 48
    assert costs["v1_calibration"]["usage"]["requests"] == 4
    assert costs["v1_calibration"]["usage"]["tokens"] == 8496
    assert costs["connectivity"]["usage"]["tokens"] == 55
    assert costs["full_total"]["requests"] == 58
    assert costs["full_total"]["tokens"] == 530 + 8496 + 55
    assert costs["sdk_recovery"]["counted_as_provider_calls"] is False
    assert costs["full_total"]["money"] is None


def test_partial_snapshot_cannot_label_observed_cost_as_full_total(snapshot, tmp_path):
    root, _, rows, save, _ = snapshot
    rows[:] = rows[
        : next(i for i, r in enumerate(rows) if r["event"] == "trial_started" and r["trial_id"].startswith("test-"))
    ]
    save()
    calibration, connectivity = auxiliary_records(tmp_path)
    result = analyze_campaign(root, calibration_root=calibration, connectivity_path=connectivity)
    assert result["costs"]["full_total"] is None
    assert result["costs"]["observed_total_so_far"]["requests"] == 10


def test_snapshot_change_detected_without_repair_or_input_write(snapshot, monkeypatch):
    from experiments.context_rsi import analyze_live

    root, _, _, _, _ = snapshot
    before = (root / "journal.jsonl").read_bytes()
    original = analyze_live._Reader.verify_snapshot

    def mutate_then_verify(reader):
        (root / "journal.jsonl").write_bytes(before + b" ")
        original(reader)

    monkeypatch.setattr(analyze_live._Reader, "verify_snapshot", mutate_then_verify)
    with pytest.raises(AnalysisError, match="changed"):
        analyze_campaign(root)
    assert (root / "journal.jsonl").read_bytes() == before + b" "  # no automatic repair


def test_policy_seal_drift_is_rejected(snapshot):
    root, _, _, _, _ = snapshot
    path = root / "research-policy.json"
    value = json.loads(path.read_text())
    value["parameters"]["max_refine"] = 0
    write(path, value)
    with pytest.raises(AnalysisError, match="policy"):
        analyze_campaign(root)


def test_analyzer_never_reads_prompt_transcript_or_candidate_files(snapshot, monkeypatch):
    from pathlib import Path

    root = snapshot[0]
    blocked = root / "trials" / "test-1-23-A" / "branches"
    blocked.mkdir()
    original = Path.read_bytes

    def checked(path):
        assert path.name in {
            "campaign.json",
            "plan.json",
            "journal.jsonl",
            "research-policy.json",
            "result.json",
            "final-evaluation.json",
        }
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", checked)
    assert analyze_campaign(root)["test"]["complete"]


def test_no_zero_imputation_for_pending_unknown_usage(snapshot):
    from experiments.context_rsi.analyze_live import _usage

    result = _usage(
        [
            {
                "record": {
                    "request": 1,
                    "status": "unknown",
                    "reserved_tokens": 99,
                    "error_type": "SyntheticProviderError",
                },
                "raw_usage": None,
            }
        ]
    )
    assert result["tokens"] is None and result["input_tokens"] is None and result["output_tokens"] is None
    assert result["unknown"] == 1 and result["held_tokens"] == 99
    assert result["complete_cache_hit_rate"] is None
    assert result["per_request"][0]["error_type"] == "SyntheticProviderError"


def test_sealed_host_failure_keeps_missing_attempt_observations_unavailable(snapshot):
    root, slots, rows, _, reseal = snapshot
    directory = root / "trials" / slots[5]["trial_id"]
    result = json.loads((directory / "result.json").read_text())
    result.update(status="invalid", valid_attempts=0, invalid_attempts=1)
    result["attempts"] = [{"valid": False, "stage": "started", "agent_terminal": None, "visible": None}]
    write(directory / "result.json", result)
    final = json.loads((directory / "final-evaluation.json").read_text())
    final.update(
        agent_task_status="invalid",
        episode_execution_status="failed",
        execution_integrity=False,
        final=None,
        final_success=False,
    )
    write(directory / "final-evaluation.json", final)
    next(r for r in rows if r["event"] == "trial_finished" and r["trial_id"] == slots[5]["trial_id"])["status"] = (
        "failed"
    )
    reseal(5)
    report = analyze_campaign(root)
    row = next(r for r in report["rows"] if r["trial_id"] == slots[5]["trial_id"])
    assert row["host_status"] == "failed" and row["attempt_invalid"] == 1
    assert row["context_activity"]["micro_groups"] is None and row["tool_events"] is None
    assert row["final_pass"] is None and report["comparisons"]["status"] == "schema_mismatch"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"prompt_tokens": 150, "cache_read_tokens": 50, "completion_tokens": 10, "total_tokens": 160}, 150),
        ({"prompt_tokens": 150, "completion_tokens": 10, "total_tokens": 160}, 150),
        (
            {
                "prompt_tokens": 80,
                "cache_read_tokens": 10,
                "cache_write_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
            100,
        ),
        ({"prompt_tokens": 80, "cache_read_tokens": 10, "cache_write_tokens": 20, "completion_tokens": 10}, 110),
        ({"completion_tokens": 10, "total_tokens": 160}, 150),
    ],
)
def test_authoritative_total_minus_completion_never_double_counts_cache(raw, expected):
    from experiments.context_rsi.analyze_live import _usage

    value = _usage(
        [
            {
                "record": {
                    "request": 1,
                    "status": "settled",
                    "reserved_tokens": 1000,
                    "input_tokens": expected,
                    "output_tokens": 10,
                    "used_tokens": expected + 10,
                },
                "raw_usage": raw,
            }
        ]
    )
    assert value["input_tokens"] == expected and value["tokens"] == expected + 10
    assert value["per_request"][0]["usage"] == raw
    if "cache_read_tokens" not in raw:
        assert value["complete_cache_hit_rate"] is None and value["cache_read_tokens"] is None


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt_tokens": 80, "completion_tokens": 10},
        {"prompt_tokens": 80, "cache_read_tokens": 10, "completion_tokens": 10},
        {"prompt_tokens": 80, "cache_write_tokens": 10, "completion_tokens": 10},
        {"total_tokens": 9, "completion_tokens": 10},
    ],
)
def test_incomplete_components_without_total_are_not_zero_imputed(raw):
    from experiments.context_rsi.analyze_live import _accounted_usage

    with pytest.raises(AnalysisError):
        _accounted_usage(raw)


def test_wrong_final_evaluator_identity_rejected_even_after_resealing(snapshot):
    root, slots, _, _, reseal = snapshot
    path = root / "trials" / slots[5]["trial_id"] / "final-evaluation.json"
    value = json.loads(path.read_text())
    value["final_evaluator_id"] = "9" * 64
    write(path, value)
    reseal(5)
    with pytest.raises(AnalysisError, match="identity"):
        analyze_campaign(root)


def test_test_data_cannot_precede_frozen_research_policy(snapshot):
    root, _, rows, save, _ = snapshot
    marker = next(r for r in rows if r["event"] == "optimizer_finished")
    rows.remove(marker)
    rows.append(marker)
    save()
    with pytest.raises(AnalysisError, match="policy was frozen"):
        analyze_campaign(root)
