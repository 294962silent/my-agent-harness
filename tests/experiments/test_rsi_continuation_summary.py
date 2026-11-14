"""Synthetic journal/seal fixtures; no paid execution or task content."""

import hashlib
import json

import pytest

from experiments.context_rsi.summarize_continuation import summarize_continuation
from experiments.context_rsi.analyze_live import AnalysisError


def blob(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob(value) + b"\n")


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path / "campaign"
    incident = tmp_path / "incident"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text("# synthetic no execution\n")
    campaign = {
        "schema_version": 1,
        "campaign_id": "rsi-live-20261114-v2",
        "execution_source": {"fixture.py": "a" * 64},
    }
    slots = [
        {
            "trial_id": f"collect-{n}",
            "phase": "collection",
            "task_id": f"HumanEval/{n}",
            "split": split,
            "seed": 23,
            "arm": "B",
        }
        for n, split in [(7, "train"), (11, "train"), (27, "train"), (32, "val"), (45, "val")]
    ]
    slots += [
        {
            "trial_id": f"test-HumanEval-{n}-s{seed}-{arm}",
            "phase": "test",
            "task_id": f"HumanEval/{n}",
            "split": "test",
            "seed": seed,
            "arm": arm,
        }
        for n in (1, 6, 9, 20, 33, 40)
        for seed in (23, 47)
        for arm in ("A", "B", "C", "random")
    ]
    plan = {"schema_version": 1, "slots": slots, "task_manifest_sha256": "b" * 64}
    write(root / "campaign.json", campaign)
    write(root / "plan.json", plan)
    write(root / "research-policy.json", {"promoted": False})
    failed = "test-HumanEval-6-s23-C"
    write(root / "trials" / failed / "result.json", {"status": "invalid", "error_type": "PermissionError"})
    report_hash = sha((root / "trials" / failed / "result.json").read_bytes())
    write(
        root / "trials" / failed / "final-evaluation.json",
        {"runtime_result_sha256": report_hash, "final": None, "final_success": False},
    )
    rows = []

    def append(event, **data):
        row = {"event": event, "manifest_sha256": sha(blob(campaign)), **data}
        rows.append(row)

    append("campaign_frozen")
    append("plan_frozen", plan_sha256=sha(blob(plan)))
    for slot in slots[:16]:
        append("trial_started", trial_id=slot["trial_id"], metadata=slot)
        append(
            "trial_finished",
            trial_id=slot["trial_id"],
            status="failed" if slot["trial_id"] == failed else "completed",
            evidence={"final_evaluation_sha256": sha((root / "trials" / failed / "final-evaluation.json").read_bytes())}
            if slot["trial_id"] == failed
            else {},
        )

    def save():
        prior = None
        for index, row in enumerate(rows):
            row.update(sequence=index, previous=prior)
            row["sha256"] = sha(blob({k: v for k, v in row.items() if k != "sha256"}))
            prior = row["sha256"]
        (root / "journal.jsonl").write_bytes(b"".join(blob(row) + b"\n" for row in rows))

    save()
    pending = [s["trial_id"] for s in slots[16:]]
    pins = {
        name: sha((root / name).read_bytes())
        for name in [
            "campaign.json",
            "plan.json",
            "research-policy.json",
            f"trials/{failed}/result.json",
            f"trials/{failed}/final-evaluation.json",
        ]
    }
    value = {
        "schema_version": 1,
        "incident_id": "permissionerror-operational-exception-r1",
        "campaign_root": str(root.resolve()),
        "script_sha256": sha(wrapper.read_bytes()),
        "original_event_count": len(rows),
        "original_journal_sha256": sha((root / "journal.jsonl").read_bytes()),
        "original_journal_head": rows[-1]["sha256"],
        "failed_slot": failed,
        "failed_terminal_sha256": rows[-1]["sha256"],
        "original_started_ids": sorted(s["trial_id"] for s in slots[:16]),
        "pending_trial_ids": pending,
        "pending_ids_sha256": sha(blob(pending)),
        "pins": pins,
        "source_pin_sha256": sha(blob(campaign["execution_source"])),
        "task_manifest_sha256": "b" * 64,
        "original_requests": 0,
        "original_known_tokens": 0,
    }
    write(incident / "incident.json", value)
    (incident / "original-journal.jsonl").write_bytes((root / "journal.jsonl").read_bytes())
    seal = {
        "schema_version": 1,
        "files": {name: sha((incident / name).read_bytes()) for name in ["incident.json", "original-journal.jsonl"]},
    }
    write(incident / "seal.json", seal)
    append(
        "operator_continuation",
        phase="registered",
        incident_id=value["incident_id"],
        incident_seal_sha256=sha((incident / "seal.json").read_bytes()),
        script_sha256=value["script_sha256"],
        pending_ids_sha256=value["pending_ids_sha256"],
    )
    save()
    return root, incident, wrapper, slots, rows, append, save


