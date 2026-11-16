"""Raw evidence schema, reproducibility manifest, derived statistics and CLI runner."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

MODULES = ("tools", "session", "context", "memory", "permission", "runtime", "observation", "model")
ROOT = Path(__file__).resolve().parents[2]


def aggregate(rows: list[dict]) -> dict:
    """Compute every result from raw observations; unavailable provider data stays null."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["module"], row["case"], row["arm"])].append(row)
    groups = []
    for (module, case, arm), samples in sorted(grouped.items()):
        durations = sorted(s["duration_seconds"] for s in samples)
        keys = set().union(*(s["metrics"].keys() for s in samples))
        metrics = {}
        for key in sorted(keys):
            values = [s["metrics"][key] for s in samples if isinstance(s["metrics"].get(key), (int, float))]
            if values:
                metrics[key] = dict(mean=statistics.mean(values), minimum=min(values), maximum=max(values))
        groups.append(
            dict(
                module=module,
                case=case,
                arm=arm,
                kind=samples[0]["kind"],
                samples=len(samples),
                passed=sum(s["passed"] for s in samples),
                failed=sum(not s["passed"] for s in samples),
                duration_median_seconds=statistics.median(durations),
                duration_p95_seconds=durations[max(0, math.ceil(0.95 * len(durations)) - 1)],
                metrics=metrics,
            )
        )
    return dict(
        schema_version=1,
        records=len(rows),
        unique_cases=len({(s["module"], s["case"]) for s in rows}),
        failures=sum(not s["passed"] for s in rows),
        groups=groups,
        tokens=None,
        cache=None,
        quality=None,
        limits="Offline deterministic fixtures establish engineering contracts, not real-model quality or throughput.",
    )


def resolved_sources(paths):
    """Record actual imported files, rejecting accidental parent-checkout imports."""
    resolved = []
    for entry in paths:
        source = entry.split(":", 1)[0]
        module_name = source.removeprefix("src/").removesuffix(".py").replace("/", ".")
        module = importlib.import_module(module_name)
        actual = Path(module.__file__).resolve()
        expected = (ROOT / source).resolve()
        if actual != expected:
            raise RuntimeError("production module imported from unexpected checkout")
        resolved.append(
            dict(path=source, actual_path=str(actual), sha256=hashlib.sha256(actual.read_bytes()).hexdigest())
        )
    return resolved


def manifest(seed, repeats, suites):
    def git(*args):
        completed = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True)
        return completed.stdout.strip()

    digest = hashlib.sha256()
    for folder in ("src", "experiments/harness_eval", "tests/experiments"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    try:
        sha, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        sha, dirty = None, None
    licenses = [p.name for p in ROOT.glob("LICENSE*") if p.is_file()]
    return dict(
        schema_version=1,
        created_at=datetime.now(timezone.utc).isoformat(),
        git_sha=sha,
        dirty_worktree=dirty,
        source_sha256=digest.hexdigest(),
        source_hash_scope=["src/**/*.py", "experiments/harness_eval/**/*.py", "tests/experiments/**/*.py"],
        python=sys.version,
        platform=platform.platform(),
        seed=seed,
        config=dict(mode="offline", suites=suites, repeats=repeats),
        license_files=licenses,
        fixture_license="Synthetic project-authored fixtures; no external benchmark dataset.",
        provider_requests=0,
        tokens=None,
        cache=None,
        quality=None,
    )


def report(summary):
    lines = [
        "# Offline harness evaluation",
        "",
        "Synthetic fixtures exercise actual production paths. Paired observations are not evidence of real-model quality.",
        "Passing means the expected engineering contract was observed; an off arm may intentionally expose a safety failure.",
        "A comparison wrapper is explicitly identified when there is no production feature switch.",
        "Contract cases have one arm. Repeats are timing samples, not additional unique tasks.",
        "",
        f"Unique cases: {summary['unique_cases']}; raw records: {summary['records']}; assertion failures: {summary['failures']}.",
        "",
    ]
    for group in summary["groups"]:
        lines += [
            f"## {group['module']} / {group['case']} / {group['arm']} ({group['kind']})",
            "",
            f"Passed {group['passed']}/{group['samples']}; median {group['duration_median_seconds']:.6f}s; p95 {group['duration_p95_seconds']:.6f}s.",
            "Metrics: " + json.dumps(group["metrics"], sort_keys=True),
            "",
        ]
    lines += [
        "Tokens, cache usage and real-model quality: unavailable (null).",
        "Durations include local fixture setup and filesystem work. Small samples are not throughput benchmarks.",
    ]
    return "\n".join(lines) + "\n"


async def run_suite(*, suites, repeats=3, output=Path("harness-eval-runs"), seed=0):
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1 or repeats > 1000:
        raise ValueError("repeats must be an integer in [1, 1000]")
    if not suites or any(s not in (*MODULES, "all") for s in suites):
        raise ValueError("unknown or empty suite")
    selected = list(MODULES) if "all" in suites else list(dict.fromkeys(suites))
    from .scenarios import cases

    plan = [case for case in cases() if case.module in selected]
    run_dir = Path(output) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12])
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "manifest.json").write_text(json.dumps(manifest(seed, repeats, selected), indent=2), encoding="utf-8")
    rng, rows = random.Random(seed), []
    with (run_dir / "raw.jsonl").open("x", encoding="utf-8") as stream:
        for repeat in range(repeats):
            for case in plan:
                arms = list(case.arms)
                rng.shuffle(arms)
                for arm in arms:
                    start = time.perf_counter()
                    with tempfile.TemporaryDirectory(prefix="harness-eval-") as temporary:
                        try:
                            sources = resolved_sources(case.paths)
                            metrics, assertions, detail = await asyncio.wait_for(
                                case.run(arm, Path(temporary)), timeout=10
                            )
                            failure = None
                        except Exception as exc:
                            metrics, assertions, detail = {}, {"scenario_completed": False}, {}
                            sources = []
                            failure = type(exc).__name__  # Exception messages may contain model inputs or credentials.
                    row = dict(
                        schema_version=1,
                        mode="offline",
                        module=case.module,
                        case=case.name,
                        kind=case.kind,
                        arm=arm,
                        repeat=repeat,
                        seed=seed,
                        duration_seconds=time.perf_counter() - start,
                        metrics=metrics,
                        assertions=assertions,
                        passed=all(assertions.values()) and failure is None,
                        error=failure,
                        evidence=dict(
                            production_paths=case.paths,
                            resolved_sources=sources,
                            intervention=case.intervention,
                            observations=detail,
                        ),
                        tokens=None,
                        cache=None,
                        quality=None,
                    )
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    rows.append(row)
    final_manifest = manifest(seed, repeats, selected)
    initial_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    initial_manifest["source_sha256_after_run"] = final_manifest["source_sha256"]
    initial_manifest["source_changed_during_run"] = initial_manifest["source_sha256"] != final_manifest["source_sha256"]
    (run_dir / "manifest.json").write_text(json.dumps(initial_manifest, indent=2), encoding="utf-8")
    summary = aggregate(rows)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(report(summary), encoding="utf-8")
    return run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", nargs="+", choices=("all", *MODULES), default=["all"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("harness-eval-runs"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        path = asyncio.run(run_suite(suites=args.suite, repeats=args.repeats, output=args.output, seed=args.seed))
    except ValueError as exc:
        parser.error(str(exc))
    print(path.resolve())
    return 1 if json.loads((path / "summary.json").read_text())["failures"] else 0
