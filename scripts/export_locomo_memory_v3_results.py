"""Validate and export safe LoCoMo v3 results without model requests."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

CALL_PREFIX = "v3r1."


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _usage(rows):
    fields = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")
    result = {name: 0 for name in fields}
    for row in rows:
        usage = row.get("usage")
        if row.get("status") != "ok" or not isinstance(usage, dict):
            raise ValueError("a v3 request is unsettled or has unknown usage")
        for name in fields:
            value = usage.get(name, 0)
            if type(value) is not int or value < 0:
                raise ValueError("v3 usage is inconsistent")
            result[name] += value
    return {"requests": len(rows), **result}


def export(plan_root, run_root, output):
    from experiments.memory_validation.multisession_runner_v3 import verify_plan, verify_ready

    plan_root, run_root, output = map(Path, (plan_root, run_root, output))
    plan = verify_plan(plan_root)
    ready = verify_ready(plan_root, run_root)
    summary_path = run_root / "reports" / "summary.json"
    rows_path = run_root / "reports" / "rows.jsonl"
    summary = read_json(summary_path)
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line]
    if summary.get("status") != "complete" or len(rows) != 120 or summary.get("missing_conversations"):
        raise ValueError("evaluation is not terminal with 120 safe rows")
    readback = read_json(run_root / "first-extract-readback.json")
    if readback.get("status") != "verified" or readback.get("sample_count") != 2:
        raise ValueError("required remote readback is not verified")
    conversations = []
    for entry in plan["conversations"]:
        state = read_json(run_root / "conversations" / (entry["conversation_id"] + ".json"))
        if state.get("status") != "completed":
            raise ValueError("a conversation is not terminally complete")
        conversations.append(state)

    ledger = read_json(plan["ledger_path"])
    current = []
    inherited = []
    for row in ledger["requests"]:
        (current if isinstance(row.get("run_id"), str) and CALL_PREFIX in row["run_id"] else inherited).append(row)
    if Counter(row["status"] for row in current) != {"ok": len(current)}:
        raise ValueError("v3 request terminal status differs")
    if any(row["status"] == "pending" or not row.get("usage") for row in current):
        raise ValueError("v3 has unknown or pending usage")

    stages = defaultdict(list)
    for row in current:
        purpose = row["purpose"].removeprefix("experiment_")
        stages[purpose].append(row)
    usage = {
        "v3": _usage(current),
        "v3_by_stage": {name: _usage(stage_rows) for name, stage_rows in sorted(stages.items())},
        "inherited": _usage(inherited),
        "all_requests": _usage(ledger["requests"]),
        "provider_billed_cost_usd": None,
        "conservative_cost_usd": {
            "v3": summary["v3_conservative_cost_usd"],
            "cumulative_ledger": summary["conservative_cost_usd"],
        },
    }

    split_arms = {}
    for split in ("dev", "test"):
        split_arms[split] = {}
        for arm in ("raw_lexical", "raw_mem0", "facts_lexical", "facts_mem0"):
            subset = [row for row in rows if row["split"] == split and row["arm"] == arm]
            valid = sum(row["judge_status"] == "valid" for row in subset)
            correct = sum(bool(row["correct"]) for row in subset)
            split_arms[split][arm] = {
                "expected": len(subset),
                "correct": correct,
                "valid_judges": valid,
                "accuracy_over_expected": correct / len(subset) if subset else None,
                "accuracy_over_valid_judges": correct / valid if valid else None,
            }

    operational = {
        "answer_status": dict(Counter(row["answer_status"] for row in rows)),
        "judge_status": dict(Counter(row["judge_status"] for row in rows)),
        "terminal_requests": dict(Counter(row["status"] for row in current)),
        "stage_requests": {name: len(stage_rows) for name, stage_rows in sorted(stages.items())},
    }

    stopped_path = run_root / "STOPPED.json"
    stopped = read_json(stopped_path) if stopped_path.exists() else None
    orchestrator_status = "completed"
    if stopped is not None:
        if stopped.get("reason") != "FileExistsError" or stopped.get("processed") != ["conv-41", "conv-42"]:
            raise ValueError("unexpected orchestrator stop")
        orchestrator_status = "stopped_after_all_terminal_calls"
    final = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "campaign_id": plan["campaign_id"],
        "evaluation_status": "complete",
        "orchestrator_status": orchestrator_status,
        "model_requests": len(current),
        "answer_plus_judge_rows": len(rows),
        "safe_conversations": len(conversations),
        "all_requests_settled": True,
        "unknown_or_inflight": False,
        "required_tracing_verified": True,
        "automatic_model_retry": False,
        "post_stop_model_requests": 0,
        "stop_reason": stopped.get("reason") if stopped else None,
        "stop_scope": "write_owner_close_after_all_test_calls_and_conversation_terminal_states"
        if stopped
        else None,
        "plan_sha256": sha256(plan_root / "plan.json"),
        "ready_plan_sha256": ready["plan_sha256"],
        "ready_receipt_sha256": sha256(run_root / "READY.json"),
        "summary_sha256": sha256(summary_path),
        "rows_sha256": sha256(rows_path),
    }
    provenance = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "plan_sha256": sha256(plan_root / "plan.json"),
        "plan_manifest_sha256": sha256(plan_root / "manifest.json"),
        "source_dataset": plan["source"],
        "gold_sha256": plan["gold"]["sha256"],
        "model": plan["model"],
        "embedding": plan["embedding"],
        "counts": plan["counts"],
        "actual_v3_counts": {"extract": len(stages["extract"]), "answer": len(stages["answer"]), "judge": len(stages["judge"])},
        "request_count_note": "The frozen planner counted 92 unique session IDs, but session_documents produced 94 extraction documents because two long session IDs were split for the 6000-character bound. The real call ledger is authoritative.",
        "prior_ledgers": plan["prior_ledgers"],
        "selected_question_count": plan["counts"]["dev_qa"] + plan["counts"]["test_qa"],
    }
    write_json(output / "usage.json", usage)
    write_json(output / "provenance.json", provenance)
    write_json(output / "finalization.json", final)
    write_json(output / "split-summary.json", split_arms)
    write_json(output / "operational-status.json", operational)

    forbidden = [value for name, value in os.environ.items() if name.endswith(("_SECRET_KEY", "_API_KEY")) and value]
    inventory = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        data = path.read_bytes()
        if any(secret.encode() in data for secret in forbidden):
            raise ValueError("credential material entered the safe export")
        inventory[path.relative_to(output).as_posix()] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    write_json(output / "manifest.json", inventory)
    return final | {"files": len(inventory), "usage": usage}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export(args.plan, args.run, args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
