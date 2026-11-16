"""Recompute portable ledger metrics after checking every archived file.

Only plan.json and ledger.json are parsed. Other manifest files, including model
patches, are streamed as opaque bytes for integrity checks, never interpreted.
No archived absolute host path is followed. The manifest is a local trust anchor,
not a signature; supply --expected-manifest-sha256 to pin its approved bytes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def integer(value):
    return type(value) is int and value >= 0


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def confined_file(root, name):
    """Reject POSIX/Windows absolute paths, traversal and redirected components."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("invalid manifest relative path")
    relative = PurePosixPath(name)
    if (relative.is_absolute() or PureWindowsPath(name).drive
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("manifest path escape rejected: " + name)
    candidate = root.joinpath(*relative.parts)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("manifest path escape rejected: " + name)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("manifest link rejected: " + name)
    if not candidate.is_file():
        raise ValueError("manifest file missing: " + name)
    return candidate


def verify_archive(raw, expected_manifest_sha256=None):
    root = Path(raw).resolve()
    manifest_path = confined_file(root, "manifest.json")
    digest = sha256(manifest_path)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise ValueError("manifest checksum mismatch")
    manifest = load_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be a list")
    checked = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("invalid manifest file entry")
        name = entry.get("file")
        path = confined_file(root, name)
        if name in checked or name == "manifest.json":
            raise ValueError("duplicate or self-referential manifest file")
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid manifest SHA256")
        if not integer(entry.get("bytes")) or path.stat().st_size != entry["bytes"]:
            raise ValueError("archive size mismatch: " + name)
        if sha256(path) != expected:
            raise ValueError("archive checksum mismatch: " + name)
        checked[name] = path
    for required in ("plan.json", "ledger.json"):
        if required not in checked:
            raise ValueError("required archive input is not verified: " + required)
    if manifest.get("plan_sha256") != sha256(checked["plan.json"]):
        raise ValueError("manifest plan checksum mismatch")
    return root, manifest, digest, checked


def field_coverage(values, request_count):
    observed = [value for value in values if integer(value)]
    return {
        "covered_requests": len(observed),
        "request_count": request_count,
        "observed_sum": sum(observed) if observed else None,
        "total_if_complete": sum(observed) if request_count > 0 and len(observed) == request_count else None,
    }


def summarize(requests):
    count = len(requests)
    usages = [row.get("usage") if isinstance(row.get("usage"), dict) else {} for row in requests]
    completions = [usage.get("completion_tokens") for usage in usages]
    totals = [usage.get("total_tokens") for usage in usages]
    inputs = [total - completion if integer(total) and integer(completion) and total >= completion else None
              for total, completion in zip(totals, completions)]
    token_complete = count > 0 and all(integer(value) for value in inputs)
    costs = [row.get("cost_micro_usd") for row in requests]
    known_costs = [cost for cost in costs if integer(cost)]
    cost_complete = len(known_costs) == count
    cost = sum(known_costs) if cost_complete else None
    return {
        "requests": count,
        "request_statuses": dict(sorted(Counter(str(row.get("status")) for row in requests).items())),
        "cost_micro_usd": cost,
        "cost_usd": float(Decimal(cost) / 1_000_000) if cost is not None else None,
        "cost_complete": cost_complete,
        "cost_covered_requests": len(known_costs),
        "observed_cost_micro_usd": sum(known_costs) if known_costs else (0 if count == 0 else None),
        "tokens": {
            "complete": token_complete,
            "input_tokens": sum(inputs) if token_complete else None,
            "completion_tokens": sum(completions) if token_complete else None,
            "total_tokens": sum(totals) if token_complete else None,
            "input_coverage": field_coverage(inputs, count),
            "completion_coverage": field_coverage(completions, count),
            "total_coverage": field_coverage(totals, count),
            "normalized_prompt_tokens": field_coverage([u.get("prompt_tokens") for u in usages], count),
            "cache_read_tokens": field_coverage([u.get("cache_read_tokens") for u in usages], count),
            "cache_write_tokens": field_coverage([u.get("cache_write_tokens") for u in usages], count),
        },
    }


def recompute(raw, expected_manifest_sha256=None):
    _, manifest, manifest_sha, checked = verify_archive(raw, expected_manifest_sha256)
    plan = load_json(checked["plan.json"])
    ledger = load_json(checked["ledger.json"])
    trials = plan.get("trials")
    requests = ledger.get("requests")
    if not isinstance(trials, list) or not isinstance(requests, list):
        raise ValueError("plan trials and ledger requests must be lists")
    mapping = {}
    trial_ids = set()
    for trial in trials:
        run_id, trial_id = trial.get("run_id"), trial.get("trial_id")
        if (not isinstance(run_id, str) or not run_id or run_id in mapping
                or not isinstance(trial_id, str) or not trial_id or trial_id in trial_ids
                or trial.get("arm") not in ("on", "off")):
            raise ValueError("ambiguous plan trial/run ID or arm")
        mapping[run_id] = trial
        trial_ids.add(trial_id)
    request_ids = set()
    for row in requests:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                or not isinstance(row.get("request_id"), str) or not row["request_id"]
                or row["request_id"] in request_ids):
            raise ValueError("invalid or duplicate ledger request")
        request_ids.add(row["request_id"])
    batch = [row for row in requests if row["run_id"] in mapping]
    historical = [row for row in requests if row["run_id"] not in mapping]
    per_trial = []
    for trial in trials:
        per_trial.append({
            "trial_id": trial["trial_id"], "run_id": trial["run_id"],
            "instance_id": trial.get("instance_id"), "arm": trial["arm"],
            **summarize([row for row in batch if row["run_id"] == trial["run_id"]]),
        })
    return {
        "schema_version": 1,
        "generation_source_commit": manifest.get("source_commit"),
        "adapter_protocol": plan.get("adapter_protocol"),
        "integrity": {
            "manifest_sha256": manifest_sha, "verified_manifest_files": len(checked),
            "archive_files_including_manifest": len(checked) + 1,
            "plan_sha256": sha256(checked["plan.json"]),
            "raw_ledger_sha256": sha256(checked["ledger.json"]),
            "parsed_files": ["plan.json", "ledger.json"],
        },
        "definitions": {
            "scope": "Ledger accounting and field coverage only; no scoring or causal comparison.",
            "input_tokens": "Per request total_tokens - completion_tokens; aggregate only when both fields are complete and consistent.",
            "normalized_prompt_tokens": "Separate normalized field; never substituted for all input tokens.",
            "coverage": "observed_sum covers present valid values only; total_if_complete is null when any request lacks the field, or no requests exist.",
            "cost": "Sum persisted cost_micro_usd. Conservative all-input cold/peak-price estimate, rounded upward to whole micro-USD per request; not a provider invoice. Not recalculated from token totals.",
            "zero_requests": "No-request trials have known zero ledger cost; token fields have no observations and remain null.",
        },
        "ledger_pricing": ledger.get("pricing"),
        "global": summarize(requests), "batch": summarize(batch), "historical": summarize(historical),
        "arms": {arm: summarize([row for row in batch if mapping[row["run_id"]]["arm"] == arm])
                 for arm in ("on", "off")},
        "trials": per_trial,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path(__file__).resolve().parent.parent / "raw")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--output", type=Path, help="Default: stdout; refuses to overwrite raw archive files")
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.resolve().is_relative_to(args.raw_dir.resolve()):
            raise ValueError("output must not overwrite any raw archive path")
        result = recompute(args.raw_dir, args.expected_manifest_sha256)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, "metrics verification failed: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
