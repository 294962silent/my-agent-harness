"""Single-attempt guarded answer/judge stages; never imported by retrieval workers."""

from __future__ import annotations

from pathlib import Path
import time

from experiments.longmemeval.harness_ablation import (
    read,
    write,
    inventory,
    verify_inventory,
    verify_plan,
    phase_root,
    file_hash,
    run_id,
    tag,
)
from experiments.longmemeval.harness_corpus import ARMS, canonical, sha, answer_prompt


def judge_prompt(question, reference, hypothesis):
    from experiments.longmemeval.judge import _ABSTENTION_TEMPLATE, build_judge_prompt

    if reference["is_abstention"]:
        return _ABSTENTION_TEMPLATE.format(question["question"], reference["answer"], hypothesis)
    return build_judge_prompt(question["question_type"], question["question"], reference["answer"], hypothesis)


def _verify_phase(plan_root, root, kind):
    root = Path(root).resolve()
    verify_inventory(root)
    phase = read(root / "phase.json")
    if phase["kind"] != kind or phase["plan_sha256"] != file_hash(Path(plan_root) / "plan.json"):
        raise ValueError("phase does not belong to this plan")
    return root


def _paid_implementation():
    repo = Path(__file__).resolve().parents[2]
    return {
        name: file_hash(repo / name)
        for name in (
            "experiments/longmemeval/harness_paid.py",
            "experiments/benchmark_live/budget.py",
            "src/my_agent_llm/client.py",
        )
    }


