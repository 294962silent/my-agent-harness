"""Export safe scoring-sensitivity and confirmation artifacts without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]

def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _assert_no_credentials(output):
    secrets = [
        value
        for name, value in os.environ.items()
        if value and name.endswith(("_SECRET_KEY", "_API_KEY"))
    ]
    for path in Path(output).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(secret.encode() in data for secret in secrets):
                raise ValueError("credential material entered the safe export")


def _ledger_requests(plan):
    from experiments.memory_validation.multisession_runner_v3 import TokenLedgerV3

    ledger = TokenLedgerV3(
        plan["ledger_path"],
        campaign_id=plan["campaign_id"],
        max_tokens=plan["budget"]["accounting_units"],
        max_requests=plan["budget"]["max_requests"],
    )
    return ledger.snapshot()


def export_sensitivity(run, output):
    from experiments.memory_validation.followup_common import read_json, verify_seal, write_json

    run, output = Path(run).resolve(), Path(output).resolve()
    plan = verify_seal(run, "plan.json")
    summary = read_json(run / "summary.json")
    status = read_json(run / "status.json")
    if (
        plan.get("protocol") != "locomo-judge-sensitivity-v1"
        or summary.get("status") != "complete"
        or status.get("status") != "completed"
        or summary.get("rechecked", {}).get("count") != 12
    ):
        raise ValueError("sensitivity study is not terminal and complete")
    cases = []
    for path in sorted((run / "safe" / "cases").glob("*.json")):
        row = read_json(path)
        cases.append(
            {
                key: row.get(key)
                for key in (
                    "run_id",
                    "question_id",
                    "arm",
                    "answer_sha256",
                    "messages_sha256",
                    "finish_reason",
                    "status",
                    "correct",
                    "usage",
                    "latency_seconds",
                    "content_sha256",
                )
            }
        )
    if len(cases) != 12:
        raise ValueError("sensitivity safe case count differs")
    ledger = _ledger_requests(plan)
    provenance = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "campaign_id": plan["campaign_id"],
        "plan_sha256": file_sha(run / "plan.json"),
        "plan_manifest_sha256": file_sha(run / "manifest.json"),
        "ready_sha256": file_sha(run / "READY.json"),
        "summary_sha256": file_sha(run / "summary.json"),
        "ledger_sha256": file_sha(plan["ledger_path"]),
        "original_plan": plan["original_plan"],
        "original_rows": plan["original_rows"],
        "gold": plan["gold"],
        "prompts": plan["prompts"],
        "model": plan["model"],
        "provider": plan["provider"],
        "base_url": plan["base_url"],
        "temperature": plan["temperature"],
        "max_retries": plan["max_retries"],
        "output_caps": plan["output_caps"],
        "budget": plan["budget"],
        "selection": plan["selection"],
        "legacy_source_policy": plan["legacy_source_policy"],
        "legacy_source_mismatches": plan["legacy_source_mismatches"],
        "requests": summary["requests"],
        "token_state": summary["token_state"],
        "ledger_request_count": len(ledger["requests"]),
        "first_two_readback": summary["first_two_readback"],
        "owner_close": summary["owner_close"],
    }
    write_json(output / "summary.json", summary)
    _write_jsonl(output / "cases.jsonl", cases)
    write_json(output / "provenance.json", provenance)
    _assert_no_credentials(output)
    return {"status": "exported", "cases": len(cases), "output": str(output)}


def export_confirmation(plan_root, run, output):
    from experiments.memory_validation.confirmation_raw_v1 import (
        _verify_ready as verify_confirmation_ready,
        verify_plan as verify_confirmation_plan,
    )
    from experiments.memory_validation.followup_common import read_json, write_json

    plan_root, run, output = Path(plan_root).resolve(), Path(run).resolve(), Path(output).resolve()
    plan, preflight_receipt, ready = verify_confirmation_ready(plan_root, run)
    verify_confirmation_plan(plan_root)
    summary = read_json(run / "summary.json")
    status = read_json(run / "status.json")
    if (
        summary.get("status") != "complete"
        or status.get("status") != "completed"
        or summary.get("rows") != 72
    ):
        raise ValueError("confirmation study is not terminal and complete")
    rows = []
    for path in sorted((run / "safe").glob("q_*.json")):
        rows.extend(read_json(path)["rows"])
    if len(rows) != 72:
        raise ValueError("confirmation safe row count differs")
    ledger = _ledger_requests(plan)
    provenance = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "campaign_id": plan["campaign_id"],
        "selection": plan["selection"],
        "source": plan["source"],
        "data": plan["data"],
        "gold": plan["gold"],
        "counts": plan["counts"],
        "arms": plan["arms"],
        "retrieval": plan["retrieval"],
        "prompts": plan["prompts"],
        "model": plan["model"],
        "provider": plan["provider"],
        "base_url": plan["base_url"],
        "temperature": plan["temperature"],
        "max_retries": plan["max_retries"],
        "output_caps": plan["output_caps"],
        "embedding": {
            key: value for key, value in plan["embedding"].items() if key != "model_dir"
        },
        "execution_hardware": preflight_receipt["embedding_status"],
        "execution_hardware_note": "Host exposed one RTX 5060 as CUDA_VISIBLE_DEVICES=0; device 2 was unavailable. Model revision did not change.",
        "preflight": {
            "sha256": file_sha(run / "PREFLIGHT.json"),
            "questions": preflight_receipt["questions"],
            "arm_questions": preflight_receipt["arm_questions"],
            "candidate_count_per_arm": preflight_receipt["candidate_count_per_arm"],
            "max_context_chars": preflight_receipt["max_context_chars"],
            "embedding_invocations": preflight_receipt["embedding_invocations"],
            "paid_model_calls": preflight_receipt["paid_model_calls"],
        },
        "anchors": {
            "plan_sha256": file_sha(plan_root / "plan.json"),
            "plan_manifest_sha256": file_sha(plan_root / "manifest.json"),
            "ready_sha256": file_sha(run / "READY.json"),
            "summary_sha256": file_sha(run / "summary.json"),
            "ledger_sha256": file_sha(plan["ledger_path"]),
            "safe_rows_sha256": summary["anchors"]["rows_sha256"],
        },
        "requests": summary["requests"],
        "token_state": summary["token_state"],
        "ledger_request_count": len(ledger["requests"]),
        "first_two_readback": summary["first_two_readback"],
        "owner_close": summary["owner_close"],
        "ready": ready,
    }
    write_json(output / "summary.json", summary)
    _write_jsonl(output / "rows.jsonl", rows)
    write_json(output / "retrieval-preflight.json", preflight_receipt)
    write_json(output / "provenance.json", provenance)
    _assert_no_credentials(output)
    return {"status": "exported", "rows": len(rows), "output": str(output)}


def export_confirmation_sensitivity(run, output):
    from experiments.memory_validation.followup_common import read_json, verify_seal, write_json

    run, output = Path(run).resolve(), Path(output).resolve()
    plan = verify_seal(run, "plan.json")
    summary = read_json(run / "summary.json")
    status = read_json(run / "status.json")
    if (
        plan.get("protocol") != "locomo-confirmation-judge-sensitivity-v1"
        or summary.get("status") != "complete"
        or status.get("status") != "completed"
        or summary.get("rechecked", {}).get("count") != 3
    ):
        raise ValueError("confirmation sensitivity study is not terminal and complete")
    cases = []
    for path in sorted((run / "safe" / "cases").glob("*.json")):
        row = read_json(path)
        cases.append(
            {
                key: row.get(key)
                for key in (
                    "run_id",
                    "question_id",
                    "arm",
                    "answer_sha256",
                    "messages_sha256",
                    "finish_reason",
                    "status",
                    "correct",
                    "usage",
                    "latency_seconds",
                    "content_sha256",
                )
            }
        )
    if len(cases) != 3:
        raise ValueError("confirmation sensitivity safe case count differs")
    ledger = _ledger_requests(plan)
    provenance = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "campaign_id": plan["campaign_id"],
        "plan_sha256": file_sha(run / "plan.json"),
        "plan_manifest_sha256": file_sha(run / "manifest.json"),
        "ready_sha256": file_sha(run / "READY.json"),
        "summary_sha256": file_sha(run / "summary.json"),
        "ledger_sha256": file_sha(plan["ledger_path"]),
        "original_plan": plan["original_plan"],
        "original_run": plan["original_run"],
        "gold": plan["gold"],
        "prompts": plan["prompts"],
        "model": plan["model"],
        "provider": plan["provider"],
        "base_url": plan["base_url"],
        "temperature": plan["temperature"],
        "max_retries": plan["max_retries"],
        "output_caps": plan["output_caps"],
        "budget": plan["budget"],
        "selection": plan["selection"],
        "legacy_source_policy": plan["legacy_source_policy"],
        "legacy_source_mismatches": plan["legacy_source_mismatches"],
        "requests": summary["requests"],
        "token_state": summary["token_state"],
        "ledger_request_count": len(ledger["requests"]),
        "first_two_readback": summary["first_two_readback"],
        "owner_close": summary["owner_close"],
    }
    write_json(output / "summary.json", summary)
    _write_jsonl(output / "cases.jsonl", cases)
    write_json(output / "provenance.json", provenance)
    _assert_no_credentials(output)
    return {"status": "exported", "cases": len(cases), "output": str(output)}


def export_context_diversity(plan_root, run, output):
    from experiments.memory_validation.context_diversity_v1 import (
        _verify_ready as verify_diversity_ready,
        verify_plan as verify_diversity_plan,
    )
    from experiments.memory_validation.followup_common import read_json, write_json

    plan_root, run, output = Path(plan_root).resolve(), Path(run).resolve(), Path(output).resolve()
    plan, preflight_receipt, ready = verify_diversity_ready(plan_root, run)
    verify_diversity_plan(plan_root)
    summary = read_json(run / "summary.json")
    status = read_json(run / "status.json")
    if (
        summary.get("status") != "complete"
        or status.get("status") != "completed"
        or summary.get("rows") != 72
    ):
        raise ValueError("context-diversity study is not terminal and complete")
    rows = []
    for path in sorted((run / "safe").glob("q_*.json")):
        rows.extend(read_json(path)["rows"])
    if len(rows) != 72:
        raise ValueError("context-diversity safe row count differs")
    ledger = _ledger_requests(plan)
    provenance = {
        "schema_version": 1,
        "protocol": plan["protocol"],
        "campaign_id": plan["campaign_id"],
        "selection": plan["selection"],
        "source": plan["source"],
        "data": plan["data"],
        "gold": plan["gold"],
        "counts": plan["counts"],
        "arms": plan["arms"],
        "retrieval": plan["retrieval"],
        "prompts": plan["prompts"],
        "model": plan["model"],
        "provider": plan["provider"],
        "base_url": plan["base_url"],
        "temperature": plan["temperature"],
        "max_retries": plan["max_retries"],
        "output_caps": plan["output_caps"],
        "embedding": {key: value for key, value in plan["embedding"].items() if key != "model_dir"},
        "execution_hardware": preflight_receipt["embedding_status"],
        "execution_hardware_note": "Host exposed one RTX 5060 as CUDA_VISIBLE_DEVICES=0; model revision unchanged.",
        "preflight": {
            "sha256": file_sha(run / "PREFLIGHT.json"),
            "questions": preflight_receipt["questions"],
            "arm_questions": preflight_receipt["arm_questions"],
            "candidate_count_per_arm": preflight_receipt["candidate_count_per_arm"],
            "max_context_chars": preflight_receipt["max_context_chars"],
            "embedding_invocations": preflight_receipt["embedding_invocations"],
            "paid_model_calls": preflight_receipt["paid_model_calls"],
            "cost_preflight": preflight_receipt["cost_preflight"],
        },
        "anchors": {
            "plan_sha256": file_sha(plan_root / "plan.json"),
            "plan_manifest_sha256": file_sha(plan_root / "manifest.json"),
            "ready_sha256": file_sha(run / "READY.json"),
            "summary_sha256": file_sha(run / "summary.json"),
            "ledger_sha256": file_sha(plan["ledger_path"]),
            "safe_rows_sha256": summary["anchors"]["rows_sha256"],
        },
        "requests": summary["requests"],
        "token_state": summary["token_state"],
        "ledger_request_count": len(ledger["requests"]),
        "first_two_readback": summary["first_two_readback"],
        "owner_close": summary["owner_close"],
        "ready": ready,
    }
    write_json(output / "summary.json", summary)
    _write_jsonl(output / "rows.jsonl", rows)
    write_json(output / "retrieval-preflight.json", preflight_receipt)
    write_json(output / "provenance.json", provenance)
    _assert_no_credentials(output)
    return {"status": "exported", "rows": len(rows), "output": str(output)}


def manifest(output):
    from experiments.memory_validation.followup_common import write_json

    output = Path(output).resolve()
    _assert_no_credentials(output)
    inventory = {
        path.relative_to(output).as_posix(): {"bytes": path.stat().st_size, "sha256": file_sha(path)}
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    write_json(output / "manifest.json", inventory, replace=True)
    return {"status": "manifested", "files": len(inventory), "output": str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sensitivity = commands.add_parser("sensitivity")
    sensitivity.add_argument("--run", required=True)
    sensitivity.add_argument("--output", required=True)
    confirmation = commands.add_parser("confirmation")
    confirmation.add_argument("--plan", required=True)
    confirmation.add_argument("--run", required=True)
    confirmation.add_argument("--output", required=True)
    confirmation_sensitivity = commands.add_parser("confirmation-sensitivity")
    confirmation_sensitivity.add_argument("--run", required=True)
    confirmation_sensitivity.add_argument("--output", required=True)
    diversity = commands.add_parser("diversity")
    diversity.add_argument("--plan", required=True)
    diversity.add_argument("--run", required=True)
    diversity.add_argument("--output", required=True)
    manifest_parser = commands.add_parser("manifest")
    manifest_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "sensitivity":
        result = export_sensitivity(args.run, args.output)
    elif args.command == "confirmation":
        result = export_confirmation(args.plan, args.run, args.output)
    elif args.command == "confirmation-sensitivity":
        result = export_confirmation_sensitivity(args.run, args.output)
    elif args.command == "diversity":
        result = export_context_diversity(args.plan, args.run, args.output)
    else:
        result = manifest(args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
