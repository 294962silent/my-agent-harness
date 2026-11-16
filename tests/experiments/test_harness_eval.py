"""The evaluator derives metrics from raw observations and never fabricates usage."""

import json

import pytest

from experiments.harness_eval.runner import aggregate, run_suite


def test_aggregation_counts_unique_cases_and_observed_values():
    rows = [
        dict(
            module="tools",
            case="independent",
            arm="on",
            repeat=i,
            duration_seconds=v,
            passed=True,
            metrics={"overlap": i + 1},
            kind="ablation",
        )
        for i, v in enumerate((0.01, 0.03, 0.02))
    ]
    summary = aggregate(rows)
    group = summary["groups"][0]
    assert summary["unique_cases"] == 1
    assert group["samples"] == 3 and group["duration_median_seconds"] == 0.02
    assert group["duration_p95_seconds"] == 0.03
    assert group["metrics"]["overlap"]["mean"] == 2
    assert summary["tokens"] is None and summary["quality"] is None


@pytest.mark.anyio
async def test_output_is_unique_and_evidence_uses_production_paths(tmp_path):
    result = await run_suite(suites=["tools"], repeats=2, output=tmp_path, seed=7)
    raw = [json.loads(line) for line in (result / "raw.jsonl").read_text().splitlines()]
    assert raw and all(row["evidence"]["production_paths"] for row in raw)
    assert all(row["evidence"]["resolved_sources"] for row in raw)
    assert all(row["passed"] for row in raw)
    assert {row["arm"] for row in raw} == {"on", "off"}
    assert (result / "manifest.json").exists() and (result / "report.md").exists()
    other = await run_suite(suites=["tools"], repeats=1, output=tmp_path, seed=7)
    assert other != result
    assert len(raw) == len((result / "raw.jsonl").read_text().splitlines())


@pytest.mark.anyio
async def test_invalid_parameters_fail_before_output(tmp_path):
    with pytest.raises(ValueError):
        await run_suite(suites=["all"], repeats=0, output=tmp_path)
    with pytest.raises(ValueError):
        await run_suite(suites=["unknown"], repeats=1, output=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_all_modules_contracts_and_manifest(tmp_path):
    from experiments.harness_eval.runner import MODULES

    result = await run_suite(suites=["all"], repeats=1, output=tmp_path)
    rows = [json.loads(line) for line in (result / "raw.jsonl").read_text().splitlines()]
    assert {row["module"] for row in rows} == set(MODULES)
    assert all(row["passed"] for row in rows), [
        (r["module"], r["error"], r["assertions"]) for r in rows if not r["passed"]
    ]
    assert {row["kind"] for row in rows} == {"ablation", "comparison", "contract"}
    manifest = json.loads((result / "manifest.json").read_text())
    assert len(manifest["source_sha256"]) == 64
    assert manifest["config"]["mode"] == "offline"
    assert manifest["provider_requests"] == 0
    assert manifest["tokens"] is None
    # Raw numerical evidence, not assertion counts, shows the intervention.
    permission = {r["arm"]: r for r in rows if r["module"] == "permission"}
    assert permission["on"]["metrics"]["unauthorized_side_effects"] == 0
    assert permission["off"]["metrics"]["unauthorized_side_effects"] == 1


@pytest.mark.anyio
async def test_failures_are_retained_without_exception_body(tmp_path, monkeypatch):
    from experiments.harness_eval import scenarios

    async def broken(arm, path):
        raise ValueError("SYNTHETIC_SECRET_SHOULD_NOT_APPEAR")

    monkeypatch.setattr(
        scenarios,
        "cases",
        lambda: [
            scenarios.Case(
                "tools",
                "fails",
                ("contract",),
                "contract",
                broken,
                ["src/my_agent_core/registry.py"],
                "fixture failure",
            )
        ],
    )
    result = await run_suite(suites=["tools"], repeats=1, output=tmp_path)
    raw_text = (result / "raw.jsonl").read_text()
    raw = json.loads(raw_text)
    assert raw["error"] == "ValueError" and not raw["passed"]
    assert "SYNTHETIC_SECRET_SHOULD_NOT_APPEAR" not in raw_text
    assert json.loads((result / "summary.json").read_text())["failures"] == 1


def test_cli_rejects_non_offline_mode():
    from experiments.harness_eval.runner import main

    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live"])
    assert exc.value.code == 2


@pytest.mark.anyio
async def test_context_fair_recovery_and_summary_binding(tmp_path):
    result = await run_suite(suites=['context'], repeats=1, output=tmp_path)
    rows = [json.loads(line) for line in (result/'raw.jsonl').read_text().splitlines()]
    artifact = {r['arm']: r for r in rows if r['case'] == 'artifact_roundtrip'}
    assert artifact['off']['metrics']['evidence_recoverable'] == 1
    assert artifact['on']['metrics']['evidence_recoverable'] == 1
    assert artifact['off']['metrics']['view_contains_full_source'] == 1
    assert artifact['on']['metrics']['artifact_readback_matches_source'] == 1
    assert artifact['on']['metrics']['artifact_disk_bytes'] > 0
    binding = next(r for r in rows if r['case'] == 'summary_source_binding')
    assert binding['kind'] == 'contract' and binding['passed']
    assert binding['metrics']['changed_source_summary_leaks'] == 0
