"""Offline deadline/progress guard and deterministic long-task ablation.

The guard is task-agnostic. It observes tool events, injects at most one
structured steering decision, and never writes a candidate answer itself. The
ablation uses a fake clock and a temporary Python repository; it does not call
models, APIs, or Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_REPEAT = 20
DEFAULT_TOTAL_SECONDS = 10.0
DEFAULT_WARNING_RATIO = 0.7
DEFAULT_MAX_NO_PROGRESS = 3

STEERING = {
    "kind": "progress_guard_steering",
    "required_order": ["produce_final_artifact", "run_full_validation"],
    "instruction": (
        "Before doing more local checks, persist the requested final deliverable, "
        "then run the required full validation against that exact artifact."
    ),
    "constraints": [
        "Do not replace the agent or synthesize the answer in the guard.",
        "Do not repeat this steering after it has been injected once.",
    ],
}


@dataclass(frozen=True)
class ToolEvent:
    index: int
    elapsed_seconds: float
    name: str
    args: Mapping[str, Any]
    ok: bool
    result: Mapping[str, Any]
    fingerprint: str
    progress: bool
    final_delivery: bool


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {"value": value}


def _default_delivery_predicate(
    _name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    return any(
        payload.get(key) is True
        for payload in (args, result)
        for key in ("final_delivery", "final_artifact", "delivery_complete")
    )


class ProgressGuard:
    """Deadline and stagnation guard with at-most-once steering injection."""

    def __init__(
        self,
        *,
        total_seconds: float = DEFAULT_TOTAL_SECONDS,
        warning_ratio: float = DEFAULT_WARNING_RATIO,
        max_consecutive_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
        clock: Callable[[], float] | None = None,
        delivery_predicate: Callable[[str, Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
    ) -> None:
        if type(total_seconds) not in (int, float) or total_seconds <= 0:
            raise ValueError("total_seconds must be positive")
        if type(warning_ratio) not in (int, float) or not 0 < warning_ratio < 1:
            raise ValueError("warning_ratio must be between zero and one")
        if type(max_consecutive_no_progress) is not int or max_consecutive_no_progress < 1:
            raise ValueError("max_consecutive_no_progress must be a positive integer")
        self.total_seconds = float(total_seconds)
        self.warning_ratio = float(warning_ratio)
        self.max_consecutive_no_progress = max_consecutive_no_progress
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._delivery_predicate = delivery_predicate or _default_delivery_predicate
        self._events: list[ToolEvent] = []
        self._consecutive_no_progress = 0
        self._last_progress_fingerprint: str | None = None
        self._delivered = False
        self._delivery_event: int | None = None
        self._injected = False
        self._injection_event: int | None = None
        self._injection_decision: str | None = None

    @property
    def events(self) -> tuple[ToolEvent, ...]:
        return tuple(self._events)

    def register_tool(
        self,
        name: str,
        args: Mapping[str, Any] | Any,
        ok: bool,
        result: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        """Register one completed tool call and return the current guard decision."""
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be nonempty")
        args_map, result_map = _mapping(args), _mapping(result)
        fingerprint = hashlib.sha256(
            _canonical({"name": name, "args": args_map, "ok": bool(ok), "result": result_map}).encode("utf-8")
        ).hexdigest()
        explicit_progress = result_map.get("progress")
        progress = explicit_progress if type(explicit_progress) is bool else fingerprint != self._last_progress_fingerprint
        final_delivery = self._delivery_predicate(name, args_map, result_map)
        if final_delivery:
            progress = True
        event = ToolEvent(
            index=len(self._events),
            elapsed_seconds=self._elapsed(),
            name=name,
            args=dict(args_map),
            ok=bool(ok),
            result=dict(result_map),
            fingerprint=fingerprint,
            progress=progress,
            final_delivery=final_delivery,
        )
        self._events.append(event)
        if progress:
            self._consecutive_no_progress = 0
            self._last_progress_fingerprint = fingerprint
        else:
            self._consecutive_no_progress += 1
        if final_delivery:
            self._delivered = True
            self._delivery_event = event.index
        return self.poll(trigger=f"tool:{event.index}")

    def poll(self, *, trigger: str = "poll") -> dict[str, Any]:
        """Evaluate the current deadline and progress state without side effects."""
        elapsed = self._elapsed()
        reason: str | None = None
        decision = "continue"
        stop = False
        inject = False

        if elapsed >= self.total_seconds:
            decision, reason, stop = "stop", "time_budget_exhausted", True
        elif self._delivered:
            decision, reason = "delivered", "final_delivery_observed"
        elif self._injected:
            decision, reason = "suppressed", "steering_already_injected"
        elif elapsed / self.total_seconds >= self.warning_ratio:
            decision, reason, inject = "inject", "deadline_warning_without_delivery", True
        elif self._consecutive_no_progress > self.max_consecutive_no_progress:
            decision, reason, inject = "inject", "consecutive_no_progress", True

        if inject:
            self._injected = True
            self._injection_event = len(self._events) - 1 if self._events else None
            self._injection_decision = reason

        return {
            "decision": decision,
            "inject": inject,
            "stop": stop,
            "reason": reason,
            "steering": dict(STEERING) if inject else None,
            "evidence": self._evidence(trigger, elapsed),
        }

    def _elapsed(self) -> float:
        return max(0.0, float(self._clock() - self._started))

    def _evidence(self, trigger: str, elapsed: float) -> dict[str, Any]:
        return {
            "trigger": trigger,
            "event_count": len(self._events),
            "elapsed_seconds": round(elapsed, 6),
            "total_seconds": self.total_seconds,
            "deadline_ratio": round(elapsed / self.total_seconds, 6),
            "warning_ratio": self.warning_ratio,
            "consecutive_no_progress": self._consecutive_no_progress,
            "max_consecutive_no_progress": self.max_consecutive_no_progress,
            "final_delivery": self._delivered,
            "final_delivery_event": self._delivery_event,
            "injection_count": 1 if self._injected else 0,
            "injection_event": self._injection_event,
            "injection_reason": self._injection_decision,
        }


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _write_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "diagnostic.py").write_text("VALUE = 'ready-for-final-assembly'\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_final.py").write_text(
        "import unittest\n\n"
        "from final_artifact import deliver\n\n"
        "class FinalArtifactTests(unittest.TestCase):\n"
        "    def test_final_contract(self):\n"
        "        self.assertEqual(deliver(), {'status': 'complete', 'version': 1})\n",
        encoding="utf-8",
    )
    return root


def _local_validation(repo: Path) -> bool:
    source = (repo / "diagnostic.py").read_text(encoding="utf-8")
    compile(source, str(repo / "diagnostic.py"), "exec")
    return True


def _write_final(repo: Path) -> None:
    (repo / "final_artifact.py").write_text(
        "def deliver():\n"
        "    return {'status': 'complete', 'version': 1}\n",
        encoding="utf-8",
    )


def _run_host_validation(repo: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(repo / "final_artifact.py")],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    test_result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(repo / "tests"), "-v"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    compile_evidence = {
        "returncode": compile_result.returncode,
        "stdout_tail": compile_result.stdout[-800:],
        "stderr_tail": compile_result.stderr[-800:],
    }
    test_evidence = {
        "returncode": test_result.returncode,
        "stdout_tail": test_result.stdout[-1200:],
        "stderr_tail": test_result.stderr[-800:],
    }
    return compile_evidence, test_evidence


def _ablation_once(root: Path, index: int) -> dict[str, Any]:
    baseline_repo = _write_repo(root / f"run-{index}" / "baseline-repo")
    guarded_repo = _write_repo(root / f"run-{index}" / "guarded-repo")

    baseline_clock = FakeClock()
    baseline_checks = 0
    while baseline_clock.now < DEFAULT_TOTAL_SECONDS:
        baseline_clock.advance(1.5)
        if _local_validation(baseline_repo):
            baseline_checks += 1
    baseline_timeout = baseline_clock.now >= DEFAULT_TOTAL_SECONDS and not (baseline_repo / "final_artifact.py").exists()

    guarded_clock = FakeClock()
    guard = ProgressGuard(
        total_seconds=DEFAULT_TOTAL_SECONDS,
        warning_ratio=DEFAULT_WARNING_RATIO,
        max_consecutive_no_progress=8,
        clock=guarded_clock,
    )
    local_checks = 0
    decisions = []
    while guarded_clock.now < DEFAULT_TOTAL_SECONDS * DEFAULT_WARNING_RATIO:
        decision = guard.register_tool(
            "local_validation",
            {"target": "diagnostic.py"},
            True,
            {"passed": True, "progress": False},
        )
        decisions.append(decision)
        local_checks += 1 if _local_validation(guarded_repo) else 0
        guarded_clock.advance(1.5)
    guarded_clock.advance(0.1)
    warning = guard.poll(trigger="deadline_warning")
    if warning["inject"]:
        _write_final(guarded_repo)
        delivery = guard.register_tool(
            "write_file",
            {"path": "final_artifact.py"},
            True,
            {"progress": True, "final_delivery": True},
        )
        compile_evidence, test_evidence = _run_host_validation(guarded_repo)
        validation = guard.register_tool(
            "host_validation",
            {"targets": ["py_compile", "unittest"]},
            compile_evidence["returncode"] == 0 and test_evidence["returncode"] == 0,
            {"progress": True, "compile_rc": compile_evidence["returncode"], "test_rc": test_evidence["returncode"]},
        )
    else:
        delivery = warning
        validation = warning
        compile_evidence, test_evidence = {"returncode": None}, {"returncode": None}

    injection_count = sum(decision["inject"] for decision in [*decisions, warning])
    post_delivery_injects = int(delivery["inject"]) + int(validation["inject"])
    ordered_delivery_before_validation = (guarded_repo / "final_artifact.py").is_file()
    enhanced_pass = bool(
        injection_count == 1
        and delivery["decision"] == "delivered"
        and post_delivery_injects == 0
        and ordered_delivery_before_validation
        and compile_evidence["returncode"] == 0
        and test_evidence["returncode"] == 0
    )
    baseline_pass = bool(baseline_timeout is False and (baseline_repo / "final_artifact.py").is_file())
    signature = hashlib.sha256(
        _canonical(
            {
                "baseline_checks": baseline_checks,
                "baseline_timeout": baseline_timeout,
                "enhanced_local_checks": local_checks,
                "injection_count": injection_count,
                "delivery_decision": delivery["decision"],
                "compile_rc": compile_evidence["returncode"],
                "test_rc": test_evidence["returncode"],
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        "baseline_pass": baseline_pass,
        "enhanced_pass": enhanced_pass,
        "baseline_reason": "timeout_before_final_delivery" if not baseline_pass else None,
        "enhanced_reason": None if enhanced_pass else "guard_or_host_validation_failed",
        "signature": signature,
        "evidence": {
            "baseline": {
                "elapsed_seconds": baseline_clock.now,
                "local_checks": baseline_checks,
                "final_delivery": (baseline_repo / "final_artifact.py").is_file(),
                "timeout": baseline_timeout,
            },
            "enhanced": {
                "warning_decision": warning,
                "delivery_decision": delivery,
                "validation_decision": validation,
                "compile": compile_evidence,
                "unittest": test_evidence,
            },
        },
        "metrics": {
            "baseline": {"local_checks": baseline_checks, "elapsed_seconds": baseline_clock.now},
            "enhanced": {
                "local_checks": local_checks,
                "injection_count": injection_count,
                "injection_elapsed_seconds": warning["evidence"]["elapsed_seconds"],
                "compile_returncode": compile_evidence["returncode"],
                "test_returncode": test_evidence["returncode"],
            },
        },
    }


def run_ablation(*, repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    if type(repeat) is not int or repeat < 1:
        raise ValueError("repeat must be a positive integer")
    baseline_flags, enhanced_flags, signatures = [], [], []
    baseline_reasons, enhanced_reasons = [], []
    first: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="progress-guard-ablation-") as temporary:
        for index in range(repeat):
            result = _ablation_once(Path(temporary), index)
            baseline_flags.append(result["baseline_pass"])
            enhanced_flags.append(result["enhanced_pass"])
            signatures.append(result["signature"])
            baseline_reasons.append(result["baseline_reason"])
            enhanced_reasons.append(result["enhanced_reason"])
            if index == 0:
                first = result
    return {
        "schema_version": 1,
        "experiment": "deadline_progress_guard_ablation",
        "repeat": repeat,
        "configuration": {
            "total_seconds": DEFAULT_TOTAL_SECONDS,
            "warning_ratio": DEFAULT_WARNING_RATIO,
            "ablation_max_consecutive_no_progress": 8,
            "clock": "deterministic fake clock",
        },
        "baseline": {
            "passed": sum(baseline_flags),
            "total": repeat,
            "failure_classes": dict(sorted(Counter(reason or "none" for reason in baseline_reasons).items())),
        },
        "enhanced": {
            "passed": sum(enhanced_flags),
            "total": repeat,
            "failure_classes": dict(sorted(Counter(reason or "none" for reason in enhanced_reasons).items())),
        },
        "deterministic": len(set(signatures)) == 1,
        "distinct_signatures": len(set(signatures)),
        "first_observation": first,
        "scope": {
            "model_calls": False,
            "api_calls": False,
            "docker_calls": False,
            "repository": "temporary Python files with host py_compile and unittest subprocesses",
            "claim": "Scripted mechanism ablation only; not evidence of real-model long-task improvement.",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="deterministic repeats (default: 20)")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_ablation(repeat=args.repeat)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"progress-guard ablation error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
