"""Strict paired analysis and claim gates for frozen quality campaigns."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import random
from typing import Any

from .protocol import ARM_SPECS, DEFAULT_ARMS, ProtocolError


@dataclass(frozen=True)
class TrialOutcome:
    task_id: str
    arm: str
    trial: int
    status: str
    official_reward: int | None
    requests: int
    total_tokens: int | None
    cache_read_tokens: int | None
    cost_micro_usd: int | None
    usage_complete: bool
    prompt_tokens: int | None = None
    cache_write_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ProtocolError("outcome.task_id must be nonempty text")
        if self.arm not in ARM_SPECS:
            raise ProtocolError("outcome.arm is unknown")
        if type(self.trial) is not int or self.trial < 0:
            raise ProtocolError("outcome.trial must be nonnegative")
        if not isinstance(self.status, str) or not self.status:
            raise ProtocolError("outcome.status must be nonempty text")
        if self.official_reward not in (None, 0, 1):
            raise ProtocolError("official_reward must be 0, 1 or null")
        if type(self.requests) is not int or self.requests < 0:
            raise ProtocolError("requests must be a nonnegative integer")
        for name, value in (
            ("total_tokens", self.total_tokens),
            ("cache_read_tokens", self.cache_read_tokens),
            ("cost_micro_usd", self.cost_micro_usd),
            ("prompt_tokens", self.prompt_tokens),
            ("cache_write_tokens", self.cache_write_tokens),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ProtocolError(f"{name} must be a nonnegative integer or null")
        if type(self.usage_complete) is not bool:
            raise ProtocolError("usage_complete must be boolean")


def parse_outcomes(rows: Sequence[Mapping[str, Any]]) -> list[TrialOutcome]:
    if not isinstance(rows, Sequence):
        raise ProtocolError("outcomes must be a sequence")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ProtocolError(f"outcomes[{index}] must be an object")
    return [
        TrialOutcome(
            task_id=row.get("task_id"),
            arm=row.get("arm"),
            trial=row.get("trial"),
            status=row.get("status"),
            official_reward=row.get("official_reward"),
            requests=row.get("requests"),
            total_tokens=row.get("total_tokens"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cost_micro_usd=row.get("cost_micro_usd"),
            usage_complete=row.get("usage_complete"),
            prompt_tokens=row.get("prompt_tokens"),
            cache_write_tokens=row.get("cache_write_tokens"),
        )
        for row in rows
    ]


def _mean(values: Sequence[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ProtocolError("cannot take a quantile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def cluster_bootstrap_ci(
    task_deltas: Mapping[str, float],
    *,
    iterations: int = 10_000,
    confidence: float = 0.95,
    seed: int = 20261118,
) -> tuple[float, float]:
    if type(iterations) is not int or iterations < 1:
        raise ProtocolError("bootstrap iterations must be positive")
    if not math.isfinite(confidence) or not 0 < confidence < 1:
        raise ProtocolError("confidence must be between zero and one")
    tasks = sorted(task_deltas)
    if not tasks:
        raise ProtocolError("bootstrap requires at least one task")
    values = [float(task_deltas[task]) for task in tasks]
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(sum(sample) / len(sample))
    alpha = (1 - confidence) / 2
    return _quantile(estimates, alpha), _quantile(estimates, 1 - alpha)


def exact_sign_test(values: Sequence[float]) -> dict[str, Any]:
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    ties = sum(value == 0 for value in values)
    count = positive + negative
    if count == 0:
        p_value = 1.0
    else:
        tail = min(positive, negative)
        probability = sum(math.comb(count, index) for index in range(tail + 1))
        p_value = min(1.0, 2 * probability / (2**count))
    return {
        "positive_tasks": positive,
        "negative_tasks": negative,
        "tie_tasks": ties,
        "p_value_two_sided": p_value,
    }


def analyze(
    outcomes: Sequence[TrialOutcome],
    *,
    plan: Mapping[str, Any] | None = None,
    baseline: str = "react",
    treatment: str = "mea",
    bootstrap_iterations: int = 10_000,
    seed: int = 20261118,
    non_inferiority_margin: float = 0.03,
) -> dict[str, Any]:
    if baseline not in DEFAULT_ARMS or treatment not in DEFAULT_ARMS:
        raise ProtocolError("baseline/treatment must be frozen quality arms")
    if baseline == treatment:
        raise ProtocolError("baseline and treatment must differ")
    if not outcomes:
        raise ProtocolError("outcomes must be nonempty")
    keyed: dict[tuple[str, str, int], TrialOutcome] = {}
    for outcome in outcomes:
        key = (outcome.task_id, outcome.arm, outcome.trial)
        if key in keyed:
            raise ProtocolError(f"duplicate outcome: {key}")
        keyed[key] = outcome

    if plan is not None:
        expected = {
            (slot["task_id"], slot["arm"], slot["trial"])
            for slot in plan["slots"]
        }
        actual = set(keyed)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ProtocolError(
                f"outcomes differ from frozen slots: missing={missing}, extra={extra}"
            )

    tasks = sorted(
        {
            task_id
            for task_id, arm, _trial in keyed
            if arm in {baseline, treatment}
        }
    )
    if not tasks:
        raise ProtocolError("no paired tasks found")

    grouped: dict[tuple[str, str], list[TrialOutcome]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.task_id in tasks and outcome.arm in {baseline, treatment}:
            grouped[(outcome.task_id, outcome.arm)].append(outcome)

    trial_counts: dict[str, int] = {}
    validity_errors: list[str] = []
    task_deltas: dict[str, float] = {}
    for task_id in tasks:
        base_rows = sorted(grouped.get((task_id, baseline), []), key=lambda row: row.trial)
        treat_rows = sorted(grouped.get((task_id, treatment), []), key=lambda row: row.trial)
        if len(base_rows) != len(treat_rows) or len(base_rows) < 3:
            validity_errors.append(f"{task_id}: paired trial counts differ or are below three")
            continue
        base_trials = [row.trial for row in base_rows]
        treat_trials = [row.trial for row in treat_rows]
        if base_trials != list(range(len(base_trials))) or treat_trials != base_trials:
            validity_errors.append(f"{task_id}: trial ids are not a contiguous paired range")
            continue
        task_valid = True
        for row in [*base_rows, *treat_rows]:
            if row.status != "scored":
                validity_errors.append(f"{task_id}/{row.arm}/{row.trial}: status {row.status}")
                task_valid = False
            if row.official_reward not in (0, 1):
                validity_errors.append(f"{task_id}/{row.arm}/{row.trial}: reward missing")
                task_valid = False
            if not row.usage_complete:
                validity_errors.append(f"{task_id}/{row.arm}/{row.trial}: usage incomplete")
                task_valid = False
        if not task_valid:
            continue
        base_mean = sum(row.official_reward for row in base_rows) / len(base_rows)
        treat_mean = sum(row.official_reward for row in treat_rows) / len(treat_rows)
        task_deltas[task_id] = treat_mean - base_mean
        trial_counts[task_id] = len(base_rows)

    if not task_deltas:
        raise ProtocolError("no valid paired task outcomes")

    deltas = [task_deltas[task] for task in sorted(task_deltas)]
    mean_delta = sum(deltas) / len(deltas)
    ci_low, ci_high = cluster_bootstrap_ci(
        task_deltas,
        iterations=bootstrap_iterations,
        seed=seed,
    )
    sign_test = exact_sign_test(deltas)

    all_rows = [
        row
        for row in outcomes
        if row.task_id in tasks and row.arm in {baseline, treatment}
    ]
    requests_by_arm = {
        arm: sum(row.requests for row in all_rows if row.arm == arm)
        for arm in (baseline, treatment)
    }
    tokens_by_arm = {
        arm: sum(
            (row.total_tokens or 0)
            for row in all_rows
            if row.arm == arm and row.usage_complete
        )
        for arm in (baseline, treatment)
    }
    cost_by_arm = {
        arm: sum(
            (row.cost_micro_usd or 0)
            for row in all_rows
            if row.arm == arm and row.usage_complete
        )
        for arm in (baseline, treatment)
    }
    cache_by_arm: dict[str, dict[str, Any]] = {}
    for arm in (baseline, treatment):
        arm_rows = [row for row in all_rows if row.arm == arm]
        observed = bool(arm_rows) and all(
            row.usage_complete
            and row.prompt_tokens is not None
            and row.cache_read_tokens is not None
            and row.cache_write_tokens is not None
            for row in arm_rows
        )
        if observed:
            prompt_tokens = sum(row.prompt_tokens or 0 for row in arm_rows)
            cache_read_tokens = sum(row.cache_read_tokens or 0 for row in arm_rows)
            cache_write_tokens = sum(row.cache_write_tokens or 0 for row in arm_rows)
            denominator = prompt_tokens + cache_read_tokens + cache_write_tokens
            cache_by_arm[arm] = {
                "prompt_tokens": prompt_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cache_hit_rate": (
                    cache_read_tokens / denominator if denominator > 0 else None
                ),
            }
        else:
            cache_by_arm[arm] = {
                "prompt_tokens": None,
                "cache_read_tokens": (
                    sum(row.cache_read_tokens or 0 for row in arm_rows)
                    if arm_rows
                    else None
                ),
                "cache_write_tokens": None,
                "cache_hit_rate": None,
            }

    def ratio(treatment_value: int, baseline_value: int) -> float | None:
        if baseline_value <= 0:
            return None
        return treatment_value / baseline_value

    plan_valid = (
        plan is not None
        and plan.get("stage") == "confirmatory"
        and plan.get("claim_ceiling") == "quality_and_benchmark"
        and plan.get("task_count") == len(tasks)
    )
    analysis_valid = not validity_errors and len(task_deltas) == len(tasks)
    quality_claim = (
        plan_valid
        and analysis_valid
        and mean_delta > 0
        and ci_low > 0
        and sign_test["p_value_two_sided"] < 0.05
    )
    cost_ratio = ratio(cost_by_arm[treatment], cost_by_arm[baseline])
    request_ratio = ratio(requests_by_arm[treatment], requests_by_arm[baseline])
    efficiency_claim = (
        plan_valid
        and analysis_valid
        and ci_low >= -non_inferiority_margin
        and ((cost_ratio is not None and cost_ratio < 1)
             or (request_ratio is not None and request_ratio < 1))
    )
    claims = []
    if quality_claim:
        claims.append("benchmark_quality_improvement")
    if efficiency_claim:
        claims.append("quality_non_inferior_with_lower_cost_or_requests")
    if plan is None:
        claim_ceiling = "descriptive_only"
    else:
        claim_ceiling = plan.get("claim_ceiling", "descriptive_only")
    return {
        "protocol": (
            plan.get("protocol") if plan is not None else "long-horizon-quality-analysis"
        ),
        "baseline": baseline,
        "treatment": treatment,
        "claim_ceiling": claim_ceiling,
        "task_count": len(tasks),
        "analyzed_task_count": len(task_deltas),
        "trial_counts": dict(sorted(Counter(trial_counts.values()).items())),
        "validity_errors": validity_errors,
        "analysis_valid": analysis_valid,
        "task_deltas": dict(sorted(task_deltas.items())),
        "mean_delta": mean_delta,
        "cluster_bootstrap_95pct": [ci_low, ci_high],
        "sign_test": sign_test,
        "resources": {
            "requests": requests_by_arm,
            "total_tokens": tokens_by_arm,
            "cost_micro_usd": cost_by_arm,
            "cache": cache_by_arm,
            "request_ratio": request_ratio,
            "token_ratio": ratio(tokens_by_arm[treatment], tokens_by_arm[baseline]),
            "cost_ratio": cost_ratio,
        },
        "claims": claims,
        "claim_boundaries": [
            "Pilot campaigns are descriptive-only and cannot support quality claims.",
            "Benchmark claims require the complete 89-task set and official verifier.",
            "Invalid, incomplete or unknown-usage trials invalidate the confirmatory gate.",
            "Recovery and immutable-path cohorts must be analyzed separately.",
        ],
    }
