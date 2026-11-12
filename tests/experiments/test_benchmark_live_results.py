"""Outcome parsing and portable archived evidence; no model or grader calls."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
import shutil

import pytest

from experiments.benchmark_live.swe_results import (
    archive_outcomes,
    derive_outcome_record,
    parse_official_outcome,
    summarize_outcomes,
)

INSTANCE = "pytest-dev__pytest-5103"
PAIRS = {
    "submitted": "submitted_ids",
    "completed": "completed_ids",
    "resolved": "resolved_ids",
    "unresolved": "unresolved_ids",
    "empty_patch": "empty_patch_ids",
    "error": "error_ids",
    "infra_failure": "infra_failure_ids",
    "ambiguous_failure": "ambiguous_failure_ids",
}


def report(kind):
    result = {"schema_version": 2, "total_instances": 1, "incomplete_ids": [], "failure_reasons": {}}
    for stem, field in PAIRS.items():
        result[stem + "_instances"] = 0
        result[field] = []

    def include(stem):
        result[stem + "_instances"] = 1
        result[PAIRS[stem]] = [INSTANCE]

    include("submitted")
    if kind == "empty":
        include("empty_patch")
    elif kind == "resolved":
        include("completed")
        include("resolved")
    elif kind == "unresolved":
        include("completed")
        include("unresolved")
    elif kind == "error":
        include("error")
        include("ambiguous_failure")
    return result


class OfficialOutcomeTests(unittest.TestCase):
    def test_empty_both_arms_are_known_failures_without_test_execution(self):
        outcome = parse_official_outcome(report("empty"), INSTANCE)
        self.assertEqual(
            outcome,
            {"category": "empty_patch", "resolved": False, "test_executed": False, "reason": "official_empty_patch"},
        )
        rows = [{"instance_id": INSTANCE, "arm": arm, "outcome": outcome} for arm in ("on", "off")]
        self.assertEqual(summarize_outcomes(rows)["known_official_outcomes"], 2)
        self.assertEqual(summarize_outcomes(rows)["test_executed_trials"], 0)
        self.assertEqual(summarize_outcomes(rows)["complete_known_pairs"], 1)

    def test_empty_plus_unresolved_is_one_known_pair_one_executed_test_trial(self):
        rows = [
            {"instance_id": INSTANCE, "arm": "on", "outcome": parse_official_outcome(report("empty"), INSTANCE)},
            {"instance_id": INSTANCE, "arm": "off", "outcome": parse_official_outcome(report("unresolved"), INSTANCE)},
        ]
        summary = summarize_outcomes(rows)
        self.assertEqual(
            (summary["known_official_outcomes"], summary["test_executed_trials"], summary["complete_known_pairs"]),
            (2, 1, 1),
        )
        self.assertEqual(summary["resolved_trials"], 0)

    def test_resolved_and_completed_unresolved(self):
        self.assertEqual(parse_official_outcome(report("resolved"), INSTANCE)["resolved"], True)
        outcome = parse_official_outcome(report("unresolved"), INSTANCE)
        self.assertEqual(
            (outcome["category"], outcome["resolved"], outcome["test_executed"]), ("unresolved", False, True)
        )

    def test_errors_and_missing_fields_never_become_false(self):
        malformed = [report("error"), {}]
        for key in ("submitted_ids", "error_instances", "completed_instances"):
            value = report("resolved")
            del value[key]
            malformed.append(value)
        for value in malformed:
            with self.subTest(value=value):
                outcome = parse_official_outcome(value, INSTANCE)
                self.assertIsNone(outcome["resolved"])
                self.assertIsNone(outcome["test_executed"])

    def test_wrong_submitted_or_outcome_id(self):
        for field in ("submitted_ids", "resolved_ids"):
            value = report("resolved")
            value[field] = ["sympy__sympy-20590"]
            outcome = parse_official_outcome(value, INSTANCE)
            self.assertIsNone(outcome["resolved"])
            self.assertIsNone(outcome["test_executed"])

    def test_conflicting_sets_counts_duplicate_ids_and_bool_counts(self):
        values = []
        value = report("empty")
        value["completed_ids"] = [INSTANCE]
        value["completed_instances"] = 1
        value["resolved_ids"] = [INSTANCE]
        value["resolved_instances"] = 1
        values.append(value)
        value = report("resolved")
        value["resolved_instances"] = 0
        values.append(value)
        value = report("resolved")
        value["submitted_ids"] = [INSTANCE, INSTANCE]
        values.append(value)
        value = report("resolved")
        value["completed_instances"] = True
        values.append(value)
        for value in values:
            outcome = parse_official_outcome(value, INSTANCE)
            self.assertIsNone(outcome["resolved"])
            self.assertIsNone(outcome["test_executed"])

    def test_dataset_total_can_exceed_single_submission(self):
        value = report("resolved")
        value["total_instances"] = 3
        value["incomplete_ids"] = ["sympy__sympy-1", "sympy__sympy-2"]
        self.assertIs(parse_official_outcome(value, INSTANCE)["resolved"], True)

    def test_official_completed_parse_error_overlap_is_legal_but_unknown(self):
        value = report("error")
        value["completed_ids"] = [INSTANCE]
        value["completed_instances"] = 1
        outcome = parse_official_outcome(value, INSTANCE)
        self.assertEqual(outcome["category"], "error")
        self.assertIsNone(outcome["resolved"])
        self.assertIsNone(outcome["test_executed"])

    def test_infra_overlay_on_unresolved_is_not_a_model_failure(self):
        value = report("unresolved")
        value["infra_failure_ids"] = [INSTANCE]
        value["infra_failure_instances"] = 1
        outcome = parse_official_outcome(value, INSTANCE)
        self.assertIsNone(outcome["resolved"])
        self.assertIsNone(outcome["test_executed"])

    def test_hash_verified_rederivation_ignores_old_wrong_cached_category(self):
        raw = json.dumps(report("empty")).encode()
        fingerprint = hashlib.sha256(raw).hexdigest()
        cached = {"score_category": "environment_or_execution_error", "official_resolved": None}
        original = copy.deepcopy(cached)
        record = derive_outcome_record(raw, INSTANCE, expected_sha256=fingerprint, previous_cached=cached)
        self.assertEqual(record["outcome"]["category"], "empty_patch")
        self.assertIs(record["outcome"]["resolved"], False)
        self.assertEqual(record["previous_cached_audit"], cached)
        self.assertEqual(cached, original)
        self.assertEqual(record["raw_report_sha256"], fingerprint)
        self.assertIn("parser_version", record)
        with self.assertRaises(ValueError):
            derive_outcome_record(raw + b" ", INSTANCE, expected_sha256=fingerprint)

    def test_unknown_half_does_not_complete_a_known_pair(self):
        summary = summarize_outcomes(
            [
                {"instance_id": INSTANCE, "arm": "on", "outcome": parse_official_outcome(report("resolved"), INSTANCE)},
                {"instance_id": INSTANCE, "arm": "off", "outcome": parse_official_outcome(report("error"), INSTANCE)},
            ]
        )
        self.assertEqual((summary["known_official_outcomes"], summary["complete_known_pairs"]), (1, 0))


if __name__ == "__main__":
    unittest.main()


ROOT = Path(__file__).resolve().parents[2]


def test_real_empty_and_previously_resolved_official_5_0_2_reports():
    raw = ROOT / "docs/experiments/results/swe-v2-pilot-20261112/raw"
    old = ROOT / "docs/experiments/results/live-swe-20261112"
    if not (raw / "manifest.json").exists() or not (old / "manifest.json").exists():
        pytest.skip("archived official examples unavailable in this checkout")
    for base, suffix, expected, category in (
        (raw, "trials/01-pytest-dev__pytest-5103-on/official-summary.json", INSTANCE, "empty_patch"),
        (raw, "trials/02-pytest-dev__pytest-5103-off/official-summary.json", INSTANCE, "empty_patch"),
        (old, "on/official-summary.json", "sympy__sympy-20590", "resolved"),
        (old, "off/official-summary.json", "sympy__sympy-20590", "resolved"),
    ):
        manifest = json.loads((base / "manifest.json").read_text())
        fingerprint = next(r["sha256"] for r in manifest["files"] if r["file"] == suffix)
        parsed = derive_outcome_record((base / suffix).read_bytes(), expected, expected_sha256=fingerprint)
        assert parsed["outcome"]["category"] == category


def test_relocated_real_archive_is_recomputed_and_tampering_is_rejected(tmp_path):
    original = ROOT / "docs/experiments/results/swe-v2-pilot-20261112/raw"
    if not (original / "manifest.json").exists():
        pytest.skip("v2 archive unavailable")
    fingerprint = hashlib.sha256((original / "manifest.json").read_bytes()).hexdigest()
    archive = tmp_path / "relocated"
    shutil.copytree(original, archive)
    result = archive_outcomes(archive, expected_manifest_sha256=fingerprint)
    assert result["verified_archived_files"] == 81
    assert result["generation_source_commit"] == "765216830b1972ba3b3c0d20171315343c63d288"
    assert result["resolved_trials"] == 3 and result["empty_patch_trials"] == 7
    assert result["known_official_outcomes"] == 10 and result["test_executed_trials"] == 3
    assert result["complete_known_pairs"] == 5
    assert result["by_arm"]["on"]["resolved_trials"] == 2 and result["by_arm"]["off"]["resolved_trials"] == 1
    patch = archive / "trials/01-pytest-dev__pytest-5103-on/official-summary.json"
    with patch.open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        archive_outcomes(archive, expected_manifest_sha256=fingerprint)
    assert hashlib.sha256((original / "manifest.json").read_bytes()).hexdigest() == fingerprint
