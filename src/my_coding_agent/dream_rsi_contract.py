"""Pure contracts for trusted, bounded DREAM-RSI parameter policies."""

from __future__ import annotations

import hashlib
import json
import math

SOURCE_COMMIT = "8ec35bd2538220517ce1bfca4282db21b8c518db"
MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_NODES = 100_000


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _cell(value):
    if not isinstance(value, dict) or value.get("evaluated") is not True or not _number(value.get("combined_score")):
        raise ValueError("trace must contain evaluated cells with finite scores")
    for key in ("n_valid", "n_total"):
        if value.get(key) is not None and not _integer(value[key]):
            raise ValueError("invalid score counts")
    if value.get("n_valid") is not None and value.get("n_total") is not None and value["n_valid"] > value["n_total"]:
        raise ValueError("invalid score count relation")
    for key in ("sol_times_ms", "max_gaps"):
        values = value.get(key, [])
        if not isinstance(values, list) or any(v is not None and not _number(v) for v in values):
            raise ValueError("invalid numeric score details")
    if not isinstance(value.get("valids", []), list) or any(
        v is not None and type(v) is not bool for v in value.get("valids", [])
    ):
        raise ValueError("invalid score validity details")


def validate_trace(value: dict) -> dict:
    """Validate the scored subset of upstream raw trace v1 without touching node paths.

    This verifies structure and scored status, not the truth of the supplied task
    evaluator. Callers remain responsible for authentic score provenance.
    """
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("expected DREAM-RSI raw trace version 1")
    if not _integer(value.get("n_problems"), minimum=1):
        raise ValueError("trace n_problems must be positive")
    _cell(value.get("baseline"))
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError("trace needs a bounded nonempty node list")
    by_id, branches = {}, {}
    for node in nodes:
        if not isinstance(node, dict) or not _integer(node.get("branch")) or not _integer(node.get("attempt")):
            raise ValueError("invalid branch/attempt")
        branch, attempt = node["branch"], node["attempt"]
        if branch >= MAX_NODES or attempt >= MAX_NODES:
            raise ValueError("branch/attempt exceeds trace bounds")
        identity = node.get("id")
        if identity != f"b{branch}_a{attempt}" or identity in by_id:
            raise ValueError("invalid or duplicate discovery node ID")
        if node.get("status") not in (None, "done", "failed", "completed"):
            raise ValueError("trace node is not terminal")
        if not isinstance(node.get("path"), str) or not isinstance(node.get("intent", ""), str):
            raise ValueError("invalid discovery node metadata")
        if not isinstance(node.get("tags", []), list) or any(not isinstance(v, str) for v in node.get("tags", [])):
            raise ValueError("invalid discovery node tags")
        expected_parent = None if attempt == 0 else f"b{branch}_a{attempt - 1}"
        if node.get("parent_id") != expected_parent:
            raise ValueError("discovery parent must be the previous attempt of its branch")
        _cell(node.get("raw"))
        by_id[identity] = node
        branches.setdefault(branch, set()).add(attempt)
    if set(branches) != set(range(len(branches))):
        raise ValueError("discovery branches must form a contiguous grid prefix")
    for attempts in branches.values():
        if attempts != set(range(max(attempts) + 1)):
            raise ValueError("discovery attempts must form a contiguous scored prefix")
    return {"nodes": len(nodes), "branches": len(branches), "max_attempt": max(n["attempt"] for n in nodes)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate trace JSON key")
        result[key] = value
    return result


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def checksum(value) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"max_width", "max_refine"}:
        raise ValueError("parameters must contain only max_width/max_refine")
    width, depth = parameters["max_width"], parameters["max_refine"]
    if type(width) is not int or not 1 <= width <= 64 or type(depth) is not int or not 0 <= depth <= 64:
        raise ValueError("parameter bounds require strict integers")
    return dict(parameters)