async def paid_phase(
    stage, plan_root, retrieval, output, *, answers=None, resume=False, max_questions=None, raw_llm=None
):
    from experiments.benchmark_live.budget import BudgetLedger, BudgetStop, GuardedLLM, create_live_llm
    from my_agent_llm.models import Message
    from my_agent_llm.observation import observation_context

    if stage not in ("answer", "judge") or (
        max_questions is not None and (type(max_questions) is not int or not 1 <= max_questions <= 30)
    ):
        raise ValueError("invalid paid stage/throttle")
    plan_root = Path(plan_root).resolve()
    plan = verify_plan(plan_root)
    if not plan["ledger_path"] or not Path(plan["ledger_path"]).is_file() or plan["campaign_ceiling_usd"] is None:
        raise ValueError("paid stages require the frozen existing shared ledger and explicit campaign ceiling")
    retrieval = _verify_phase(plan_root, retrieval, "retrieval")
    if stage == "judge":
        if answers is None:
            raise ValueError("judge needs the frozen answer phase")
        answers = _verify_phase(plan_root, answers, "answer")
    descriptor = {
        "retrieval_phase_sha256": file_hash(retrieval / "phase.json"),
        "paid_implementation": _paid_implementation(),
    }
    if answers is not None:
        descriptor["answers_phase_sha256"] = file_hash(answers / "phase.json")
    root = phase_root(plan_root, output, stage, descriptor, resume=resume)
    if (root / "status.json").exists():
        previous = read(root / "status.json")
        if previous.get("status") == "stopped":
            return previous  # BudgetStop seals this phase; resume cannot bypass it with another arm ID.
    plan_sha = file_hash(plan_root / "plan.json")
    ledger = BudgetLedger(
        plan["ledger_path"], campaign_id=plan["campaign_id"], spend_ceiling_usd=plan["campaign_ceiling_usd"]
    )
    processed = 0
    try:
        for entry in plan["questions"]:
            for arm in ARMS:
                for name in ("answer", "judge"):
                    ledger.register_run(run_id(plan_sha, entry["question_id"], arm, name), max_requests=1, time_cap=180)
        campaign = ledger.campaign_status()
        if campaign.get("blocked") or campaign.get("any_campaign_blocked") or campaign.get("pending_requests"):
            result = {"status": "stopped", "reason": "shared_campaign_blocked_or_pending", "stage": stage}
            write(root / "status.json", result, replace=True)
            inventory(root)
            return result
        references = read(plan_root / "references.json") if stage == "judge" else None
        for entry in plan["questions"]:
            qid, identity = entry["question_id"], tag(entry["question_id"])
            paths = {arm: root / "trials" / (identity + "-" + arm + ".json") for arm in ARMS}
            if all(p.exists() and read(p)["status"] in ("completed", "unavailable") for p in paths.values()):
                continue
            if max_questions is not None and processed >= max_questions:
                break
            retrieval_path = retrieval / "results" / (identity + ".json")
            if not retrieval_path.exists():
                break  # unfinished retrieval is pending, never silently labelled as a negative answer
            processed += 1
            question = read(plan_root / entry["question_file"])
            retrieval_rows = read(retrieval_path)["rows"]
            for arm in ARMS:
                path = paths[arm]
                if path.exists():
                    old = read(path)
                    if old["status"] in ("completed", "unavailable"):
                        continue
                    write(path, {**old, "status": "unavailable", "reason": "interrupted_no_retry"}, replace=True)
                    inventory(root)
                    continue
                identifier = run_id(plan_sha, qid, arm, stage)
                state = {
                    "question_id": qid,
                    "question_type": entry["question_type"],
                    "arm": arm,
                    "stage": stage,
                    "run_id": identifier,
                    "parent_trial_id": f"{plan_sha[:16]}-{identity}-{arm}",
                    "model": plan["model"],
                    "max_requests": 1,
                    "time_cap": 180,
                    "max_output_tokens": 1024,
                    "status": "started",
                    "retries": 0,
                    "retrieval_sha256": file_hash(retrieval_path),
                }
                retrieved = next(r for r in retrieval_rows if r["arm"] == arm)
                if not retrieved["valid"]:
                    write(path, {**state, "status": "unavailable", "reason": "invalid_retrieval"})
                    inventory(root)
                    continue
                if stage == "answer":
                    messages = answer_prompt(question, retrieved["context"])
                else:
                    answer_path = answers / "trials" / (identity + "-" + arm + ".json")
                    if not answer_path.exists():
                        continue  # pending answer is resumable, not a failed judge
                    answer = read(answer_path)
                    if answer["status"] != "completed" or not answer.get("hypothesis"):
                        write(path, {**state, "status": "unavailable", "reason": "answer_unavailable"})
                        inventory(root)
                        continue
                    state["answer_sha256"] = file_hash(answer_path)
                    messages = [
                        {"role": "user", "content": judge_prompt(question, references[qid], answer["hypothesis"])}
                    ]
                state["input_sha256"] = sha(canonical(messages))
                write(path, state)
                inventory(root)
                if raw_llm is None:
                    raw_llm = create_live_llm(max_output_tokens=1024, timeout=120)
                if (
                    raw_llm.model != plan["model"]
                    or raw_llm.config.provider != "openai"
                    or raw_llm.config.max_retries != 0
                ):
                    raise ValueError("raw model/provider/retry settings differ from frozen protocol")
                guarded = GuardedLLM(raw_llm, ledger, identifier)
                started = time.monotonic()
                with observation_context(root_id=state["parent_trial_id"], purpose="longmem_" + stage):
                    try:
                        response = await guarded.achat([Message(**m) for m in messages], max_tokens=1024, temperature=0)
                    except BudgetStop:
                        write(path, {**state, "status": "unavailable", "reason": "BudgetStop"}, replace=True)
                        raise
                content = response.content.strip()
                state.update(
                    latency_seconds=time.monotonic() - started,
                    usage=response.usage,
                    outcome=str(response.outcome),
                    finish_reason=response.finish_reason,
                )
                if stage == "answer":
                    state.update(
                        status="completed" if content else "unavailable",
                        hypothesis=content,
                        reason=None if content else "empty_answer",
                    )
                else:
                    verdict = content.lower()
                    legal = verdict in ("yes", "no")
                    state.update(
                        status="completed" if legal else "unavailable",
                        verdict=content,
                        correct=(verdict == "yes") if legal else None,
                        reason=None if legal else "invalid_judge_format",
                        judge_level="official_prompt_nonofficial_deepseek_model",
                    )
                write(path, state, replace=True)
                inventory(root)
        states = [read(p) for p in (root / "trials").glob("*.json")] if (root / "trials").exists() else []
        terminal = sum(s["status"] in ("completed", "unavailable") for s in states)
        result = {
            "status": "completed" if terminal == 90 else "partial",
            "stage": stage,
            "terminal_slots": terminal,
            "expected_slots": 90,
            "available": sum(s["status"] == "completed" for s in states),
            "unavailable": sum(s["status"] == "unavailable" for s in states),
        }
        write(root / "status.json", result, replace=True)
        inventory(root)
        return result
    except BudgetStop:
        result = {"status": "stopped", "reason": "BudgetStop", "stage": stage}
        write(root / "status.json", result, replace=True)
        inventory(root)
        return result
    except BaseException:
        inventory(root)
        raise


