"""CLI for freezing quality campaigns and analyzing paired outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .analysis import analyze, parse_outcomes
from .protocol import DEFAULT_TRIALS, ProtocolError, freeze_campaign, load_plan


def _write_json(path: str | Path | None, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="long-horizon-quality")
    commands = result.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze")
    freeze.add_argument("--task-manifest", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--campaign-id", required=True)
    freeze.add_argument("--stage", choices=("pilot", "confirmatory"), required=True)
    freeze.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    freeze.add_argument("--seed", type=int, default=20261118)
    freeze.add_argument("--json")

    report = commands.add_parser("analyze")
    report.add_argument("--results", required=True)
    report.add_argument("--plan")
    report.add_argument("--baseline", default="react")
    report.add_argument("--treatment", default="mea")
    report.add_argument("--bootstrap-iterations", type=int, default=10_000)
    report.add_argument("--seed", type=int, default=20261118)
    report.add_argument("--json")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "freeze":
            plan = freeze_campaign(
                task_manifest=args.task_manifest,
                output=args.output,
                campaign_id=args.campaign_id,
                stage=args.stage,
                trials=args.trials,
                seed=args.seed,
            )
            _write_json(args.json, plan)
            return 0
        rows = json.loads(Path(args.results).read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ProtocolError("results JSON must be a list")
        plan = load_plan(args.plan) if args.plan else None
        report = analyze(
            parse_outcomes(rows),
            plan=plan,
            baseline=args.baseline,
            treatment=args.treatment,
            bootstrap_iterations=args.bootstrap_iterations,
            seed=args.seed,
        )
        _write_json(args.json, report)
        return 0
    except (OSError, json.JSONDecodeError, ProtocolError) as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