def test_exact_receipt_original37_and_failed_denominator_preserved(evidence):
    root, incident, wrapper, *_ = evidence
    result = summarize_continuation(root, incident, wrapper_path=wrapper)
    assert result["denominator"]["planned"] == 53 and result["denominator"]["original_failed"] == 1
    assert result["continuation"]["original_pending_count"] == 37
    assert result["continuation"]["started"] == 0 and not result["complete"]
    assert result["original_failed"]["status"] == "failed"


@pytest.mark.parametrize("target", ["script", "prefix", "incident", "failed_result"])
def test_drift_rejected_without_repair(evidence, target):
    root, incident, wrapper, *_ = evidence
    path = {
        "script": wrapper,
        "prefix": incident / "original-journal.jsonl",
        "incident": incident / "incident.json",
        "failed_result": root / "trials" / "test-HumanEval-6-s23-C" / "result.json",
    }[target]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(AnalysisError):
        summarize_continuation(root, incident, wrapper_path=wrapper)


def test_new_completion_adds_coverage_without_rewriting_original_failure(evidence):
    root, incident, wrapper, slots, _, append, save = evidence
    slot = slots[16]
    append("trial_started", trial_id=slot["trial_id"], metadata=slot)
    append("trial_finished", trial_id=slot["trial_id"], status="completed", evidence={})
    save()
    result = summarize_continuation(root, incident, wrapper_path=wrapper)
    assert result["continuation"]["completed"] == 1 and result["continuation"]["unstarted"] == 36
    assert result["denominator"]["original_failed"] == 1


def test_diagnostic_payload_never_copied(evidence):
    root, incident, wrapper, _, rows, append, save = evidence
    registration = rows[-1]
    append(
        "operator_continuation",
        phase="diagnostic",
        incident_id=registration["incident_id"],
        incident_seal_sha256=registration["incident_seal_sha256"],
        script_sha256=registration["script_sha256"],
        pending_ids_sha256=registration["pending_ids_sha256"],
        diagnostic={
            "error_type": "PermissionError",
            "message": "fixture-secret",
            "filename_relative": "fixture-secret-file",
        },
    )
    save()
    report = summarize_continuation(root, incident, wrapper_path=wrapper)
    assert "fixture-secret" not in json.dumps(report)
    assert report["operator_events"][-1]["diagnostic_type"] == "PermissionError"


def test_all37_completed_still_keeps_original_failed_in_full53(evidence):
    root, incident, wrapper, slots, _, append, save = evidence
    for slot in slots[16:]:
        append("trial_started", trial_id=slot["trial_id"], metadata=slot)
        append("trial_finished", trial_id=slot["trial_id"], status="completed", evidence={})
    save()
    result = summarize_continuation(root, incident, wrapper_path=wrapper)
    assert result["complete"] and result["authorization_continuation_clean"]
    assert result["denominator"]["terminal"] == 53 and result["denominator"]["original_failed"] == 1
    assert result["continuation"]["completed"] == 37 and result["continuation"]["unstarted"] == 0


def test_unplanned_new_id_is_rejected(evidence):
    root, incident, wrapper, _, _, append, save = evidence
    append("trial_started", trial_id="alternate-id", metadata={})
    save()
    with pytest.raises(AnalysisError, match="outside"):
        summarize_continuation(root, incident, wrapper_path=wrapper)


def test_additional_failure_is_reported_without_absorbing_it_into_exception(evidence):
    root, incident, wrapper, slots, _, append, save = evidence
    trial = slots[16]["trial_id"]
    append("trial_started", trial_id=trial, metadata=slots[16])
    append("trial_finished", trial_id=trial, status="failed", evidence={})
    save()
    result = summarize_continuation(root, incident, wrapper_path=wrapper)
    assert not result["authorization_continuation_clean"] and not result["complete"]
    assert result["continuation"]["additional_failed_ids"] == [trial]
    assert result["original_failed"]["status"] == "failed"
