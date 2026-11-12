"""Pure-stdlib SWE-bench 5.0.2 outcome parsing and portable archive verification.

One submitted instance is required; total_instances may cover a larger dataset.
Official incomplete_ids are unsubmitted dataset members, not failed submissions.
completed_ids can overlap error_ids when an existing instance report is corrupt.
infra/ambiguous failures are additive overlays on unresolved/error IDs.

No file is written and no cached classification is trusted as outcome evidence.
"""

from __future__ import annotations

import copy
import argparse
import hashlib
import json
import re
from pathlib import Path

PARSER_VERSION = "swebench-5.0.2-outcome-v1"
ID_FIELDS = {
    stem: stem + "_ids"
    for stem in (
        "submitted",
        "completed",
        "resolved",
        "unresolved",
        "empty_patch",
        "error",
        "infra_failure",
        "ambiguous_failure",
    )
}
KNOWN_CATEGORIES = frozenset(("empty_patch", "unresolved", "resolved"))


def _unknown(reason, category="unknown"):
    return {"category": category, "resolved": None, "test_executed": None, "reason": reason}


def _id_set(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        return None
    result = set(value)
    return result if len(result) == len(value) else None


def parse_official_outcome(report, expected_instance_id):
    """Return category/resolved/test_executed/reason, failing unknown on ambiguity."""
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        return _unknown("invalid_expected_instance_id")
    if not isinstance(report, dict):
        return _unknown("report_not_object")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 2:
        return _unknown("unsupported_or_missing_schema_version")
    total = report.get("total_instances")
    if type(total) is not int or total < 1:
        return _unknown("invalid_total_instances")
    groups = {}
    for stem, field in ID_FIELDS.items():
        count_field = stem + "_instances"
        if field not in report or count_field not in report:
            return _unknown("missing_field:" + field + "/" + count_field)
        count = report[count_field]
        if type(count) is not int or count < 0:
            return _unknown("invalid_count:" + count_field)
        ids = _id_set(report[field])
        if ids is None:
            return _unknown("invalid_or_duplicate_ids:" + field)
        if len(ids) != count:
            return _unknown("count_ids_mismatch:" + stem)
        groups[stem] = ids
    submitted = groups["submitted"]
    if submitted != {expected_instance_id}:
        return _unknown("submitted_identity_mismatch")
    if report["submitted_instances"] != 1 or total < report["submitted_instances"]:
        return _unknown("submitted_count_mismatch")
    if any(not ids <= submitted for stem, ids in groups.items() if stem != "submitted"):
        return _unknown("outcome_identity_mismatch")
    incomplete = _id_set(report.get("incomplete_ids"))
    if incomplete is None:
        return _unknown("invalid_or_missing_incomplete_ids")
    if incomplete & submitted:
        return _unknown("submitted_also_incomplete")
    resolved, unresolved, completed, error, empty = (
        groups[k] for k in ("resolved", "unresolved", "completed", "error", "empty_patch")
    )
    if resolved & unresolved or (resolved | unresolved) & error:
        return _unknown("conflicting_resolved_unresolved_error_sets")
    if completed - error != resolved | unresolved:
        return _unknown("completed_outcome_partition_mismatch")
    if empty & (completed | error | resolved | unresolved):
        return _unknown("empty_patch_conflicts_with_execution")
    represented = completed | error | empty
    if represented != submitted:
        return _unknown("submitted_outcome_missing")
    if len(represented | incomplete) != total:
        return _unknown("dataset_identity_count_mismatch")
    infra, ambiguous = groups["infra_failure"], groups["ambiguous_failure"]
    if infra & ambiguous or not (infra | ambiguous) <= unresolved | error:
        return _unknown("invalid_failure_overlays")
    reasons = report.get("failure_reasons", {})
    if not isinstance(reasons, dict) or not set(reasons) <= infra | ambiguous:
        return _unknown("failure_reason_identity_mismatch")
    if expected_instance_id in empty:
        return {"category": "empty_patch", "resolved": False, "test_executed": False, "reason": "official_empty_patch"}
    if expected_instance_id in error:
        return _unknown("official_error", "error")
    if expected_instance_id in infra:
        return _unknown("official_infrastructure_failure", "error")
    if expected_instance_id in ambiguous:
        return _unknown("official_ambiguous_failure", "error")
    if expected_instance_id in resolved:
        return {"category": "resolved", "resolved": True, "test_executed": True, "reason": "official_resolved"}
    if expected_instance_id in unresolved:
        return {
            "category": "unresolved",
            "resolved": False,
            "test_executed": True,
            "reason": "official_completed_unresolved",
        }
    return _unknown("official_outcome_unavailable")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate raw report JSON field")
        result[key] = value
    return result


def derive_outcome_record(raw_report, expected_instance_id, *, expected_sha256, previous_cached=None):
    """Verify original bytes before deriving; previous labels are audit-only copies."""
    if not isinstance(raw_report, bytes):
        raise TypeError("raw_report must be original bytes")
    actual = hashlib.sha256(raw_report).hexdigest()
    if (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or actual != expected_sha256
    ):
        raise ValueError("original official report checksum mismatch")
    try:
        report = json.loads(raw_report, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        outcome = _unknown("unreadable_raw_report")
    else:
        outcome = parse_official_outcome(report, expected_instance_id)
    audit_keys = (
        "category",
        "score_category",
        "score_category_original_v765",
        "resolved",
        "official_resolved",
        "test_executed",
    )
    audit = (
        {key: copy.deepcopy(previous_cached[key]) for key in audit_keys if key in previous_cached}
        if isinstance(previous_cached, dict)
        else None
    )
    return {
        "parser_version": PARSER_VERSION,
        "expected_instance_id": expected_instance_id,
        "raw_report_sha256": actual,
        "outcome": outcome,
        "previous_cached_audit": audit,
        "raw_report_or_cache_modified": False,
    }


def summarize_outcomes(rows):
    """Known empty submissions count as outcomes, independently from executed tests."""
    known = executed = resolved = unknown_tests = 0
    pairs = {}
    seen = set()
    for row in rows:
        instance = row["instance_id"]
        arm = row["arm"]
        outcome = row["outcome"]
        if arm not in ("on", "off") or (instance, arm) in seen:
            raise ValueError("invalid or duplicate paired trial identity")
        seen.add((instance, arm))
        available = outcome.get("category") in KNOWN_CATEGORIES and type(outcome.get("resolved")) is bool
        if available:
            known += 1
            pairs.setdefault(instance, set()).add(arm)
        executed += int(outcome.get("test_executed") is True)
        unknown_tests += int(outcome.get("test_executed") is None)
        resolved += int(available and outcome["resolved"] is True)
    return {
        "parser_version": PARSER_VERSION,
        "known_official_outcomes": known,
        "test_executed_trials": executed,
        "test_execution_unknown_trials": unknown_tests,
        "resolved_trials": resolved,
        "complete_known_pairs": sum(arms == {"on", "off"} for arms in pairs.values()),
    }


def archive_outcomes(raw_dir, *, expected_manifest_sha256=None):
    """Verify every archived file, then derive from local reports only; never write.

    No original-machine path, current generation source, model, grader or cached
    outcome is needed. An optional externally approved manifest SHA also binds
    the manifest itself; hashes alone are not an authenticity signature.
    """
    root = Path(raw_dir).resolve()
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        raise ValueError("archive manifest checksum mismatch")
    manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_object)
    files = {}
    for item in manifest["files"]:
        name = item["file"]
        path = (root / name).resolve()
        if name in files or not path.is_relative_to(root) or Path(name).is_absolute():
            raise ValueError("duplicate or escaping archive file")
        if not path.is_file():
            raise ValueError("archived file missing: " + name)
        raw = path.read_bytes()
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("archived file checksum mismatch: " + name)
        files[name] = (path, item["sha256"])
    if "plan.json" not in files:
        raise ValueError("archive has no verified plan")
    if files["plan.json"][1] != manifest["plan_sha256"]:
        raise ValueError("archive plan checksum differs from manifest")
    plan = json.loads(files["plan.json"][0].read_bytes(), object_pairs_hook=_unique_object)
    cached = {}
    if "summary.json" in files:
        old = json.loads(files["summary.json"][0].read_bytes(), object_pairs_hook=_unique_object)
        cached = {row["trial_id"]: row for row in old.get("trials", [])}
    eligible = {row["instance_id"]: row["eligible"] for row in plan["tasks"]}
    records = []
    seen = set()
    official_reports = 0
    for trial in plan["trials"]:
        identity = trial["trial_id"]
        if identity in seen:
            raise ValueError("duplicate archive trial ID")
        seen.add(identity)
        name = "trials/" + identity + "/official-summary.json"
        if name in files:
            path, fingerprint = files[name]
            derived = derive_outcome_record(
                path.read_bytes(),
                trial["instance_id"],
                expected_sha256=fingerprint,
                previous_cached=cached.get(identity),
            )
            official_reports += 1
        else:
            reason = "official_report_missing" if eligible[trial["instance_id"]] else "environment_blocked"
            derived = {
                "parser_version": PARSER_VERSION,
                "expected_instance_id": trial["instance_id"],
                "raw_report_sha256": None,
                "outcome": _unknown(reason),
                "previous_cached_audit": None,
                "raw_report_or_cache_modified": False,
            }
        records.append({"trial_id": identity, "instance_id": trial["instance_id"], "arm": trial["arm"], **derived})
    summary = summarize_outcomes(records)
    by_arm = {arm: summarize_outcomes([row for row in records if row["arm"] == arm]) for arm in ("on", "off")}
    for arm in by_arm:
        by_arm[arm]["empty_patch_trials"] = sum(
            row["arm"] == arm and row["outcome"]["category"] == "empty_patch" for row in records
        )
    return {
        "analysis_parser_version": PARSER_VERSION,
        "analysis_parser_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "generation_source_commit": manifest["source_commit"],
        "archive_manifest_sha256": manifest_sha,
        "verified_archived_files": len(files),
        "selected_tasks": len(eligible),
        "eligible_tasks": sum(eligible.values()),
        "blocked_tasks": sum(not v for v in eligible.values()),
        "official_reports": official_reports,
        "empty_patch_trials": sum(row["outcome"]["category"] == "empty_patch" for row in records),
        **summary,
        "by_arm": by_arm,
        "records": records,
        "archive_modified": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help="Portable raw evidence directory; read and verify only")
    parser.add_argument("--manifest-sha256", help="Optional externally approved manifest hash")
    args = parser.parse_args(argv)
    print(json.dumps(archive_outcomes(args.archive, expected_manifest_sha256=args.manifest_sha256), indent=2))


if __name__ == "__main__":
    main()