def summarize(plan_root, retrieval, answers, judges, output):
    plan_root = Path(plan_root).resolve()
    plan = verify_plan(plan_root)
    retrieval = _verify_phase(plan_root, retrieval, "retrieval")
    answers = _verify_phase(plan_root, answers, "answer")
    judges = _verify_phase(plan_root, judges, "judge")
    root = phase_root(
        plan_root,
        output,
        "summary",
        {
            "retrieval_manifest_sha256": file_hash(retrieval / "manifest.json"),
            "answers_manifest_sha256": file_hash(answers / "manifest.json"),
            "judges_manifest_sha256": file_hash(judges / "manifest.json"),
        },
    )
    plan_sha = file_hash(plan_root / "plan.json")
    rows = [read(p) for p in (judges / "trials").glob("*.json")] if (judges / "trials").exists() else []
    arms = {}
    for arm in ARMS:
        values = [r for r in rows if r["arm"] == arm]
        available = [r for r in values if r["status"] == "completed"]
        arms[arm] = {
            "expected_slots": 30,
            "recorded_slots": len(values),
            "available": len(available),
            "unavailable": sum(r["status"] == "unavailable" for r in values),
            "correct": sum(r["correct"] is True for r in available),
            "accuracy_on_available": sum(r["correct"] is True for r in available) / len(available)
            if available
            else None,
        }
    identifiers = {
        run_id(plan_sha, e["question_id"], arm, stage)
        for e in plan["questions"]
        for arm in ARMS
        for stage in ("answer", "judge")
    }
    ledger = read(plan["ledger_path"])
    requests = [r for r in ledger["requests"] if r["run_id"] in identifiers]
    paid_states = [read(p) for directory in (answers, judges) for p in (directory / "trials").glob("*.json")]
    successful_ids = {r["run_id"] for r in paid_states if "usage" in r}
    request_ids = {r["run_id"] for r in requests}
    missing_cost_ids = successful_ids - request_ids
    complete_cost = not missing_cost_ids and all(
        r["status"] != "pending" and r["cost_micro_usd"] is not None for r in requests
    )
    cost = {
        "requests": len(requests),
        "settled_cost_usd": sum(r["cost_micro_usd"] for r in requests) / 1_000_000 if complete_cost else None,
        "unknown_or_pending": sum(r["status"] == "pending" or r["cost_micro_usd"] is None for r in requests),
        "successful_calls_missing_ledger": len(missing_cost_ids),
        "global_limit_micro_usd": ledger["limit_micro_usd"],
    }
    result = {
        "status": "completed" if len(rows) == 90 else "partial",
        "arms": arms,
        "cost": cost,
        "judge_level": "official_prompt_nonofficial_deepseek_model",
        "limits": plan["limits"],
    }
    write(root / "summary.json", result)
    inventory(root)
    return result
