"""Read-only analysis of sealed RSI campaign evidence; no agent/provider imports.

Only campaign/plan/journal and sealed result/final JSON are read. Candidate
trees, transcripts, prompts, gold solutions and official tests are never opened.
Outputs are new-only and cannot be placed inside the input campaign.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics


ARMS = ("A", "B", "C", "random")
TEST_TASKS = (1, 6, 9, 20, 33, 40)
SEEDS = (23, 47)
COLLECTION = {7: "train", 11: "train", 27: "train", 32: "val", 45: "val"}
POLICY_SOURCE = "8ec35bd2538220517ce1bfca4282db21b8c518db"
HASH = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
TRIAL = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class AnalysisError(ValueError):
    """Invalid or changing evidence. Messages never include raw evidence text."""


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _require(condition, message):
    if not condition:
        raise AnalysisError(message)


def _integer(value):
    return type(value) is int and value >= 0


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(AnalysisError("nonfinite JSON value")),
        )
    except (UnicodeError, json.JSONDecodeError):
        raise AnalysisError("invalid JSON evidence") from None


class _Reader:
    def __init__(self):
        self.hashes = {}

    def raw(self, path):
        path = Path(path)
        try:
            _require(path.stat().st_size <= 64_000_000, "evidence exceeds size bound")
            value = path.read_bytes()
        except OSError:
            raise AnalysisError("required sealed artifact unavailable") from None
        digest = _sha(value)
        _require(path not in self.hashes or self.hashes[path] == digest, "evidence changed during snapshot")
        self.hashes[path] = digest
        return value

    def json(self, path):
        value = _decode(self.raw(path))
        _require(type(value) is dict, "expected JSON object")
        return value

    def verify_snapshot(self):
        for path, digest in self.hashes.items():
            try:
                _require(
                    _sha(path.read_bytes()) == digest, "evidence changed during snapshot; retry read-only analysis"
                )
            except OSError:
                raise AnalysisError("evidence disappeared during snapshot") from None


def _journal(reader, root):
    campaign = reader.json(root / "campaign.json")
    _require(campaign.get("schema_version") == 1, "unsupported campaign schema")
    raw = reader.raw(root / "journal.jsonl")
    _require(raw.endswith(b"\n"), "incomplete journal tail")
    rows = [_decode(line) for line in raw.splitlines()]
    _require(bool(rows), "empty journal")
    previous = None
    manifest_sha = _sha(_bytes(campaign))
    starts, ends, requests = {}, {}, {}
    for index, row in enumerate(rows):
        _require(
            type(row) is dict
            and row.get("sequence") == index
            and row.get("previous") == previous
            and row.get("manifest_sha256") == manifest_sha,
            "journal chain/schema mismatch",
        )
        unsigned = {k: v for k, v in row.items() if k != "sha256"}
        _require(row.get("sha256") == _sha(_bytes(unsigned)), "journal hash mismatch")
        previous = row["sha256"]
        event = row.get("event")
        if event == "trial_started":
            trial = row.get("trial_id")
            _require(
                isinstance(trial, str) and TRIAL.fullmatch(trial) and trial not in starts,
                "duplicate or invalid started trial",
            )
            starts[trial] = row
        elif event in ("request_reserved", "request_settled"):
            trial, request_id, record = row.get("trial_id"), row.get("request_id"), row.get("record")
            _require(
                trial in starts
                and trial not in ends
                and type(record) is dict
                and _integer(record.get("request"))
                and record["request"] > 0
                and request_id == f"{trial}:{record['request']}",
                "invalid request binding",
            )
            if event == "request_reserved":
                _require(
                    request_id not in requests
                    and record.get("status") == "pending"
                    and _integer(record.get("reserved_tokens")),
                    "duplicate or invalid reservation",
                )
                requests[request_id] = {"trial_id": trial, "record": record, "raw_usage": None}
            else:
                _require(
                    request_id in requests
                    and requests[request_id]["record"]["status"] == "pending"
                    and record.get("status") in ("settled", "unknown"),
                    "unreserved or duplicate settlement",
                )
                _require(
                    record.get("reserved_tokens") == requests[request_id]["record"]["reserved_tokens"],
                    "reservation changed at settlement",
                )
                requests[request_id] = {"trial_id": trial, "record": record, "raw_usage": row.get("raw_usage")}
        elif event == "trial_finished":
            trial = row.get("trial_id")
            _require(
                trial in starts and trial not in ends and row.get("status") in ("completed", "failed", "cancelled"),
                "duplicate or invalid terminal trial",
            )
            ends[trial] = row
    _require(rows[0].get("event") == "campaign_frozen", "missing campaign genesis")
    return campaign, rows, starts, ends, requests


def _inventory(plan):
    _require(plan.get("schema_version") == 1 and type(plan.get("slots")) is list, "unsupported plan schema")
    slots, ids, keys = plan["slots"], set(), set()
    _require(len(slots) == 53, "frozen plan must contain all 53 slots")
    expected = {("collection", f"HumanEval/{n}", split, 23, "B") for n, split in COLLECTION.items()}
    expected |= {("test", f"HumanEval/{n}", "test", seed, arm) for n in TEST_TASKS for seed in SEEDS for arm in ARMS}
    for slot in slots:
        _require(
            type(slot) is dict and set(slot) == {"trial_id", "phase", "task_id", "split", "seed", "arm"},
            "invalid slot schema",
        )
        trial = slot["trial_id"]
        key = tuple(slot[k] for k in ("phase", "task_id", "split", "seed", "arm"))
        _require(
            isinstance(trial, str) and TRIAL.fullmatch(trial) and trial not in ids and key not in keys,
            "duplicate slot key or trial identity",
        )
        ids.add(trial)
        keys.add(key)
    _require(keys == expected, "slot inventory differs from frozen experiment")
    return slots


def _accounted_usage(raw):
    """Match ParentBudget: reported total wins over ambiguous cache components."""
    fields = {"prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens"}
    _require(
        type(raw) is dict and all(_integer(raw[k]) for k in fields if k in raw), "unsupported normalized usage schema"
    )
    _require(_integer(raw.get("completion_tokens")), "settled completion usage unavailable")
    if "total_tokens" in raw:
        total = raw["total_tokens"]
        basis = "reported_total_minus_completion"
    else:
        components = {"prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens"}
        _require(components <= raw.keys(), "missing total requires all four explicit components")
        total = sum(raw[k] for k in components)
        basis = "explicit_all_input_components"
    output = raw["completion_tokens"]
    _require(total >= output, "reported total is smaller than completion")
    return total - output, output, total, basis


def _usage(requests):
    known_input = known_output = known_tokens = cache_sum = coverage = 0
    statuses = Counter()
    per_request = []
    for item in requests:
        record, raw = item["record"], item["raw_usage"]
        status = record["status"]
        statuses[status] += 1
        if status != "settled":
            error_type = record.get("error_type")
            per_request.append(
                {
                    "request": record["request"],
                    "status": status,
                    "usage": None,
                    "error_type": error_type if isinstance(error_type, str) and NAME.fullmatch(error_type) else None,
                }
            )
            continue
        input_tokens, output_tokens, total_tokens, basis = _accounted_usage(raw)
        _require(
            all(_integer(record.get(k)) for k in ("input_tokens", "output_tokens", "used_tokens")),
            "settled usage is incomplete",
        )
        _require(
            total_tokens == record["used_tokens"] and output_tokens == record["output_tokens"],
            "normalized usage/accounting mismatch",
        )
        _require(
            input_tokens == record["input_tokens"]
            and record["input_tokens"] + record["output_tokens"] == record["used_tokens"],
            "token arithmetic mismatch",
        )
        known_input += record["input_tokens"]
        known_output += record["output_tokens"]
        known_tokens += record["used_tokens"]
        present = "cache_read_tokens" in raw
        coverage += int(present)
        if present:
            cache_sum += raw["cache_read_tokens"]
        safe_usage = {
            k: raw[k]
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            )
            if k in raw
        }
        per_request.append(
            {
                "request": record["request"],
                "status": status,
                "usage": safe_usage,
                "input_tokens": record["input_tokens"],
                "output_tokens": record["output_tokens"],
                "tokens": record["used_tokens"],
                "cache_read_field_present": present,
                "usage_basis": basis,
            }
        )
    complete = statuses["pending"] == statuses["unknown"] == 0
    cache_complete = complete and len(requests) > 0 and coverage == len(requests)
    return {
        "requests": len(requests),
        "settled": statuses["settled"],
        "pending": statuses["pending"],
        "unknown": statuses["unknown"],
        "usage_complete": complete,
        "input_tokens": known_input if complete else None,
        "output_tokens": known_output if complete else None,
        "tokens": known_tokens if complete else None,
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_tokens": known_tokens,
        "held_tokens": sum(x["record"]["reserved_tokens"] for x in requests if x["record"]["status"] != "settled"),
        "cache_read_field_coverage": coverage,
        "cache_read_tokens": cache_sum if coverage else None,
        "cache_read_tokens_complete": cache_complete,
        "complete_cache_hit_rate": cache_sum / known_input if cache_complete and known_input else None,
        "currency": None,
        "money": None,
        "per_request": per_request,
    }


def _policy_hash(parameters):
    _require(
        type(parameters) is dict
        and set(parameters) == {"max_width", "max_refine"}
        and type(parameters["max_width"]) is int
        and 1 <= parameters["max_width"] <= 64
        and type(parameters["max_refine"]) is int
        and 0 <= parameters["max_refine"] <= 64,
        "invalid frozen policy parameters",
    )
    return _sha(_bytes({"schema_version": 1, "source_commit": POLICY_SOURCE, "parameters": parameters}))


def _research_policy(reader, root, events):
    markers = [r for r in events if r["event"] == "optimizer_finished"]
    if not markers:
        _require(not (root / "research-policy.json").exists(), "uncommitted policy artifact")
        return None
    _require(len(markers) == 1, "duplicate optimizer terminal")
    policy = reader.json(root / "research-policy.json")
    _require(
        markers[0].get("policy") == policy and markers[0].get("policy_sha256") == _sha(_bytes(policy)),
        "research policy seal mismatch",
    )
    _require(
        policy.get("policy_hash") == _policy_hash(policy.get("parameters")) and policy.get("promoted") is False,
        "invalid or published research policy",
    )
    if policy.get("C_condition") == "reference_fallback_not_trained_intervention":
        _require(
            policy.get("publication_status") == "validation_incomplete_rejected_for_publication"
            and policy.get("validation_gain") is None
            and policy.get("incumbent_gain") is None
            and policy.get("validation_coverage") == {"replayable": 1, "total": 2, "unavailable": ["HumanEval/32"]},
            "fallback cannot claim full validation or publication",
        )
    return policy


def _check_budget(budget, campaign, usage):
    _require(type(budget) is dict and budget.get("limits") == campaign["limits"], "budget schema mismatch")
    _require(
        budget.get("requests") == usage["requests"]
        and budget.get("used_tokens") == usage["known_tokens"]
        and budget.get("held_tokens") == usage["held_tokens"]
        and budget.get("usage_complete") == usage["usage_complete"],
        "sealed budget differs from journal",
    )


def _trial_row(reader, root, campaign, plan, slot, start, end, requests, policy):
    usage = _usage(requests)
    row = {
        **slot,
        "planned": True,
        "started": start is not None,
        "terminal_present": end is not None,
        "completed": end is not None and end["status"] == "completed",
        "host_status": end["status"] if end else "started" if start else "planned",
        "artifact_status": "not_sealed",
        "agent_status": None,
        "agent_terminals": None,
        "agent_success": None,
        "final_valid": None,
        "final_pass": None,
        "final_unavailable": True,
        "final_unavailable_reason": "not_sealed",
        "usage": usage,
        "attempt_valid": None,
        "attempt_invalid": None,
        "attempt_undispatched": None,
        "attempt_planned": None,
        "attempt_count": None,
        "tool_events": None,
        "tool_calls": None,
        "context_activity": None,
        "context_activity_by_attempt": None,
        "tool_schema_hashes": None,
        "policy_hash": None,
        "policy_parameters": None,
        "provenance": None,
        "artifact_hashes": None,
    }
    if start is None:
        _require(end is None and not requests, "unstarted slot has evidence")
        return row
    metadata = start.get("metadata")
    _require(
        type(metadata) is dict and all(metadata.get(k) == slot[k] for k in ("task_id", "split", "seed", "arm")),
        "started slot tuple mismatch",
    )
    task = plan["tasks"].get(slot["task_id"])
    _require(
        type(task) is dict
        and task.get("split") == slot["split"]
        and _hash(task.get("source_sha"))
        and _hash(task.get("host_evaluator_sha"))
        and metadata.get("source_sha") == task.get("source_sha")
        and metadata.get("host_evaluator_sha") == task.get("host_evaluator_sha"),
        "task source pin mismatch",
    )
    mode = "baseline" if slot["arm"] == "A" else "layered"
    schedule = "random" if slot["arm"] == "random" else "rsi" if slot["arm"] == "C" else "fixed"
    _require(metadata.get("mode") == mode and metadata.get("schedule") == schedule, "controlled mode/schedule mismatch")
    frozen = metadata.get("policy")
    _require(
        type(frozen) is dict and frozen.get("policy_hash") == _policy_hash(frozen.get("parameters")),
        "frozen policy hash mismatch",
    )
    expected = policy["parameters"] if slot["arm"] == "C" and policy is not None else {"max_width": 2, "max_refine": 1}
    _require(slot["arm"] != "C" or policy is not None, "C started without committed policy")
    _require(frozen["parameters"] == expected, "policy parameters differ from frozen arm")
    row.update(policy_hash=frozen["policy_hash"], policy_parameters=frozen["parameters"])
    if end is None:
        return row
    evidence = end.get("evidence", {})
    seal = evidence.get("final_evaluation_sha256")
    if end["status"] != "completed" and seal is None:
        row.update(artifact_status="host_failure_without_sealed_result", final_unavailable_reason="host_failure")
        return row
    _require(_hash(seal), "completed trial lacks final seal")
    directory = root / "trials" / slot["trial_id"]
    final_path, result_path = directory / "final-evaluation.json", directory / "result.json"
    final_bytes = reader.raw(final_path)
    _require(_sha(final_bytes) == seal, "final artifact hash mismatch")
    final = reader.json(final_path)
    result_bytes = reader.raw(result_path)
    _require(final.get("runtime_result_sha256") == _sha(result_bytes), "runtime artifact hash mismatch")
    result = reader.json(result_path)
    _require(
        result.get("status")
        in ("completed", "budget_exhausted", "invalid_attempt", "invalid", "cancelled", "cleanup_failed"),
        "unsupported runtime status",
    )
    _require(
        all(final.get(k) == slot[k] and result.get(k) == slot[k] for k in ("task_id", "split"))
        and final.get("trial_id") == slot["trial_id"]
        and final.get("final_evaluator_id") == task["host_evaluator_sha"],
        "sealed task identity mismatch",
    )
    _require(
        final.get("protocol_id") == campaign["protocol_id"]
        and final.get("attempt_max_turns") == campaign["attempt_max_turns"],
        "cross-protocol final schema mismatch",
    )
    _require(
        final.get("agent_task_status") == result.get("status")
        and type(final.get("final_success")) is bool
        and final.get("selection_frozen") is True,
        "terminal schema mismatch",
    )
    _require(result.get("policy") == frozen, "runtime policy differs from frozen start")
    _check_budget(final.get("budget"), campaign, usage)
    _check_budget(result.get("budget"), campaign, usage)
    if "budget" in evidence:
        _check_budget(evidence["budget"], campaign, usage)
    ordered = sorted(requests, key=lambda x: x["record"]["request"])
    _require(result.get("provider_requests") == [x["record"] for x in ordered], "runtime requests differ from journal")
    provenance = result.get("provenance")
    _require(
        type(provenance) is dict
        and final.get("provenance") == provenance
        and provenance.get("model") == campaign["model"].get("model"),
        "provider schema mismatch",
    )
    safe_provenance = {
        k: provenance[k]
        for k in ("kind", "model", "client_class", "provider_class", "client_source_sha256", "provider_source_sha256")
        if k in provenance
    }
    for key, value in safe_provenance.items():
        _require(isinstance(value, str) and NAME.fullmatch(value), "unsafe provenance field")
        if key.endswith("sha256"):
            _require(_hash(value), "invalid provenance source hash")
    if provenance.get("kind") == "observed":
        for prefix in ("client", "provider"):
            name = provenance.get(prefix + "_class", "").rsplit(".", 1)[0]
            source = "src/" + name.replace(".", "/") + ".py"
            _require(
                campaign["execution_source"].get(source) == provenance.get(prefix + "_source_sha256"),
                "provenance differs from frozen source",
            )
    attempts = result.get("attempts")
    _require(type(attempts) is list, "invalid attempt inventory")
    tool_events, tool_ids, terminals, activities, schemas = Counter(), defaultdict(set), [], [], []
    observed_tools = 0
    for attempt in attempts:
        _require(type(attempt) is dict and type(attempt.get("valid")) is bool, "invalid attempt schema")
        terminal = attempt.get("agent_terminal")
        if terminal is None:
            terminals.append(None)
        else:
            _require(
                type(terminal) is dict
                and isinstance(terminal.get("stop_reason"), str)
                and NAME.fullmatch(terminal["stop_reason"])
                and _integer(terminal.get("iterations")),
                "invalid agent terminal",
            )
            terminals.append({"stop_reason": terminal["stop_reason"], "iterations": terminal["iterations"]})
        schema = attempt.get("tool_schema_sha256")
        _require(schema is None or _hash(schema), "invalid tool schema hash")
        schemas.append(schema)
        activity = attempt.get("context_activity")
        if activity is None:
            _require(not attempt["valid"], "valid attempt lacks context observations")
            activities.append(None)
        else:
            _require(
                type(activity) is dict and all(_integer(activity.get(k)) for k in ("phase_fold_views", "micro_groups")),
                "context observation schema mismatch",
            )
            activities.append({k: activity[k] for k in ("phase_fold_views", "micro_groups")})
        context = attempt.get("context")
        _require(
            (context is None and not attempt["valid"]) or type(context) is dict and context.get("mode") == mode,
            "attempt context mode mismatch",
        )
        actions = attempt.get("actions")
        _require((actions is None and not attempt["valid"]) or type(actions) is list, "invalid tool observations")
        observed_tools += int(actions is not None)
        for action in actions or []:
            _require(
                type(action) is dict and isinstance(action.get("tool_call_id"), str), "invalid tool event identity"
            )
            name, event = action.get("tool_name"), action.get("event")
            _require(
                isinstance(name, str)
                and NAME.fullmatch(name)
                and event in ("ToolExecutionStart", "ToolExecutionUpdate", "ToolExecutionEnd"),
                "unsafe tool event",
            )
            tool_events[f"{name}:{event}"] += 1
            tool_ids[name].add((len(activities), action.get("tool_call_id")))
    valid = sum(a["valid"] for a in attempts)
    _require(
        result.get("valid_attempts") == valid
        and result.get("invalid_attempts") == len(attempts) - valid
        and _integer(result.get("planned_attempts"))
        and result["planned_attempts"] >= len(attempts)
        and result.get("not_dispatched_attempts") == result["planned_attempts"] - len(attempts),
        "attempt counts mismatch",
    )
    measured = final.get("final")
    final_valid = final_pass = None
    reason = "not_run"
    if measured is not None:
        _require(type(measured) is dict and type(measured.get("valid")) is bool, "invalid final measurement schema")
        final_valid = measured["valid"]
        if final_valid:
            score = measured.get("score")
            _require(type(score) in (int, float) and math.isfinite(score) and score in (0, 1), "invalid final score")
            final_pass, reason = score == 1, None
        else:
            reason = "invalid_or_not_run"
    _require(
        final["final_success"] == (final_pass is True and final.get("execution_integrity") is True),
        "agent success contradicts final proof",
    )
    row.update(
        artifact_status="sealed",
        agent_status=result["status"],
        agent_terminals=terminals,
        agent_success=final["final_success"],
        final_valid=final_valid,
        final_pass=final_pass,
        final_unavailable=final_pass is None,
        final_unavailable_reason=reason,
        attempt_valid=valid,
        attempt_invalid=len(attempts) - valid,
        attempt_undispatched=result["not_dispatched_attempts"],
        attempt_planned=result["planned_attempts"],
        attempt_count=len(attempts),
        tool_events=dict(tool_events) if observed_tools else None,
        tool_calls={k: len(v) for k, v in tool_ids.items()} if observed_tools else None,
        tool_observation_coverage={"attempts_observed": observed_tools, "attempts": len(attempts)},
        context_activity_by_attempt=activities,
        context_activity={
            k: sum(a[k] for a in activities) if all(a is not None for a in activities) else None
            for k in ("phase_fold_views", "micro_groups")
        },
        tool_schema_hashes=schemas,
        provenance=safe_provenance,
        artifact_hashes={"result": _sha(result_bytes), "final_evaluation": seal},
    )
    return row


def _coverage(rows):
    return {
        "planned": len(rows),
        "started": sum(r["started"] for r in rows),
        "terminal": sum(r["terminal_present"] for r in rows),
        "host_completed": sum(r["completed"] for r in rows),
        "sealed": sum(r["artifact_status"] == "sealed" for r in rows),
        "agent_success": sum(r["agent_success"] is True for r in rows),
        "final_available": sum(r["final_pass"] is not None for r in rows),
        "final_unavailable": sum(r["final_pass"] is None for r in rows),
        "host_status_counts": dict(Counter(r["host_status"] for r in rows)),
        "agent_status_counts": dict(Counter(r["agent_status"] or "unavailable" for r in rows)),
    }


def _comparisons(rows, complete, fallback):
    out = {
        "status": "incomplete",
        "paired_rows": None,
        "task_aggregates": None,
        "final_grade_pairs": None,
        "descriptive_summary": None,
        "schema_mismatch_groups": [],
    }
    if not complete:
        return out
    groups = defaultdict(dict)
    for row in rows:
        groups[(row["task_id"], row["seed"])][row["arm"]] = row
    for (task, seed), arms in groups.items():
        hashes = [r["tool_schema_hashes"] for r in arms.values()]
        sets = [set(h) if h is not None else set() for h in hashes]
        if (
            any(not h or None in h for h in sets)
            or any(h != sets[0] for h in sets)
            or any(r["provenance"] != next(iter(arms.values()))["provenance"] for r in arms.values())
        ):
            out["schema_mismatch_groups"].append({"task_id": task, "seed": seed})
    if out["schema_mismatch_groups"]:
        out["status"] = "schema_mismatch"
        return out
    if any(type(r["agent_success"]) is not bool or not r["usage"]["usage_complete"] for r in rows):
        out["status"] = "outcome_or_usage_unavailable"
        return out
    pairs = []
    for (task, seed), arms in sorted(groups.items()):
        item = {
            "task_id": task,
            "seed": seed,
            "arms": {
                arm: {
                    "agent_success": arms[arm]["agent_success"],
                    "final_pass": arms[arm]["final_pass"],
                    "requests": arms[arm]["usage"]["requests"],
                    "tokens": arms[arm]["usage"]["tokens"],
                }
                for arm in ARMS
            },
            "differences": {},
        }
        for left, right in (("B", "A"), ("C", "B"), ("random", "B")):
            x, y = arms[left], arms[right]
            item["differences"][f"{left}_minus_{right}"] = {
                "agent_success": int(x["agent_success"]) - int(y["agent_success"]),
                "requests": x["usage"]["requests"] - y["usage"]["requests"],
                "tokens": x["usage"]["tokens"] - y["usage"]["tokens"],
                "interpretation": "descriptive_repetition_difference_learning_not_identifiable"
                if fallback and left == "C"
                else "descriptive_only",
            }
        pairs.append(item)
    summaries = {
        comparison: {
            metric: statistics.mean(p["differences"][comparison][metric] for p in pairs)
            for metric in ("agent_success", "requests", "tokens")
        }
        for comparison in pairs[0]["differences"]
    }
    aggregated = [
        {
            "task_id": task,
            "scheduling_repeats": 2,
            "mean_differences": {
                c: {
                    m: statistics.mean(p["differences"][c][m] for p in pairs if p["task_id"] == task)
                    for m in ("agent_success", "requests", "tokens")
                }
                for c in summaries
            },
        }
        for task in sorted({p["task_id"] for p in pairs})
    ]
    final_pairs = None
    if all(r["final_pass"] is not None for r in rows):
        final_pairs = [
            {"task_id": p["task_id"], "seed": p["seed"], "final_pass": {a: p["arms"][a]["final_pass"] for a in ARMS}}
            for p in pairs
        ]
    out.update(
        status="complete_descriptive_only",
        paired_rows=pairs,
        task_aggregates=aggregated,
        final_grade_pairs=final_pairs,
        descriptive_summary=summaries,
    )
    return out


def _auxiliary(reader, calibration_root, connectivity_path):
    calibration = {"status": "unavailable", "usage": None}
    if calibration_root is not None and (calibration_root / "campaign.json").is_file():
        campaign, _, starts, ends, requests = _journal(reader, calibration_root)
        calibration = {
            "status": "verified_journal",
            "campaign_id": campaign["campaign_id"],
            "terminal_complete": bool(starts) and set(starts) == set(ends),
            "usage": _usage(list(requests.values())),
            "artifact_sha256": {
                "campaign": reader.hashes[calibration_root / "campaign.json"],
                "journal": reader.hashes[calibration_root / "journal.jsonl"],
            },
        }
    connectivity = {"status": "unavailable", "usage": None}
    if connectivity_path is not None and connectivity_path.is_file():
        value = reader.json(connectivity_path)
        raw = value.get("usage")
        _require(value.get("status") == "settled" and type(raw) is dict, "connectivity usage unavailable")
        record = {
            "request": 1,
            "status": "settled",
            "reserved_tokens": value.get("reserved_tokens", 0),
            "input_tokens": value.get("input_tokens"),
            "output_tokens": value.get("output_tokens"),
            "used_tokens": value.get("total_tokens"),
        }
        connectivity = {
            "status": "verified_settled_record",
            "usage": _usage([{"record": record, "raw_usage": raw}]),
            "artifact_sha256": reader.hashes[connectivity_path],
        }
    return calibration, connectivity


def analyze_campaign(root, *, calibration_root=None, connectivity_path=None):
    """Return safe JSON data. Missing/unrun grades remain None, never zero."""
    root = Path(root).resolve()
    reader = _Reader()
    campaign, events, starts, ends, requests = _journal(reader, root)
    _require(
        isinstance(campaign.get("protocol_id"), str)
        and NAME.fullmatch(campaign["protocol_id"])
        and campaign.get("attempt_max_turns") == 12
        and type(campaign.get("limits")) is dict
        and type(campaign.get("model")) is dict
        and type(campaign.get("execution_source")) is dict,
        "unsupported paid execution schema",
    )
    plan = reader.json(root / "plan.json")
    slots = _inventory(plan)
    markers = [r for r in events if r["event"] == "plan_frozen"]
    _require(len(markers) == 1 and markers[0].get("plan_sha256") == _sha(_bytes(plan)), "frozen plan hash mismatch")
    _require(all(s["sequence"] > markers[0]["sequence"] for s in starts.values()), "trial preceded frozen plan")
    _require(set(starts).issubset({s["trial_id"] for s in slots}), "unplanned started trial")
    policy = _research_policy(reader, root, events)
    test_ids = {s["trial_id"] for s in slots if s["phase"] == "test"}
    test_starts = [s for trial, s in starts.items() if trial in test_ids]
    if test_starts:
        optimizer_terminal = next((e for e in events if e["event"] == "optimizer_finished"), None)
        _require(
            optimizer_terminal is not None and all(s["sequence"] > optimizer_terminal["sequence"] for s in test_starts),
            "test started before the research policy was frozen",
        )
    rows = [
        _trial_row(
            reader,
            root,
            campaign,
            plan,
            slot,
            starts.get(slot["trial_id"]),
            ends.get(slot["trial_id"]),
            [r for r in requests.values() if r["trial_id"] == slot["trial_id"]],
            policy,
        )
        for slot in slots
    ]
    test = [r for r in rows if r["phase"] == "test"]
    collection = [r for r in rows if r["phase"] == "collection"]
    complete = all(r["terminal_present"] for r in test)
    fallback = policy is not None and policy.get("C_condition") == "reference_fallback_not_trained_intervention"
    if fallback:
        raw = policy.get("raw_sdk_selected")
        _require(
            type(raw) is dict
            and raw.get("role") == "baseline"
            and raw.get("parameters") is None
            and policy.get("parameters") == {"max_width": 2, "max_refine": 1}
            and policy.get("selected_parameters") is None
            and raw.get("policy_hash") == policy.get("selected_sdk_policy_hash"),
            "fallback policy identity mismatch",
        )
        _require(
            raw["policy_hash"]
            == _sha(_bytes({"schema_version": 1, "source_commit": POLICY_SOURCE, "parameters": None})),
            "baseline SDK policy identity mismatch",
        )
    comparisons = _comparisons(test, complete, fallback)
    parent = root.parent
    calibration_root = (
        Path(calibration_root).resolve() if calibration_root is not None else parent / "live-rsi-20261114-campaign-v1"
    )
    connectivity_path = (
        Path(connectivity_path).resolve()
        if connectivity_path is not None
        else parent / "live-rsi-20261114-connectivity" / "request.json"
    )
    _require(calibration_root != root, "calibration cannot duplicate the main campaign")
    calibration, connectivity = _auxiliary(reader, calibration_root, connectivity_path)
    _require(calibration.get("campaign_id") != campaign["campaign_id"], "calibration identity duplicates main campaign")
    collection_usage = _usage([r for r in requests.values() if any(s["trial_id"] == r["trial_id"] for s in collection)])
    test_usage = _usage([r for r in requests.values() if any(s["trial_id"] == r["trial_id"] for s in test)])
    components = [collection_usage, test_usage, calibration["usage"], connectivity["usage"]]
    observed_total = None
    if all(c is not None and c["usage_complete"] for c in components):
        observed_total = {
            k: sum(c[k] for c in components) for k in ("requests", "input_tokens", "output_tokens", "tokens")
        }
        observed_total.update(currency=None, money=None)
    full_total = (
        observed_total
        if complete and all(r["terminal_present"] for r in collection) and calibration.get("terminal_complete")
        else None
    )
    known_total = None
    if all(c is not None for c in components):
        known_total = {
            k: sum(c[k] for c in components)
            for k in (
                "requests",
                "settled",
                "pending",
                "unknown",
                "known_input_tokens",
                "known_output_tokens",
                "known_tokens",
                "held_tokens",
            )
        }
        known_total.update(currency=None, money=None, complete=all(c["usage_complete"] for c in components))
    all_usage = _usage(list(requests.values()))
    unknown_types = dict(
        Counter(r.get("error_type") or "unavailable_type" for r in all_usage["per_request"] if r["status"] == "unknown")
    )
    result = {
        "schema_version": 1,
        "kind": "sealed_offline_descriptive_analysis",
        "campaign_id": campaign["campaign_id"],
        "protocol_id": campaign["protocol_id"],
        "execution_schema": {
            "attempt_max_turns": campaign["attempt_max_turns"],
            "limits": campaign["limits"],
            "source_snapshot_sha256": _sha(_bytes(campaign["execution_source"])),
        },
        "rows": rows,
        "financial_state": {
            "pending_requests": all_usage["pending"],
            "unknown_requests": all_usage["unknown"],
            "held_tokens": all_usage["held_tokens"],
            "usage_complete": all_usage["usage_complete"],
            "unknown_error_types": unknown_types,
        },
        "test": {
            "complete": complete,
            "coverage": _coverage(test),
            "by_arm": {
                arm: {
                    **_coverage([r for r in test if r["arm"] == arm]),
                    "usage": _usage(
                        [
                            r
                            for r in requests.values()
                            if any(s["trial_id"] == r["trial_id"] and s["arm"] == arm for s in test)
                        ]
                    ),
                }
                for arm in ARMS
            },
        },
        "collection": {"coverage": _coverage(collection), "rows": [r["trial_id"] for r in collection]},
        "comparisons": comparisons,
        "C_interpretation": {
            "condition": policy.get("C_condition") if policy else "not_frozen",
            "raw_sdk_selected": {k: policy["raw_sdk_selected"].get(k) for k in ("role", "parameters", "policy_hash")}
            if policy and "raw_sdk_selected" in policy
            else None,
            "effective_parameters": policy["parameters"] if policy else None,
            "effective_policy_hash": policy["policy_hash"] if policy else None,
            "same_parameters_as_B": policy is not None and policy["parameters"] == {"max_width": 2, "max_refine": 1},
            "learning_contribution": "not_identifiable" if fallback else "not_established",
            "publication_status": policy.get("publication_status") if policy else "unavailable",
            "full_validation_gain": None,
        },
        "costs": {
            "collection_5": collection_usage,
            "test_48": test_usage,
            "v1_calibration": calibration,
            "connectivity": connectivity,
            "full_total": full_total,
            "observed_total_so_far": observed_total,
            "known_part_total_so_far": known_total,
            "sdk_recovery": {
                "kind": "offline_scored_tree_replay",
                "action_budgets": plan.get("optimization", {}).get("budgets"),
                "budget_unit": "recorded tree actions, not LLM requests/tokens",
                "counted_as_provider_calls": False,
            },
        },
        "measurement_notes": {
            "context_activity_unit": "snapshot-observed views/groups, not operation counts",
            "sample": "6 tasks with 2 scheduling repeats; not 12 independent tasks",
            "inferential_statistics": "none",
            "cache_missing": "unavailable, never zero-imputed; complete hit rate withheld without full field coverage",
            "agent_success_vs_final": "Agent terminal failure is a failed protocol outcome; unavailable final quality stays None, never a zero grade.",
            "comparison_scope": "All 48 terminal slots and equal tool-definition sets required. No incomplete ranking or causal/generalization claim.",
            "provider_usage": "Settled normalized Response.usage, not reservation estimates or independently verified invoice.",
            "requests_unit": "Durable dispatch reservations; unknown requests may have reached the provider. Unknown usage is not zero.",
            "currency": None,
            "money": None,
        },
        "input_hashes": {
            "campaign": reader.hashes[root / "campaign.json"],
            "plan": reader.hashes[root / "plan.json"],
            "journal": reader.hashes[root / "journal.jsonl"],
        },
    }
    reader.verify_snapshot()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the input campaign")
    parser.add_argument("--calibration-root", type=Path)
    parser.add_argument("--connectivity-record", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--require-complete", action="store_true", help="default: exit 2 when test evidence is not fully comparable"
    )
    modes.add_argument("--allow-incomplete", action="store_true", help="allow coverage-only snapshots")
    args = parser.parse_args(argv)
    try:
        root, output = args.root.resolve(), args.output.resolve()
        calibration = (
            args.calibration_root.resolve() if args.calibration_root else root.parent / "live-rsi-20261114-campaign-v1"
        )
        connectivity = (
            args.connectivity_record.resolve()
            if args.connectivity_record
            else root.parent / "live-rsi-20261114-connectivity" / "request.json"
        )
        _require(
            not output.exists()
            and all(
                not output.is_relative_to(p) and not p.is_relative_to(output)
                for p in (root, calibration, connectivity.parent)
            ),
            "output must be new and outside the input campaign",
        )
        result = analyze_campaign(
            root, calibration_root=args.calibration_root, connectivity_path=args.connectivity_record
        )
        output.mkdir(parents=True, exist_ok=False)
        with (output / "analysis.json").open("xb") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n")
        (output / "analysis.sha256").write_text(_sha((output / "analysis.json").read_bytes()), encoding="ascii")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "status": result["comparisons"]["status"],
                    "test_terminal": result["test"]["coverage"]["terminal"],
                    "test_planned": 48,
                }
            )
        )
        return 0 if args.allow_incomplete or result["comparisons"]["status"] == "complete_descriptive_only" else 2
    except (AnalysisError, OSError, KeyError, TypeError, ValueError):
        print(json.dumps({"status": "rejected", "error_type": "EvidenceOrOutputError"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
