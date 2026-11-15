"""Score all frozen LoCoMo v3 judge-length cases once with a larger cap."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from experiments.memory_validation.budget_v3 import (
    LIVE_BASE_URL,
    LIVE_MODEL,
    BudgetStop,
    create_live_llm,
)
from experiments.memory_validation.followup_common import (
    FirstTwoReadback,
    FollowupPaidStage,
    LANGFUSE_CLOUD_BASE_URL,
    file_sha,
    install_live_credential,
    mean,
    paired_comparisons,
    read_json,
    request_usage,
    require_langfuse_credentials,
    required_owner,
    seal,
    usage_sum,
    verify_seal,
    write_json,
)
from experiments.memory_validation.multisession_protocol import ARMS, sha
from experiments.memory_validation.multisession_runner_v3 import (
    JUDGE_SYSTEM,
    TokenLedgerV3,
    _judge,
    call_identity,
)
from experiments.memory_validation.runner import token_state


PROTOCOL = "locomo-judge-sensitivity-v1"
CALL_NAMESPACE = "sensitivity.v1."
JUDGE_OUTPUT_CAP = 4096
ACCOUNTING_UNITS = 250_000
MAX_REQUESTS = 12
REQUEST_TIMEOUT_SECONDS = 180
WALL_CLOCK_SECONDS = 3600
LANGFUSE_PROJECT_ID = "cmwg5sagt00ksad0j6au0463n"
COMPARISONS = (
    ("raw_lexical", "raw_mem0"),
    ("facts_lexical", "facts_mem0"),
    ("raw_lexical", "facts_lexical"),
    ("raw_mem0", "facts_mem0"),
    ("raw_lexical", "facts_mem0"),
    ("raw_mem0", "facts_lexical"),
)


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value):
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ch not in allowed for ch in value):
        raise ValueError("opaque identifier is invalid")
    return value


def _locations(plan_root, output_root, ledger):
    plan_root, output_root, ledger = (Path(path).resolve() for path in (plan_root, output_root, ledger))
    if plan_root == output_root or plan_root in output_root.parents or output_root in plan_root.parents:
        raise ValueError("immutable plan and mutable output must be disjoint")
    if any(
        ledger == root or ledger in root.parents or root in ledger.parents
        for root in (plan_root, output_root)
    ):
        raise ValueError("ledger and study roots must be disjoint")
    return plan_root, output_root, ledger


def _question_index(plan_root, plan):
    result = {}
    for entry in plan["conversations"]:
        conversation = read_json(Path(plan_root) / entry["file"])
        if conversation.get("conversation_id") != entry["conversation_id"]:
            raise ValueError("frozen conversation identity differs")
        for question in conversation["questions"]:
            qid = question["question_id"]
            if qid in result:
                raise ValueError("frozen question identity is duplicated")
            result[qid] = {**question, "conversation_id": entry["conversation_id"], "split": entry["split"]}
    return result


def _legacy_plan(plan_root):
    """Bind the preserved v3 artifacts without requiring old code hashes to still match HEAD."""
    plan_root = Path(plan_root).resolve()
    plan = verify_seal(plan_root, "plan.json")
    if (
        plan.get("schema_version") != 1
        or plan.get("protocol") != "locomo-multisession-four-arm-v3"
        or plan.get("counts", {}).get("answer_calls") != 120
        or plan.get("counts", {}).get("judge_calls") != 120
        or plan.get("model") != LIVE_MODEL
        or plan.get("provider") != "openai"
        or plan.get("base_url") != LIVE_BASE_URL
        or plan.get("temperature") != 0
        or plan.get("max_retries") != 0
        or plan.get("prompts", {}).get("judge_sha256") != sha(JUDGE_SYSTEM)
    ):
        raise ValueError("preserved v3 scoring inputs differ")
    if file_sha(plan["source"]["path"]) != plan["source"]["sha256"]:
        raise ValueError("preserved v3 source dataset changed")
    if file_sha(plan["gold"]["path"]) != plan["gold"]["sha256"]:
        raise ValueError("preserved v3 gold changed")
    for row in plan["data"]:
        if file_sha(plan_root / row["path"]) != row["sha256"]:
            raise ValueError("preserved v3 conversation data changed")
    mismatches = []
    for source in plan.get("sources", []):
        path = Path(source["path"])
        current = file_sha(path) if path.is_file() else None
        if current != source["sha256"]:
            mismatches.append({"path": str(path), "frozen_sha256": source["sha256"], "current_sha256": current})
    return plan, mismatches


def _audit(plan_root, run_root):
    plan_root, run_root = Path(plan_root).resolve(), Path(run_root).resolve()
    plan, source_mismatches = _legacy_plan(plan_root)
    if plan["prompts"]["judge_sha256"] != sha(JUDGE_SYSTEM):
        raise ValueError("frozen judge prompt changed")
    gold_path = Path(plan["gold"]["path"]).resolve()
    if file_sha(gold_path) != plan["gold"]["sha256"]:
        raise ValueError("frozen gold changed")
    gold = read_json(gold_path)
    questions = _question_index(plan_root, plan)
    rows_path = run_root / "reports" / "rows.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
    if len(rows) != 120 or Counter(row["arm"] for row in rows) != Counter({arm: 30 for arm in ARMS}):
        raise ValueError("original row denominator differs")
    by_key = {(row["question_id"], row["arm"]): row for row in rows}
    if len(by_key) != 120:
        raise ValueError("original row identities are duplicated")
    for row in rows:
        qid, arm = row["question_id"], row["arm"]
        if qid not in questions or qid not in gold or arm not in ARMS:
            raise ValueError("original row leaves the frozen question universe")
        answer_id = call_identity("answer", qid, arm)
        answer_path = run_root / "private" / "calls" / (answer_id + ".json")
        answer = read_json(answer_path)
        if (
            answer.get("finish_reason") != "stop"
            or not isinstance(answer.get("content"), str)
            or not answer["content"].strip()
            or sha(answer["content"]) != row.get("answer_sha256")
            or row.get("answer_sha256") != row.get("judged_answer_sha256")
            or answer.get("usage") != row.get("answer_usage")
            or row.get("answer_status") != "valid"
        ):
            raise ValueError("frozen answer or SHA differs from the original row")
    length_rows = [row for row in rows if row.get("judge_status") == "length"]
    if len(length_rows) != 12:
        raise ValueError("expected exactly twelve original judge-length rows")
    cases = []
    for row in length_rows:
        qid, arm = row["question_id"], row["arm"]
        question = questions[qid]
        answer_id = call_identity("answer", qid, arm)
        judge_id = call_identity("judge", qid, arm)
        answer = read_json(run_root / "private" / "calls" / (answer_id + ".json"))
        original_judge = read_json(run_root / "private" / "calls" / (judge_id + ".json"))
        if (
            original_judge.get("finish_reason") != "length"
            or original_judge.get("content") != ""
            or original_judge.get("usage") != row.get("judge_usage")
        ):
            raise ValueError("original judge-length request differs")
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question["question"],
                        "reference_answer": gold[qid]["answer"],
                        "proposed_answer": answer["content"],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        run_id = CALL_NAMESPACE + "judge." + qid + "." + arm
        _safe_id(run_id)
        cases.append(
            {
                "question_id": qid,
                "conversation_id": row["conversation_id"],
                "split": row["split"],
                "category": row["category"],
                "arm": arm,
                "run_id": run_id,
                "answer_sha256": row["answer_sha256"],
                "messages": messages,
                "messages_sha256": sha(messages),
            }
        )
    cases.sort(key=lambda item: (item["split"], item["conversation_id"], item["question_id"], item["arm"]))
    return plan, rows, cases, gold_path, run_root / "reports" / "rows.jsonl", source_mismatches


def prepare(plan_root, run_root, output, ledger, *, campaign_id):
    plan_root, run_root, output, ledger = (Path(path).resolve() for path in (plan_root, run_root, output, ledger))
    _locations(plan_root, output, ledger)
    if output.exists() or ledger.exists():
        raise ValueError("new disjoint output and ledger are required")
    plan, rows, cases, gold_path, rows_path, source_mismatches = _audit(plan_root, run_root)
    value = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "created_at": now_utc(),
        "campaign_id": _safe_id(campaign_id),
        "ledger_path": str(ledger),
        "original_plan": {
            "path": str(plan_root / "plan.json"),
            "sha256": file_sha(plan_root / "plan.json"),
            "manifest_sha256": file_sha(plan_root / "manifest.json"),
        },
        "original_rows": {"path": str(rows_path), "sha256": file_sha(rows_path)},
        "gold": {"path": str(gold_path), "sha256": file_sha(gold_path)},
        "prompts": {
            "judge_sha256": sha(JUDGE_SYSTEM),
            "original_answer_sha256": plan["prompts"]["answer_sha256"],
        },
        "legacy_source_mismatches": source_mismatches,
        "legacy_source_policy": "old plan/data/gold/answer/prompt/model bindings required; later implementation hash drift recorded",
        "model": LIVE_MODEL,
        "provider": "openai",
        "base_url": LIVE_BASE_URL,
        "temperature": 0,
        "max_retries": 0,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "output_caps": {"judge": JUDGE_OUTPUT_CAP},
        "budget": {
            "accounting_units": ACCOUNTING_UNITS,
            "max_requests": MAX_REQUESTS,
            "expected_model_calls": 12,
            "retries": 0,
            "wall_clock_seconds": WALL_CLOCK_SECONDS,
        },
        "observability": {
            "owner": "ExperimentLLMOwnerV3",
            "required": True,
            "cloud_base_url": LANGFUSE_CLOUD_BASE_URL,
            "project_id": LANGFUSE_PROJECT_ID,
            "first_two_calls": "bounded 180-second metadata/usage polling, one exact observation per trace",
        },
        "selection": "all and only original rows with judge_status=length; no outcome-selective subset",
        "rows": len(rows),
        "cases": cases,
    }
    output.mkdir(parents=True)
    seal(output, "plan.json", value)
    return {"status": "prepared", "cases": len(cases), "plan_sha256": file_sha(output / "plan.json")}


def _verify_ready(output):
    output = Path(output).resolve()
    plan = verify_seal(output, "plan.json")
    ready = read_json(output / "READY.json")
    if (
        ready.get("status") != "ready"
        or ready.get("protocol") != PROTOCOL
        or ready.get("campaign_id") != plan["campaign_id"]
        or ready.get("plan_sha256") != file_sha(output / "plan.json")
        or ready.get("plan_manifest_sha256") != file_sha(output / "manifest.json")
        or file_sha(plan["original_rows"]["path"]) != plan["original_rows"]["sha256"]
        or file_sha(plan["gold"]["path"]) != plan["gold"]["sha256"]
    ):
        raise ValueError("READY receipt does not bind the scoring plan")
    return plan, ready


def freeze(output):
    output = Path(output).resolve()
    plan = verify_seal(output, "plan.json")
    cases = plan.get("cases")
    if (
        plan.get("protocol") != PROTOCOL
        or not isinstance(cases, list)
        or len(cases) != 12
        or len({case.get("run_id") for case in cases}) != 12
        or Counter(case.get("arm") for case in cases) != Counter({"facts_lexical": 5, "facts_mem0": 5, "raw_mem0": 2})
        or any(sha(case.get("messages")) != case.get("messages_sha256") for case in cases)
    ):
        raise ValueError("sensitivity plan differs from the twelve-row selection")
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "protocol": PROTOCOL,
        "campaign_id": plan["campaign_id"],
        "frozen_at": now_utc(),
        "plan_sha256": file_sha(output / "plan.json"),
        "plan_manifest_sha256": file_sha(output / "manifest.json"),
        "case_count": 12,
        "paid_calls": 0,
        "ledger_created": Path(plan["ledger_path"]).exists(),
    }
    write_json(output / "READY.json", receipt)
    return receipt


def _completed(output):
    path = Path(output) / "status.json"
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("status") != "completed" or value.get("case_count") != 12:
        raise ValueError("existing sensitivity status differs")
    return value


def run(output, *, use_configured_credential=False):
    output = Path(output).resolve()
    existing = _completed(output)
    if existing is not None:
        return existing
    plan, _ = _verify_ready(output)
    started_states = list((output / "states").glob("*.json")) if (output / "states").exists() else []
    if (output / "STOPPED.json").exists() or started_states:
        raise BudgetStop("started sensitivity study cannot be replayed")
    require_langfuse_credentials()
    if use_configured_credential:
        credential_source = install_live_credential()
    else:
        credential_source = "live_environment"
        if not __import__("os").environ.get("LIVE_BENCH_API_KEY"):
            raise ValueError("LIVE_BENCH_API_KEY must be supplied explicitly")
    ledger = TokenLedgerV3(
        plan["ledger_path"],
        campaign_id=plan["campaign_id"],
        max_tokens=plan["budget"]["accounting_units"],
        max_requests=plan["budget"]["max_requests"],
    )
    raw = owner = owner_close = None
    try:
        raw = create_live_llm(max_output_tokens=JUDGE_OUTPUT_CAP, timeout=REQUEST_TIMEOUT_SECONDS)
        if raw.model != LIVE_MODEL or raw.config.provider != "openai" or raw.config.max_retries != 0:
            raise ValueError("live client differs from the frozen model")
        owner = required_owner(raw, plan["campaign_id"])
        stage = FollowupPaidStage(
            owner,
            ledger,
            output=output,
            verify=lambda: _verify_ready(output),
            wall_deadline=time.time() + plan["budget"]["wall_clock_seconds"],
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        gate = FirstTwoReadback(
            owner,
            campaign=plan["campaign_id"],
            project=LANGFUSE_PROJECT_ID,
            model=LIVE_MODEL,
            receipt=output / "first-two-readback.json",
            protocol=PROTOCOL,
        )
        for case in plan["cases"]:
            response = stage.call(
                case["run_id"],
                case["messages"],
                arm=case["arm"],
                stage="judge",
                task_id=case["question_id"],
                max_tokens=JUDGE_OUTPUT_CAP,
            )
            gate(
                case["run_id"],
                response,
                task_id=case["question_id"],
                arm=case["arm"],
                stage="judge",
            )
            correct, status = _judge(response.content, response.finish_reason)
            private = read_json(output / "private" / "calls" / (case["run_id"] + ".json"))
            write_json(
                output / "safe" / "cases" / (case["run_id"] + ".json"),
                {
                    "run_id": case["run_id"],
                    "question_id": case["question_id"],
                    "arm": case["arm"],
                    "answer_sha256": case["answer_sha256"],
                    "messages_sha256": case["messages_sha256"],
                    "finish_reason": response.finish_reason,
                    "status": status,
                    "correct": bool(correct and status == "valid"),
                    "usage": response.usage,
                    "latency_seconds": private["latency_seconds"],
                    "content_sha256": sha(response.content),
                },
            )
        if not gate.verified:
            raise RuntimeError("first two sensitivity traces were not remotely verified")
        owner.flush_and_check()
        owner_close = __import__("asyncio").run(owner.aclose())
        write_json(output / "owner-close.json", owner_close)
        status = {
            "schema_version": 1,
            "status": "completed",
            "protocol": PROTOCOL,
            "campaign_id": plan["campaign_id"],
            "case_count": 12,
            "first_two_cloud_verified": gate.verified,
            "credential_source": credential_source,
            "token_state": token_state(ledger.snapshot()),
            "finished_at": now_utc(),
        }
        write_json(output / "status.json", status)
        return status
    except BaseException as error:
        record = {"status": "stopped", "reason": type(error).__name__, "automatic_retry": False, "at": now_utc()}
        try:
            write_json(output / "STOPPED.json", record, replace=True)
        except BaseException:
            pass
        try:
            with ledger.transaction() as data:
                data["campaigns"][plan["campaign_id"]]["blocked"] = True
        except BaseException:
            pass
        return record
    finally:
        if owner is not None and owner_close is None:
            try:
                __import__("asyncio").run(owner.aclose())
            except BaseException:
                pass


def summarize(plan_root, run_root, output):
    plan_root, run_root, output = (Path(path).resolve() for path in (plan_root, run_root, output))
    plan, _ = _verify_ready(output)
    original_path = run_root / "reports" / "rows.jsonl"
    if file_sha(original_path) != plan["original_rows"]["sha256"]:
        raise ValueError("original rows changed")
    original = [json.loads(line) for line in original_path.read_text(encoding="utf-8").splitlines()]
    cases = {case["run_id"]: case for case in plan["cases"]}
    safe_files = sorted((output / "safe" / "cases").glob("*.json"))
    if len(safe_files) != 12:
        raise ValueError("sensitivity cases are incomplete")
    rechecks = {}
    recheck_rows = []
    for path in safe_files:
        row = read_json(path)
        case = cases[row.get("run_id")]
        if row.get("answer_sha256") != case["answer_sha256"] or row.get("messages_sha256") != case["messages_sha256"]:
            raise ValueError("sensitivity case does not bind its frozen input")
        rechecks[(row["question_id"], row["arm"])] = row
    for row in original:
        item = dict(row)
        replacement = rechecks.get((row["question_id"], row["arm"]))
        if replacement is not None:
            if row.get("judge_status") != "length" or row.get("answer_sha256") != replacement["answer_sha256"]:
                raise ValueError("sensitivity replacement targets a non-length or changed answer")
            item["correct"] = replacement["correct"]
            item["judge_status"] = replacement["status"]
            item["sensitivity_correct"] = replacement["correct"]
        else:
            item["sensitivity_correct"] = bool(row["correct"])
        recheck_rows.append(item)
    original_by_arm = {}
    sensitivity_by_arm = {}
    for arm in ARMS:
        left = [row for row in original if row["arm"] == arm]
        right = [row for row in recheck_rows if row["arm"] == arm]
        replacement_rows = [rechecks[key] for key in rechecks if key[1] == arm]
        original_by_arm[arm] = {
            "denominator": len(left),
            "correct": sum(bool(row["correct"]) for row in left),
            "accuracy": sum(bool(row["correct"]) for row in left) / len(left),
            "valid_judges": sum(row.get("judge_status") == "valid" for row in left),
            "length_judges": sum(row.get("judge_status") == "length" for row in left),
            "mean_session_recall_at_8": mean(left, "session_recall_at_8"),
            "mean_session_mrr": mean(left, "session_mrr"),
        }
        sensitivity_by_arm[arm] = {
            "denominator": len(right),
            "correct": sum(bool(row["correct"]) for row in right),
            "accuracy": sum(bool(row["correct"]) for row in right) / len(right),
            "valid_judges": sum(row.get("judge_status") == "valid" for row in right),
            "length_judges": sum(row.get("judge_status") == "length" for row in right),
            "mean_session_recall_at_8": mean(right, "session_recall_at_8"),
            "mean_session_mrr": mean(right, "session_mrr"),
            "rescored": len(replacement_rows),
            "rescored_valid": sum(row["status"] == "valid" for row in replacement_rows),
            "rescored_still_length": sum(row["status"] == "length" for row in replacement_rows),
            "rescored_incorrect_or_failed": sum(not row["correct"] for row in replacement_rows),
            "rescore_usage": usage_sum(replacement_rows, "usage"),
        }
    ledger = TokenLedgerV3(
        plan["ledger_path"],
        campaign_id=plan["campaign_id"],
        max_tokens=plan["budget"]["accounting_units"],
        max_requests=plan["budget"]["max_requests"],
    ).snapshot()
    requests = [row for row in ledger["requests"] if str(row.get("run_id", "")).startswith(CALL_NAMESPACE)]
    if len(requests) != 12 or any(row.get("status") != "ok" for row in requests):
        raise ValueError("sensitivity requests and ledger differ")
    result = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "status": "complete",
        "main_result_definition": "original 120 rows; twelve length judges remain incorrect",
        "sensitivity_result_definition": "all twelve length judges replaced once at cap 4096; failures remain incorrect",
        "main": original_by_arm,
        "sensitivity": sensitivity_by_arm,
        "main_paired": paired_comparisons(original, ARMS, COMPARISONS),
        "sensitivity_paired": paired_comparisons(recheck_rows, ARMS, COMPARISONS),
        "rechecked": {
            "count": 12,
            "by_arm": dict(Counter(key[1] for key in rechecks)),
            "valid": sum(row["status"] == "valid" for row in rechecks.values()),
            "still_length": sum(row["status"] == "length" for row in rechecks.values()),
            "invalid": sum(row["status"] not in ("valid", "length") for row in rechecks.values()),
        },
        "requests": request_usage(requests),
        "token_state": token_state(ledger),
        "first_two_readback": read_json(output / "first-two-readback.json"),
        "owner_close": read_json(output / "owner-close.json"),
        "original_report_rewritten": False,
        "main_conclusion_rewritten": False,
    }
    write_json(output / "summary.json", result, replace=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="LoCoMo judge-length sensitivity")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--plan", required=True)
    prepare_parser.add_argument("--run", required=True)
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.add_argument("--ledger", required=True)
    prepare_parser.add_argument("--campaign-id", required=True)
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--output", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--use-configured-credential", action="store_true")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--plan", required=True)
    report_parser.add_argument("--run", required=True)
    report_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.plan, args.run, args.output, args.ledger, campaign_id=args.campaign_id)
    elif args.command == "freeze":
        result = freeze(args.output)
    elif args.command == "run":
        result = run(args.output, use_configured_credential=args.use_configured_credential)
    else:
        result = summarize(args.plan, args.run, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in ("stopped", "failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