def policy_identity(parameters) -> str:
    if parameters is not None:
        validate_parameters(parameters)
    return checksum(canonical({"schema_version": 1, "source_commit": SOURCE_COMMIT, "parameters": parameters}))


def effective_parameters(parameters, shape):
    if parameters is None:
        return {"max_width": shape["branches"], "max_refine": shape["max_attempt"]}
    value = validate_parameters(parameters)
    return {
        "max_width": min(value["max_width"], shape["branches"]),
        "max_refine": min(value["max_refine"], shape["max_attempt"]),
    }


def semantic_identity(trace):
    """Conservative overlap identity, also invariant to renumbered root branches."""
    validate_trace(trace)
    branches = {}
    for node in trace["nodes"]:
        branches.setdefault(node["branch"], []).append((node["attempt"], float(node["raw"]["combined_score"])))
    return checksum(
        canonical(
            {
                "baseline": float(trace["baseline"]["combined_score"]),
                "branches": sorted([score for _, score in sorted(chain)] for chain in branches.values()),
            }
        )
    )


def expected_waves(trace, parameters, budget):
    shape = validate_trace(trace)
    effective = effective_parameters(parameters, shape)
    waves, spent = [], 0
    by_depth = {}
    for node in trace["nodes"]:
        if node["attempt"] <= effective["max_refine"] and node["branch"] < effective["max_width"]:
            by_depth.setdefault(node["attempt"], []).append(node)
    for depth in sorted(by_depth):
        if spent >= budget:
            break
        cells = sorted(by_depth[depth], key=lambda n: n["branch"])
        cells = cells[: max(0, budget - spent)]
        if cells:
            waves.append(cells)
            spent += len(cells)
    return waves


