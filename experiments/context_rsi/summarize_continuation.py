"""Independent read-only disclosure of the sealed operational continuation.

Reuses only the stdlib-only analyzer's JSON/hash readers. No execution controller,
agent, provider, candidate tree, transcript, official prompt or test is imported
or opened. This audits authorization metadata/counts, not per-trial quality.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from experiments.context_rsi.analyze_live import (
    AnalysisError,
    NAME,
    _Reader,
    _bytes,
    _hash as is_hash,
    _inventory,
    _journal,
    _require,
    _sha,
    _usage,
)


INCIDENT_ID = "permissionerror-operational-exception-r1"
FAILED_SLOT = "test-HumanEval-6-s23-C"


def summarize_continuation(root, incident_directory, *, wrapper_path=None):
    """Return only sealed IDs, hashes, typed statuses and numerical coverage."""
    root, sidecar = Path(root).resolve(), Path(incident_directory).resolve()
    wrapper_path = Path(wrapper_path).resolve() if wrapper_path else Path(__file__).with_name("continue_live.py")
    reader = _Reader()
    campaign, events, starts, ends, requests = _journal(reader, root)
    _require(campaign.get("campaign_id") == "rsi-live-20261114-v2", "wrong continuation campaign")
    plan = reader.json(root / "plan.json")
    slots = _inventory(plan)
    plan_markers = [e for e in events if e["event"] == "plan_frozen"]
    _require(len(plan_markers) == 1 and plan_markers[0].get("plan_sha256") == _sha(_bytes(plan)), "plan seal mismatch")
    seal = reader.json(sidecar / "seal.json")
    _require(
        seal.get("schema_version") == 1 and set(seal.get("files", {})) == {"incident.json", "original-journal.jsonl"},
        "incident seal schema mismatch",
    )
    for name, digest in seal["files"].items():
        _require(is_hash(digest) and _sha(reader.raw(sidecar / name)) == digest, "incident file hash mismatch")
    incident = reader.json(sidecar / "incident.json")
    _require(
        incident.get("schema_version") == 1
        and incident.get("incident_id") == INCIDENT_ID
        and incident.get("failed_slot") == FAILED_SLOT
        and incident.get("campaign_root") == str(root),
        "incident identity mismatch",
    )
    _require(_sha(reader.raw(wrapper_path)) == incident.get("script_sha256"), "continuation script hash mismatch")
    pins = incident.get("pins")
    allowed_pins = {
        "campaign.json",
        "plan.json",
        "research-policy.json",
        f"trials/{FAILED_SLOT}/result.json",
        f"trials/{FAILED_SLOT}/final-evaluation.json",
    }
    _require(type(pins) is dict and set(pins) == allowed_pins, "unexpected incident pin path")
    for name, digest in pins.items():
        _require(is_hash(digest) and _sha(reader.raw(root / name)) == digest, "original pinned artifact changed")
    _require(
        incident.get("source_pin_sha256") == _sha(_bytes(campaign["execution_source"]))
        and incident.get("task_manifest_sha256") == plan.get("task_manifest_sha256"),
        "source/task metadata pin differs",
    )
    prefix = reader.raw(sidecar / "original-journal.jsonl")
    _require(
        _sha(prefix) == incident.get("original_journal_sha256")
        and reader.raw(root / "journal.jsonl").startswith(prefix),
        "original journal prefix changed",
    )
    count = incident.get("original_event_count")
    _require(
        type(count) is int and count > 0 and len(prefix.splitlines()) == count and count <= len(events),
        "prefix count differs",
    )
    before = events[:count]
    _require(before[-1]["sha256"] == incident.get("original_journal_head"), "original journal head differs")
    old_starts = {e["trial_id"]: e for e in before if e["event"] == "trial_started"}
    old_ends = {e["trial_id"]: e for e in before if e["event"] == "trial_finished"}
    _require(
        len(old_starts) == 16
        and set(old_starts) == set(old_ends)
        and sorted(old_starts) == incident.get("original_started_ids"),
        "original 16-slot inventory differs",
    )
    _require(
        {trial for trial, e in old_ends.items() if e["status"] != "completed"} == {FAILED_SLOT}
        and old_ends[FAILED_SLOT]["status"] == "failed"
        and old_ends[FAILED_SLOT]["sha256"] == incident.get("failed_terminal_sha256"),
        "original failure is not preserved",
    )
    failed_result = reader.json(root / "trials" / FAILED_SLOT / "result.json")
    failed_final = reader.json(root / "trials" / FAILED_SLOT / "final-evaluation.json")
    _require(
        failed_result.get("status") == "invalid"
        and failed_result.get("error_type") == "PermissionError"
        and failed_final.get("final") is None
        and failed_final.get("final_success") is False
        and failed_final.get("runtime_result_sha256") == pins[f"trials/{FAILED_SLOT}/result.json"]
        and old_ends[FAILED_SLOT].get("evidence", {}).get("final_evaluation_sha256")
        == pins[f"trials/{FAILED_SLOT}/final-evaluation.json"],
        "failed result seal/type differs",
    )
    pending = incident.get("pending_trial_ids")
    expected_pending = [s["trial_id"] for s in slots if s["trial_id"] not in old_starts]
    _require(
        type(pending) is list
        and len(pending) == len(set(pending)) == 37
        and pending == expected_pending
        and incident.get("pending_ids_sha256") == _sha(_bytes(pending)),
        "original 37-slot set/order differs",
    )
    _require(all(s["phase"] == "test" for s in slots if s["trial_id"] in pending), "continuation repeats collection")
    tail = events[count:]
    operators = [e for e in tail if e["event"] == "operator_continuation"]
    receipts = [e for e in operators if e.get("phase") == "registered"]
    seal_sha = reader.hashes[sidecar / "seal.json"]
    _require(len(receipts) == 1 and tail and tail[0] == receipts[0], "missing/duplicate/late registration receipt")
    safe_events = []
    for event in tail:
        if event["event"] == "operator_continuation":
            _require(
                event.get("incident_id") == INCIDENT_ID
                and event.get("incident_seal_sha256") == seal_sha
                and event.get("script_sha256") == incident["script_sha256"]
                and event.get("pending_ids_sha256") == incident["pending_ids_sha256"],
                "operator receipt binding differs",
            )
            phase = event.get("phase")
            _require(
                phase in ("registered", "run_started", "diagnostic", "run_finished"), "unexpected continuation phase"
            )
            item = {
                "phase": phase,
                "sequence": event["sequence"],
                "event_sha256": event["sha256"],
                "incident_seal_sha256": seal_sha,
                "script_sha256": incident["script_sha256"],
                "pending_ids_sha256": incident["pending_ids_sha256"],
            }
            if phase == "diagnostic":
                diagnostic = event.get("diagnostic", {})
                error_type = diagnostic.get("error_type") if type(diagnostic) is dict else None
                item["diagnostic_type"] = (
                    error_type if isinstance(error_type, str) and NAME.fullmatch(error_type) else "unavailable_type"
                )
            safe_events.append(item)
        else:
            _require(
                event["event"]
                in ("trial_started", "trial_finished", "request_reserved", "request_settled", "budget_stopped")
                and event.get("trial_id") in pending,
                "journal activity outside authorized 37 slots",
            )
    _require(set(starts) <= set(old_starts) | set(pending), "unregistered/alternate trial ID")
    _require(ends.get(FAILED_SLOT) == old_ends[FAILED_SLOT], "original failed terminal rewritten")
    old_usage = _usage([r for r in requests.values() if r["trial_id"] in old_starts])
    _require(
        old_usage["usage_complete"]
        and old_usage["requests"] == incident.get("original_requests")
        and old_usage["tokens"] == incident.get("original_known_tokens"),
        "original financial prefix differs",
    )
    current_usage = _usage(list(requests.values()))
    additional_failures = [trial for trial in pending if trial in ends and ends[trial]["status"] != "completed"]
    current = [
        {
            "trial_id": trial,
            "status": ends[trial]["status"] if trial in ends else "started" if trial in starts else "planned",
        }
        for trial in pending
    ]
    states = Counter(r["status"] for r in current)
    result = {
        "schema_version": 1,
        "kind": "readonly_operational_continuation_disclosure",
        "incident_id": INCIDENT_ID,
        "hashes": {
            "incident_json": reader.hashes[sidecar / "incident.json"],
            "incident_seal": seal_sha,
            "registered_receipt": receipts[0]["sha256"],
            "original_prefix": incident["original_journal_sha256"],
            "original_journal_head": incident["original_journal_head"],
            "current_journal": reader.hashes[root / "journal.jsonl"],
            "wrapper_script": incident["script_sha256"],
            "original_pending_set": incident["pending_ids_sha256"],
            "frozen_execution_source_snapshot": incident["source_pin_sha256"],
            "pinned_original_artifacts": pins,
        },
        "denominator": {
            "planned": len(slots),
            "collection": 5,
            "test": 48,
            "original_started": len(old_starts),
            "started": len(starts),
            "terminal": len(ends),
            "original_failed": 1,
        },
        "original_failed": {
            "trial_id": FAILED_SLOT,
            "status": "failed",
            "error_type": "PermissionError",
            "root_cause": "unlocalized",
            "final_quality": None,
            "retried": False,
        },
        "continuation": {
            "original_pending_count": 37,
            "original_pending_trial_ids": pending,
            "rows": current,
            "started": sum(t in starts for t in pending),
            "completed": states["completed"],
            "failed": states["failed"],
            "cancelled": states["cancelled"],
            "unstarted": states["planned"],
            "in_progress": states["started"],
            "additional_failed_ids": additional_failures,
        },
        "financial_state": {
            k: current_usage[k]
            for k in ("requests", "settled", "pending", "unknown", "held_tokens", "known_tokens", "usage_complete")
        },
        "operator_events": safe_events,
        "complete": len(ends) == 53 and all(t in ends for t in pending),
        "authorization_continuation_clean": not additional_failures
        and current_usage["pending"] == current_usage["unknown"] == 0,
        "scope": {
            "operational_exception_after_test_exposure": True,
            "root_cause_fixed": False,
            "original_failed_in_denominator": True,
            "learning_contribution": "not_identifiable",
            "trial_quality_audit": "Use the separate frozen sealed-result analyzer; this companion validates authorization metadata and denominator only.",
            "no_raw_exception_message_filename_prompt_candidate_gold_or_final_content": True,
            "audit_provider_calls": 0,
        },
    }
    reader.verify_snapshot()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--incident", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    try:
        root, incident, output = args.root.resolve(), args.incident.resolve(), args.output.resolve()
        _require(
            not output.exists()
            and all(not output.is_relative_to(p) and not p.is_relative_to(output) for p in (root, incident)),
            "output must be new and separate from input artifacts",
        )
        value = summarize_continuation(root, incident)
        output.mkdir(parents=True, exist_ok=False)
        data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
        with (output / "continuation.json").open("xb") as stream:
            stream.write(data)
        (output / "continuation.sha256").write_text(_sha(data), encoding="ascii")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "complete": value["complete"],
                    "new_terminal": value["continuation"]["completed"]
                    + value["continuation"]["failed"]
                    + value["continuation"]["cancelled"],
                }
            )
        )
        return 0 if args.allow_incomplete or value["complete"] and value["authorization_continuation_clean"] else 2
    except (AnalysisError, OSError, KeyError, ValueError, TypeError):
        print(json.dumps({"status": "rejected", "error_type": "ContinuationEvidenceError"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
