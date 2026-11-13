"""Bounded deterministic parameter search; no model, generated code or online RSI."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from .dream_rsi_contract import (
    SOURCE_COMMIT,
    MAX_TRACE_BYTES,
    MAX_NODES,
    canonical,
    checksum,
    policy_identity,
    semantic_identity,
    task_content_identity,
    validate_task_evidence,
    validate_trace,
    verify_leaf,
    _unique_object,
)

OBJECTIVE = "mean-recorded-best-ratio-v1"


def _load(path):
    return json.loads(Path(path).read_bytes(), object_pairs_hook=_unique_object)


def _write(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value))


def _relative(root, name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("invalid relative evidence path")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("evidence path escaped root")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("evidence path escaped root")
    return target


def load_dataset(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        raw = stream.read(256_001)
    if len(raw) > 256_000:
        raise ValueError("dataset manifest exceeds byte limit")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "kind", "source", "entries"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] not in ("synthetic", "observed")
        or not isinstance(value["source"], str)
        or not 1 <= len(value["source"]) <= 2000
        or not isinstance(value["entries"], list)
        or not 2 <= len(value["entries"]) <= 32
    ):
        raise ValueError("invalid dataset schema_v1")
    entries, seen_ids, files, tasks, meanings, contents = [], set(), set(), {}, {}, {}
    for item in value["entries"]:
        fields = {"id", "task_id", "split", "file", "sha256", "provenance"}
        if not isinstance(item, dict) or set(item) not in (fields, fields | {"task_evidence_sha256"}):
            raise ValueError("invalid dataset entry schema")
        if (
            not isinstance(item["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"])
            or item["id"] in seen_ids
            or item["split"] not in ("train", "holdout")
            or not isinstance(item["task_id"], str)
            or not 1 <= len(item["task_id"]) <= 200
            or not isinstance(item["provenance"], str)
            or not 1 <= len(item["provenance"]) <= 2000
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("invalid dataset identity/provenance")
        source = _relative(path.parent, item["file"])
        if source in files:
            raise ValueError("duplicate dataset file")
        with source.open("rb") as stream:
            body = stream.read(MAX_TRACE_BYTES + 1)
        if len(body) > MAX_TRACE_BYTES or checksum(body) != item["sha256"]:
            raise ValueError("frozen trace checksum/size mismatch")
        trace = json.loads(body, object_pairs_hook=_unique_object)
        shape = validate_trace(trace)
        ceiling = max(trace["baseline"]["combined_score"], *(n["raw"]["combined_score"] for n in trace["nodes"]))
        if ceiling <= 0:
            raise ValueError("trace requires a positive recorded score ceiling")
        meaning = semantic_identity(trace)
        content = None
        if "task_evidence_sha256" in item or "task_evidence" in trace or "task_evidence_sha256" in trace:
            evidence = validate_task_evidence(trace.get("task_evidence"))
            digest = checksum(canonical(evidence))
            if item.get("task_evidence_sha256") != digest or trace.get("task_evidence_sha256") != digest:
                raise ValueError("dataset/trace task evidence binding differs")
            content = task_content_identity(evidence)
        # Verified public input is stronger than a scored curve: the same input
        # cannot cross splits even after changing scores/IDs/checker labels.
        if content is not None and content in contents and contents[content] != item["split"]:
            raise ValueError("same public task content crosses train/holdout split")
        if (
            item["task_id"] in tasks
            and tasks[item["task_id"]] != item["split"]
            or any(split != item["split"] and (content is None or prior_content is None or prior_content == content)
                   for split, prior_content in meanings.get(meaning, []))
        ):
            raise ValueError("same task or scored-tree meaning crosses train/holdout split")
        tasks[item["task_id"]] = item["split"]
        meanings.setdefault(meaning, []).append((item["split"], content))
        if content is not None:
            contents[content] = item["split"]
        seen_ids.add(item["id"])
        files.add(source)
        entries.append(
            {
                **item,
                "path": str(source),
                "bytes": len(body),
                "shape": shape,
                "semantic_sha256": meaning,
                "task_content_sha256": content,
                "trace": trace,
            }
        )
    if {e["split"] for e in entries} != {"train", "holdout"}:
        raise ValueError("dataset requires both train and holdout whole traces")
    return {
        "kind": value["kind"],
        "source": value["source"],
        "manifest": str(path),
        "manifest_sha256": checksum(raw),
        "entries": entries,
    }


def candidate_grid(limit):
    if type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError("max_candidates must be a strict integer in 1..16")
    return [
        {"role": f"c{i:02d}", "parameters": p, "policy_hash": policy_identity(p)}
        for i, p in enumerate({"max_width": w, "max_refine": d} for w in (1, 2, 8, 64) for d in (0, 1, 4, 64))
    ][:limit]


def _bounds(budgets, max_candidates, min_gain, max_workers, timeout_seconds, max_seconds=300):
    candidate_grid(max_candidates)
    if (
        not isinstance(budgets, (list, tuple))
        or not 1 <= len(budgets) <= 4
        or any(type(b) is not int or not 1 <= b <= MAX_NODES for b in budgets)
        or len(set(budgets)) != len(budgets)
    ):
        raise ValueError("budgets need 1..4 distinct bounded positive strict integers")
    if type(min_gain) not in (int, float) or not math.isfinite(min_gain) or min_gain < 0:
        raise ValueError("min_gain must be finite and nonnegative")
    if type(max_workers) is not int or not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be a strict integer in 1..64")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600:
        raise ValueError("max_seconds must be finite and within (0,3600]")


def _implementation():
    root = Path(__file__).parent
    return [
        {"file": name, "sha256": checksum((root / name).read_bytes())}
        for name in (
            "dream_rsi_optimizer.py",
            "dream_rsi_contract.py",
            "dream_rsi_registry.py",
            "dream_rsi_replay.py",
            "_dream_rsi_worker.py",
        )
    ]


def _baseline():
    return {"role": "baseline", "parameters": None, "policy_hash": policy_identity(None)}


def _holdout_policies(plan, selected):
    unique = {}
    for policy in [_baseline(), selected, plan["incumbent"]]:
        unique.setdefault(policy["policy_hash"], policy)
    return list(unique.values())


def _verify_incumbent(root, plan):
    incumbent = plan["incumbent"]
    if (
        not isinstance(incumbent, dict)
        or set(incumbent) != {"role", "parameters", "policy_hash", "version", "token", "document_file"}
        or incumbent["role"] != "incumbent"
        or incumbent["policy_hash"] != policy_identity(incumbent["parameters"])
        or not isinstance(incumbent["token"], str)
        or (incumbent["token"] != "absent" and not re.fullmatch(r"[0-9a-f]{64}", incumbent["token"]))
    ):
        raise ValueError("invalid frozen incumbent")
    if incumbent["version"] is None:
        if incumbent["parameters"] is not None or incumbent["document_file"] is not None:
            raise ValueError("missing active version must use full reference")
    else:
        if incumbent["document_file"] != "incumbent-version.json":
            raise ValueError("invalid incumbent document path")
        raw = (root / "incumbent-version.json").read_bytes()
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            checksum(raw) != incumbent["version"]
            or document.get("source_commit") != SOURCE_COMMIT
            or document.get("parameters") != incumbent["parameters"]
            or document.get("policy_hash") != incumbent["policy_hash"]
        ):
            raise ValueError("frozen incumbent version/config differs")


def _leaf(root, plan, row):
    if not isinstance(row, dict) or set(row) != {"split", "role", "id", "budget", "directory", "policy_hash"}:
        raise ValueError("invalid leaf slot schema")
    entry = next((e for e in plan["entries"] if e["id"] == row["id"]), None)
    policy = next((p for p in [_baseline(), *plan["candidates"], plan["incumbent"]] if p["role"] == row["role"]), None)
    if (
        entry is None
        or policy is None
        or row["directory"] != f"evaluations/{row['split']}/{row['role']}/{row['id']}/b{row['budget']}"
    ):
        raise ValueError("unknown or relocated leaf slot")
    trace_path = _relative(root, entry["frozen_file"])
    raw = trace_path.read_bytes()
    if checksum(raw) != entry["sha256"]:
        raise ValueError("frozen trace changed")
    directory = _relative(root, row["directory"])
    summary, episode = _load(directory / "summary.json"), _load(directory / "decisions.json")
    if (
        summary.get("input_sha256") != entry["sha256"]
        or row["split"] != entry["split"]
        or row["policy_hash"] != policy["policy_hash"]
    ):
        raise ValueError("leaf source/slot differs")
    return verify_leaf(json.loads(raw), summary, episode, policy["parameters"], row["budget"], plan["max_workers"])


def _select(plan, rows, metrics):
    choices = [_baseline(), *plan["candidates"]]
    expected_count = sum(e["split"] == "train" for e in plan["entries"]) * len(plan["budgets"])
    scores = {}
    for policy in choices:
        values = [metrics[i] for i, row in enumerate(rows) if row["split"] == "train" and row["role"] == policy["role"]]
        if len(values) != expected_count:
            raise ValueError("incomplete training samples")
        scores[policy["role"]] = sum(values) / len(values)
    # Prefer the full fixed reference on ties, then narrower/shallower parameters.
    selected = min(
        choices,
        key=lambda p: (
            -scores[p["role"]],
            p["role"] != "baseline",
            (p["parameters"] or {}).get("max_width", 0),
            (p["parameters"] or {}).get("max_refine", 0),
        ),
    )
    return selected, scores


def _derive(root, plan, rows):
    if (
        plan["source_commit"] != SOURCE_COMMIT
        or plan["objective"] != OBJECTIVE
        or plan["candidates"] != candidate_grid(plan["max_candidates"])
    ):
        raise ValueError("plan algorithm/source differs")
    _bounds(
        plan["budgets"],
        plan["max_candidates"],
        plan["min_gain"],
        plan["max_workers"],
        plan["timeout_seconds"],
        plan["max_seconds"],
    )
    if plan["implementation"] != _implementation():
        raise ValueError("frozen evaluator/adapter implementation changed")
    if type(plan["dry_run"]) is not bool or not isinstance(rows, list) or len(rows) > 2400:
        raise ValueError("invalid frozen search mode/evaluation inventory")
    _verify_incumbent(root, plan)
    metrics = [_leaf(root, plan, row) for row in rows]
    if any(not math.isfinite(m) or not 0 <= m <= 1 for m in metrics):
        raise ValueError("invalid trusted objective values")
    selected, train_scores = _select(plan, rows, metrics)
    expected = {
        (e["split"], p["role"], e["id"], b)
        for e in plan["entries"]
        for b in plan["budgets"]
        for p in ([_baseline(), *plan["candidates"]] if e["split"] == "train" else _holdout_policies(plan, selected))
    }
    actual = [(r["split"], r["role"], r["id"], r["budget"]) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("missing/extra/duplicate evaluation slots")
    holdout = {}
    policies = _holdout_policies(plan, selected)
    for policy in [_baseline(), selected, plan["incumbent"]]:
        evaluated_role = next(p["role"] for p in policies if p["policy_hash"] == policy["policy_hash"])
        values = [
            metrics[i] for i, row in enumerate(rows) if row["split"] == "holdout" and row["role"] == evaluated_role
        ]
        holdout[policy["role"]] = sum(values) / len(values)
    gain = holdout[selected["role"]] - holdout["baseline"]
    incumbent_gain = holdout[selected["role"]] - holdout["incumbent"]
    eligible = (
        plan["kind"] == "observed"
        and not plan["dry_run"]
        and gain > plan["min_gain"]
        and gain > 0
        and incumbent_gain > plan["min_gain"]
        and incumbent_gain > 0
    )
    return {
        "schema_version": 1,
        "status": "completed",
        "kind": plan["kind"],
        "objective": OBJECTIVE,
        "source_commit": SOURCE_COMMIT,
        "selected": selected,
        "train_scores": train_scores,
        "holdout_scores": holdout,
        "validation_gain": gain,
        "incumbent_gain": incumbent_gain,
        "incumbent": plan["incumbent"],
        "min_gain": plan["min_gain"],
        "promotion_eligible": eligible,
        "evaluations": len(rows),
        "limits": [
            "Bounded parameter search, not generated Python or the paper's reward/RSI loop.",
            "Normalized recorded-score replay; no claim about unseen branches or task-quality improvement.",
            "Observed is a caller declaration, not cryptographic proof of score provenance.",
            "Synthetic datasets are function checks only and never production-active.",
            "Holdout accepts the train-selected policy only if it strictly beats both frozen reference and incumbent.",
        ],
    }


def _manifest(root, name="manifest.json"):
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / name:
            raw = path.read_bytes()
            files.append({"file": path.relative_to(root).as_posix(), "sha256": checksum(raw), "bytes": len(raw)})
    value = {"schema_version": 1, "files": files}
    if name == "evidence.json":
        _write(root / name, value)
    else:
        temporary = root / (".manifest-" + uuid4().hex + ".json")
        try:
            _write(temporary, value)
            os.replace(temporary, root / name)
        finally:
            temporary.unlink(missing_ok=True)


def verify_promotion(run):
    root = Path(run).resolve()
    evidence = _load(root / "evidence.json")
    names = set()
    for row in evidence["files"]:
        path = _relative(root, row["file"])
        raw = path.read_bytes()
        if row["file"] in names or checksum(raw) != row["sha256"] or len(raw) != row["bytes"]:
            raise ValueError("promotion artifact checksum mismatch")
        names.add(row["file"])
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual - {"evidence.json", "manifest.json", "promotion.json"} != names:
        raise ValueError("promotion artifact inventory differs")
    plan = _load(root / "plan.json")
    current = load_dataset(plan["dataset_manifest"])
    if (
        current["manifest_sha256"] != plan["dataset_manifest_sha256"]
        or current["kind"] != plan["kind"]
        or current["source"] != plan["dataset_source"]
    ):
        raise ValueError("source dataset manifest changed")
    expected_entries = [
        {k: v for k, v in e.items() if k != "trace"} | {"frozen_file": "frozen/" + e["id"] + ".json"}
        for e in current["entries"]
    ]
    if plan["entries"] != expected_entries:
        raise ValueError("source dataset trace/provenance changed")
    derived = _derive(root, plan, _load(root / "evaluations.json"))
    if derived != _load(root / "summary.json") or not derived["promotion_eligible"]:
        raise ValueError("run does not qualify for observed strict-gain promotion")
    return {
        "parameters": derived["selected"]["parameters"],
        "policy_hash": derived["selected"]["policy_hash"],
        "registry_root": plan["registry_root"],
        "expected_token": plan["incumbent"]["token"],
        "expected_incumbent_version": plan["incumbent"]["version"],
        "expected_incumbent_hash": plan["incumbent"]["policy_hash"],
        "receipt": {
            "evidence_sha256": checksum((root / "evidence.json").read_bytes()),
            "plan_sha256": checksum((root / "plan.json").read_bytes()),
            "summary_sha256": checksum((root / "summary.json").read_bytes()),
        },
    }


def optimize(
    dataset_manifest,
    output,
    *,
    registry_path=None,
    budgets=(1, 2, 4),
    max_candidates=16,
    dry_run=False,
    min_gain=0.0,
    max_workers=1,
    timeout_seconds=30,
    max_seconds=300,
    _evaluator=None,
):
    started = time.monotonic()
    if dataset_manifest is None:
        return {"status": "needs_dataset", "promoted": False, "reason": "no scored train/holdout dataset"}
    _bounds(budgets, max_candidates, min_gain, max_workers, timeout_seconds, max_seconds)
    deadline = started + max_seconds

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("parameter search global deadline exceeded")
        return value

    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean")
    data = load_dataset(dataset_manifest)
    if data["kind"] == "synthetic" and not dry_run:
        raise ValueError("synthetic data requires explicit dry_run=True")
    from .dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(registry_path)
    snapshot = registry.snapshot()
    incumbent = {
        "role": "incumbent",
        "parameters": snapshot["parameters"],
        "policy_hash": policy_identity(snapshot["parameters"]),
        "version": snapshot["version"],
        "token": snapshot["token"],
        "document_file": "incumbent-version.json" if snapshot["version"] else None,
    }
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    try:
        (root / "frozen").mkdir()
        entries = []
        for entry in data["entries"]:
            body = Path(entry["path"]).read_bytes()
            if checksum(body) != entry["sha256"]:
                raise ValueError("dataset changed while freezing")
            relative = "frozen/" + entry["id"] + ".json"
            (root / relative).write_bytes(body)
            entries.append({k: v for k, v in entry.items() if k != "trace"} | {"frozen_file": relative})
        source_body = Path(data["manifest"]).read_bytes()
        if checksum(source_body) != data["manifest_sha256"]:
            raise ValueError("dataset manifest changed while freezing")
        (root / "source-dataset.json").write_bytes(source_body)
        if snapshot["raw_document"] is not None:
            (root / "incumbent-version.json").write_bytes(snapshot["raw_document"])
        plan = {
            "schema_version": 1,
            "source_commit": SOURCE_COMMIT,
            "objective": OBJECTIVE,
            "dataset_manifest": data["manifest"],
            "dataset_manifest_sha256": data["manifest_sha256"],
            "dataset_source": data["source"],
            "kind": data["kind"],
            "entries": entries,
            "registry_root": str(registry.root),
            "incumbent": incumbent,
            "candidates": candidate_grid(max_candidates),
            "max_candidates": max_candidates,
            "budgets": list(budgets),
            "max_workers": max_workers,
            "timeout_seconds": timeout_seconds,
            "max_seconds": max_seconds,
            "implementation": _implementation(),
            "min_gain": min_gain,
            "dry_run": dry_run,
            "tie_break": "score_desc,fixed_reference,width_asc,depth_asc",
        }
        _write(root / "plan.json", plan)
        if _evaluator is None:
            from .dream_rsi_replay import replay_trace

            _evaluator = replay_trace

        def evaluate(split, policies):
            for policy in policies:
                for entry in entries:
                    if entry["split"] != split:
                        continue
                    for budget in budgets:
                        relative = f"evaluations/{split}/{policy['role']}/{entry['id']}/b{budget}"
                        directory = root / relative
                        directory.parent.mkdir(parents=True, exist_ok=True)
                        timeout = min(remaining(), timeout_seconds)
                        with (root / "progress.jsonl").open("ab") as progress:
                            progress.write(
                                canonical(
                                    {
                                        "event": "dispatch",
                                        "split": split,
                                        "role": policy["role"],
                                        "id": entry["id"],
                                        "budget": budget,
                                    }
                                )
                                + b"\n"
                            )
                        _evaluator(
                            root / entry["frozen_file"],
                            directory,
                            parameters=policy["parameters"],
                            budget=budget,
                            max_workers=max_workers,
                            timeout_seconds=timeout,
                            use_active=False,
                        )
                        remaining()
                        row = {
                            "split": split,
                            "role": policy["role"],
                            "id": entry["id"],
                            "budget": budget,
                            "directory": relative,
                            "policy_hash": policy["policy_hash"],
                        }
                        _leaf(root, plan, row)
                        rows.append(row)

        evaluate("train", [_baseline(), *plan["candidates"]])
        metrics = [_leaf(root, plan, row) for row in rows]
        selected, _ = _select(plan, rows, metrics)
        evaluate("holdout", _holdout_policies(plan, selected))
        remaining()
        _write(root / "evaluations.json", rows)
        summary = _derive(root, plan, rows)
        _write(root / "summary.json", summary)
        _write(root / "status.json", {"status": "completed"})
        _manifest(root, "evidence.json")
        remaining()
        promotion = {"promoted": False, "reason": "not_eligible"}
        if incumbent["version"] is not None and summary["selected"]["policy_hash"] == incumbent["policy_hash"]:
            promotion.update(reason="already_active", version=incumbent["version"])
        if summary["promotion_eligible"]:
            target = registry.prepare(root)
            _write(
                root / "promotion.json",
                {"status": "prepared", "version": target, "commit_authority": "registry atomic pointers.json"},
            )
            _manifest(root)
            remaining()
            # Pointer commit is last: no output writes can fail after activation.
            promotion = registry.promote(root, deadline=deadline, expected_version=target)
        else:
            _write(root / "promotion.json", promotion)
            _manifest(root)
            remaining()
        return {**summary, **promotion}
    except BaseException as error:
        if not (root / "error.json").exists():
            _write(root / "error.json", {"status": "failed", "error_type": type(error).__name__})
        if not (root / "status.json").exists():
            _write(root / "status.json", {"status": "failed", "error_type": type(error).__name__})
        _manifest(root)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--budgets", type=int, nargs="+", help="replay probe budgets, not API dollar/request budgets")
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--min-gain", type=float, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        from .settings import load_settings

        config = getattr(load_settings(cwd=None), "dream_optimization", None)
        dataset = args.dataset_manifest or (
            getattr(config, "dataset_manifest", None) if getattr(config, "enabled", False) else None
        )
        result = optimize(
            dataset,
            args.output,
            registry_path=args.registry or getattr(config, "registry_path", None),
            budgets=args.budgets if args.budgets is not None else getattr(config, "budgets", [1, 2, 4]),
            max_candidates=args.max_candidates
            if args.max_candidates is not None
            else getattr(config, "max_candidates", 16),
            max_seconds=args.max_seconds if args.max_seconds is not None else getattr(config, "max_seconds", 300),
            dry_run=args.dry_run,
            min_gain=args.min_gain,
            max_workers=args.max_workers,
            timeout_seconds=args.timeout_seconds,
        )
        result["optimization_enabled"] = getattr(config, "enabled", False)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=__import__("sys").stderr)
        return 1
    print(json.dumps(result))
    return 0