def verify_leaf(trace, summary, episode, parameters, budget, max_workers):
    """Recompute metrics from frozen scores, whitelist schedule and actual decisions.

    The only executable policy is the upstream fixed wave algorithm. Neither its
    SimResult nor a caller's top-level eligibility flag is scoring authority.
    """
    if not isinstance(summary, dict) or not isinstance(episode, dict):
        raise ValueError("invalid leaf output schema")
    if not _integer(budget, minimum=1) or budget > MAX_NODES or not _integer(max_workers, minimum=1):
        raise ValueError("invalid leaf budget/parallelism")
    shape = validate_trace(trace)
    effective = effective_parameters(parameters, shape)
    if (
        summary.get("status") != "completed"
        or summary.get("upstream", {}).get("commit") != SOURCE_COMMIT
        or summary["upstream"].get("version") != "0.1.0"
    ):
        raise ValueError("leaf source/status mismatch")
    if summary.get("policy_hash") != policy_identity(parameters):
        raise ValueError("leaf policy identity mismatch")
    if canonical(summary.get("parameters")) != canonical({"requested": parameters, "effective": effective}):
        raise ValueError("leaf parameter clamp mismatch")
    if (
        type(summary.get("budget_limit")) is not int
        or type(summary.get("max_workers")) is not int
        or summary.get("budget_limit") != budget
        or summary.get("max_workers") != max_workers
    ):
        raise ValueError("leaf execution limits differ")
    waves = expected_waves(trace, parameters, budget)
    result = summary.get("result")
    if (
        not isinstance(result, dict)
        or not _number(result.get("best"))
        or any(not _integer(result.get(k)) for k in ("budget", "decision_rounds", "effective_sequential_rounds"))
        or not isinstance(result.get("batch_sizes"), list)
        or any(not _integer(v, minimum=1) for v in result["batch_sizes"])
    ):
        raise ValueError("leaf SimResult contains invalid numeric metrics")
    events = episode.get("events")
    if (
        episode.get("mode") != "replay"
        or episode.get("max_parallelism") != max_workers
        or not isinstance(events, list)
        or len(events) != len(waves)
    ):
        raise ValueError("leaf decision count/mode mismatch")
    # The pinned TraceEnv.reset starts at 0, not the task's initial program score.
    # The initial program remains part of the normalization ceiling only.
    spent, best, sequential = 0, 0.0, 0
    sizes, curve = [], []
    for index, (event, cells) in enumerate(zip(events, waves), 1):
        pairs = [(n["branch"], n["attempt"]) for n in cells]
        batch = event.get("batch", [])
        if (
            not isinstance(batch, list)
            or any(
                not isinstance(n, dict) or type(n.get("branch")) is not int or type(n.get("attempt")) is not int
                for n in batch
            )
            or [(n.get("branch"), n.get("attempt")) for n in batch] != pairs
        ):
            raise ValueError("leaf decisions differ from controlled wave policy")
        before = event.get("before", {})
        if (
            type(event.get("round")) is not int
            or event["round"] != index
            or type(before.get("budget")) is not int
            or before["budget"] != spent
            or not _number(before.get("best"))
            or before["best"] != best
        ):
            raise ValueError("leaf prefix accounting mismatch")
        outcomes = event.get("outcomes")
        if not isinstance(outcomes, list) or len(outcomes) != len(cells):
            raise ValueError("leaf outcome count differs")
        for outcome, node in zip(outcomes, cells):
            spent += 1
            score = node["raw"]["combined_score"]
            best = max(best, score)
            if (
                any(type(outcome.get(k)) is not int for k in ("branch", "attempt", "budget"))
                or (outcome.get("branch"), outcome.get("attempt")) != (node["branch"], node["attempt"])
                or not _number(outcome.get("score"))
                or not _number(outcome.get("best_after"))
                or outcome.get("score") != score
                or outcome.get("evaluated") is not True
                or outcome.get("budget") != spent
                or outcome.get("best_after") != best
            ):
                raise ValueError("leaf scored outcome/accounting mismatch")
            curve.append({"budget": spent, "best": best})
        sizes.append(len(cells))
        sequential += math.ceil(len(cells) / max_workers)
        parallel = event.get("parallelism", {})
        if (
            any(
                type(parallel.get(k)) is not int
                for k in (
                    "worker_cap",
                    "batch_size",
                    "effective_sequential_rounds",
                    "decision_rounds_total",
                    "effective_sequential_rounds_total",
                )
            )
            or parallel.get("worker_cap") != max_workers
            or parallel.get("batch_size") != len(cells)
            or parallel.get("effective_sequential_rounds") != math.ceil(len(cells) / max_workers)
            or parallel.get("decision_rounds_total") != index
            or parallel.get("effective_sequential_rounds_total") != sequential
            or type(event.get("after", {}).get("budget")) is not int
            or not _number(event.get("after", {}).get("best"))
            or event.get("after", {}).get("budget") != spent
            or event["after"].get("best") != best
        ):
            raise ValueError("leaf decision cost accounting mismatch")
    expected = {
        "best": best,
        "budget": spent,
        "decision_rounds": len(waves),
        "effective_sequential_rounds": sequential,
        "batch_sizes": sizes,
    }
    if summary.get("result") != expected:
        raise ValueError("leaf SimResult does not match trusted scores/decisions")
    final = episode.get("final", {})
    if (
        any(final.get(k) != v for k, v in expected.items())
        or final.get("curve") != curve
        or any(type(final.get(k)) is not int for k in ("budget", "decision_rounds", "effective_sequential_rounds"))
        or not _number(final.get("best"))
        or any(type(p.get("budget")) is not int or not _number(p.get("best")) for p in final.get("curve", []))
    ):
        raise ValueError("leaf final decision metrics mismatch")
    ceiling = max(trace["baseline"]["combined_score"], *(n["raw"]["combined_score"] for n in trace["nodes"]))
    if not _number(ceiling) or ceiling <= 0 or not _number(best) or not 0 <= best / ceiling <= 1:
        raise ValueError("trace has no positive finite normalization ceiling")
    return best / ceiling
