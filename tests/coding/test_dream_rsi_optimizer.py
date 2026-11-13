"""Controlled-parameter optimization contracts; all datasets here are fixtures."""

import hashlib
import json

import pytest


def tree(peak=3.0):
    def cell(value):
        return {"evaluated": True, "combined_score": value}

    return {
        "version": 1,
        "n_problems": 1,
        "baseline": cell(1.0),
        "nodes": [
            {
                "id": f"b{b}_a{a}",
                "branch": b,
                "attempt": a,
                "parent_id": None if a == 0 else f"b{b}_a{a - 1}",
                "path": "not-read",
                "raw": cell(score),
            }
            for b, a, score in [(0, 0, 2.0), (0, 1, peak), (1, 0, 1.1)]
        ],
    }


def dataset(root, kind="synthetic"):
    root.mkdir()
    entries = []
    for split, peak in (("train", 3.0), ("holdout", 3.5)):
        body = json.dumps(tree(peak)).encode()
        (root / (split + ".json")).write_bytes(body)
        entries.append(
            {
                "id": split,
                "task_id": split + "-fixture",
                "split": split,
                "file": split + ".json",
                "sha256": hashlib.sha256(body).hexdigest(),
                "provenance": "test fixture only; not a genuine observation",
            }
        )
    manifest = {"schema_version": 1, "kind": kind, "source": "unit-test fixture", "entries": entries}
    path = root / "dataset.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def deeper_dataset(root):
    path = dataset(root, "observed")
    value = json.loads(path.read_bytes())
    for entry, peak in zip(value["entries"], (4.0, 4.5)):
        trace = tree(3.0 if entry["split"] == "train" else 3.5)
        trace["nodes"].extend(
            [
                {
                    "id": "b0_a2",
                    "branch": 0,
                    "attempt": 2,
                    "parent_id": "b0_a1",
                    "path": "fixture",
                    "raw": {"evaluated": True, "combined_score": peak},
                },
                {
                    "id": "b2_a0",
                    "branch": 2,
                    "attempt": 0,
                    "parent_id": None,
                    "path": "fixture",
                    "raw": {"evaluated": True, "combined_score": 1.2},
                },
            ]
        )
        raw = json.dumps(trace).encode()
        (root / entry["file"]).write_bytes(raw)
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(value))
    return path


def install_registry_fixture(registry, parameters):
    """Temporary registry state injection; never evidence of observed improvement."""
    from my_coding_agent.dream_rsi_contract import SOURCE_COMMIT, canonical, checksum, policy_identity

    raw = canonical(
        {
            "schema_version": 1,
            "source_commit": SOURCE_COMMIT,
            "parameters": parameters,
            "policy_hash": policy_identity(parameters),
            "receipt": {"evidence_sha256": "0" * 64, "plan_sha256": "1" * 64, "summary_sha256": "2" * 64},
        }
    )
    version = checksum(raw)
    with registry._lock():
        (registry.root / "versions").mkdir(exist_ok=True)
        (registry.root / "versions" / (version + ".json")).write_bytes(raw)
        registry._replace({"schema_version": 1, "active": version, "previous": None})
    return version


def test_parameter_whitelist_strict_types_and_clamping():
    from my_coding_agent.dream_rsi_contract import policy_identity, effective_parameters

    requested = {"max_width": 64, "max_refine": 64}
    assert effective_parameters(requested, {"branches": 2, "max_attempt": 1}) == {"max_width": 2, "max_refine": 1}
    assert policy_identity(requested) == policy_identity(dict(reversed(list(requested.items()))))
    for bad in (
        {"max_width": True, "max_refine": 1},
        {"max_width": 1, "max_refine": -1},
        {"max_width": 65, "max_refine": 0},
        {"max_width": 1, "max_refine": 1, "code": "x"},
    ):
        with pytest.raises(ValueError):
            effective_parameters(bad, {"branches": 2, "max_attempt": 1})


def test_trusted_wave_verifier_groups_nodes_once_and_stops_at_budget():
    from my_coding_agent.dream_rsi_contract import expected_waves

    class CountedNodes(list):
        traversals = 0

        def __iter__(self):
            self.traversals += 1
            return super().__iter__()

    trace = tree()
    trace["nodes"] = CountedNodes(
        [
            {
                "id": f"b0_a{a}",
                "branch": 0,
                "attempt": a,
                "parent_id": None if a == 0 else f"b0_a{a - 1}",
                "path": "unused",
                "raw": {"evaluated": True, "combined_score": a + 1},
            }
            for a in range(20)
        ]
    )
    waves = expected_waves(trace, None, 1)
    assert len(waves) == 1 and waves[0][0]["id"] == "b0_a0"
    assert trace["nodes"].traversals <= 4  # Structural passes plus one grouping, independent of depth.


def test_dataset_rejects_cross_split_task_or_semantic_duplicate(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import load_dataset

    path = dataset(tmp_path / "data")
    load_dataset(path)
    value = json.loads(path.read_bytes())
    value["entries"][1]["task_id"] = value["entries"][0]["task_id"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="split"):
        load_dataset(path)
    value["entries"][1]["task_id"] = "different-task"
    duplicate = tree()
    duplicate.update(workspace="different", exported_at="later")
    for node in duplicate["nodes"]:
        node["path"] = "renamed"
    body = json.dumps(duplicate).encode()
    (path.parent / "holdout.json").write_bytes(body)
    value["entries"][1]["sha256"] = hashlib.sha256(body).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="split"):
        load_dataset(path)

    for node in duplicate["nodes"]:
        node["branch"] = 1 - node["branch"]
        node["id"] = f"b{node['branch']}_a{node['attempt']}"
        node["parent_id"] = None if node["attempt"] == 0 else f"b{node['branch']}_a{node['attempt'] - 1}"
    body = json.dumps(duplicate).encode()
    (path.parent / "holdout.json").write_bytes(body)
    value["entries"][1]["sha256"] = hashlib.sha256(body).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="split"):
        load_dataset(path)


def test_dataset_rejects_escape_and_synthetic_requires_explicit_dry_run(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import load_dataset, optimize

    path = dataset(tmp_path / "data")
    with pytest.raises(ValueError, match="dry_run"):
        optimize(path, tmp_path / "never", budgets=[2], max_candidates=2)
    assert not (tmp_path / "never").exists()
    manifest = json.loads(path.read_bytes())
    manifest["entries"][0]["file"] = "../outside.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_dataset(path)
    assert optimize(None, tmp_path / "none")["status"] == "needs_dataset"


def fake_evaluator(trace, output, *, parameters=None, budget, **kwargs):
    """Injected fixture evaluator, never passed off as a real SDK execution."""
    from my_coding_agent.dream_rsi_contract import (
        expected_waves,
        effective_parameters,
        validate_trace,
        policy_identity,
        SOURCE_COMMIT,
    )
    import math

    output.mkdir(parents=True)
    raw = trace.read_bytes()
    value = json.loads(raw)
    workers = kwargs.get("max_workers", 1)
    best, spent, sequential = 0.0, 0, 0
    events, curve, sizes = [], [], []
    for index, nodes in enumerate(expected_waves(value, parameters, budget), 1):
        before = {"budget": spent, "best": best}
        outcomes = []
        for node in nodes:
            spent += 1
            best = max(best, node["raw"]["combined_score"])
            outcomes.append(
                {
                    "branch": node["branch"],
                    "attempt": node["attempt"],
                    "score": node["raw"]["combined_score"],
                    "budget": spent,
                    "best_after": best,
                    "evaluated": True,
                }
            )
            curve.append({"budget": spent, "best": best})
        sizes.append(len(nodes))
        sequential += math.ceil(len(nodes) / workers)
        events.append(
            {
                "round": index,
                "before": before,
                "after": {"budget": spent, "best": best},
                "batch": [{"branch": n["branch"], "attempt": n["attempt"]} for n in nodes],
                "outcomes": outcomes,
                "parallelism": {
                    "worker_cap": workers,
                    "batch_size": len(nodes),
                    "effective_sequential_rounds": math.ceil(len(nodes) / workers),
                    "decision_rounds_total": index,
                    "effective_sequential_rounds_total": sequential,
                },
            }
        )
    result = {
        "best": best,
        "budget": spent,
        "decision_rounds": len(events),
        "effective_sequential_rounds": sequential,
        "batch_sizes": sizes,
    }
    summary = {
        "result": result,
        "budget_limit": budget,
        "max_workers": workers,
        "parameters": {"requested": parameters, "effective": effective_parameters(parameters, validate_trace(value))},
    }
    episode = {"mode": "replay", "max_parallelism": workers, "events": events, "final": {**result, "curve": curve}}
    summary.update(
        status="completed",
        input_sha256=hashlib.sha256(raw).hexdigest(),
        upstream={"commit": SOURCE_COMMIT, "version": "0.1.0"},
        policy_hash=policy_identity(parameters),
    )
    for name, data in (("summary.json", summary), ("decisions.json", episode)):
        (output / name).write_text(json.dumps(data), encoding="utf-8")
    (output / "manifest.json").write_text('{"fixture_only":true}', encoding="utf-8")
    return summary


def test_observed_fixture_selection_promotion_active_and_rollback(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize, verify_promotion
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    # 'observed' is intentionally a test-only declaration, not real evidence.
    path = dataset(tmp_path / "data", kind="observed")
    registry = PolicyRegistry(tmp_path / "registry")
    result = optimize(
        path, tmp_path / "run", registry_path=registry.root, budgets=[2], max_candidates=2, _evaluator=fake_evaluator
    )
    assert result["promotion_eligible"] and result["promoted"]
    active = registry.status()
    assert active["parameters"] == {"max_width": 1, "max_refine": 1}
    assert verify_promotion(tmp_path / "run")["parameters"] == active["parameters"]
    # Editing even a leaf best value, then rehashing only the top level, is rejected.
    leaf = next((tmp_path / "run" / "evaluations").rglob("summary.json"))
    value = json.loads(leaf.read_bytes())
    value["result"]["best"] = 999
    leaf.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        registry.promote(tmp_path / "run")
    assert registry.status()["version"] == active["version"]
    with pytest.raises(ValueError):
        registry.rollback("0" * 64)
    assert registry.status()["version"] == active["version"]


def test_synthetic_cannot_activate_and_failure_is_not_dropped(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    path = dataset(tmp_path / "data")
    result = optimize(
        path,
        tmp_path / "run",
        registry_path=tmp_path / "registry",
        budgets=[2],
        max_candidates=2,
        dry_run=True,
        _evaluator=fake_evaluator,
    )
    assert not result["promotion_eligible"] and not result["promoted"]
    assert PolicyRegistry(tmp_path / "registry").status()["reason"] == "no_active_policy"

    def fail(*args, **kwargs):
        raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError):
        optimize(path, tmp_path / "failed", budgets=[2], max_candidates=2, dry_run=True, _evaluator=fail)
    assert json.loads((tmp_path / "failed" / "status.json").read_bytes())["status"] == "failed"


def reseal(run):
    """Attack fixture: rehash changed files, so metric checks must catch the lie."""
    evidence = json.loads((run / "evidence.json").read_bytes())
    for row in evidence["files"]:
        raw = (run / row["file"]).read_bytes()
        row.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    (run / "evidence.json").write_text(json.dumps(evidence))


def test_rehashing_leaf_lies_or_top_eligible_cannot_promote(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize, verify_promotion

    path = dataset(tmp_path / "data", "observed")
    run = tmp_path / "run"
    optimize(path, run, budgets=[2], max_candidates=2, min_gain=1000, _evaluator=fake_evaluator)
    leaf = next((run / "evaluations").rglob("summary.json"))
    original = leaf.read_bytes()
    value = json.loads(original)
    value["result"]["best"] = 999
    leaf.write_text(json.dumps(value))
    reseal(run)
    with pytest.raises(ValueError, match="SimResult"):
        verify_promotion(run)
    leaf.write_bytes(original)
    summary = json.loads((run / "summary.json").read_bytes())
    summary.update(promotion_eligible=True, validation_gain=1001)
    (run / "summary.json").write_text(json.dumps(summary))
    reseal(run)
    with pytest.raises(ValueError):
        verify_promotion(run)


def test_second_fixture_promotion_atomic_rollback_and_corrupt_active_recovery(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    path = dataset(tmp_path / "data", "observed")
    registry = PolicyRegistry(tmp_path / "registry")
    optimize(
        path, tmp_path / "run1", registry_path=registry.root, budgets=[2], max_candidates=2, _evaluator=fake_evaluator
    )
    first = registry.status()["version"]
    second_dataset = deeper_dataset(tmp_path / "data2")
    optimize(
        second_dataset,
        tmp_path / "run2",
        registry_path=registry.root,
        budgets=[3],
        max_candidates=3,
        _evaluator=fake_evaluator,
    )
    second = registry.status()["version"]
    assert first != second and registry.status()["previous"] == first
    assert registry.rollback()["version"] == first
    assert registry.rollback()["version"] == second
    (registry.root / "versions" / (second + ".json")).write_text("{}")
    assert registry.status()["reason"] == "invalid_registry"
    assert registry.rollback()["version"] == first
    assert registry.status()["previous"] is None


def test_final_manifest_lists_nested_leaf_manifests_and_registry_lock(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    path = dataset(tmp_path / "data")
    optimize(path, tmp_path / "run", budgets=[2], max_candidates=2, dry_run=True, _evaluator=fake_evaluator)
    run = tmp_path / "run"
    listed = {r["file"] for r in json.loads((run / "manifest.json").read_bytes())["files"]}
    assert listed == {
        p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file() and p != run / "manifest.json"
    }
    registry = PolicyRegistry(tmp_path / "registry")
    with registry._lock():
        with pytest.raises(RuntimeError, match="busy"):
            with PolicyRegistry(registry.root)._lock():
                pass


def test_leaf_cost_boolean_cannot_equal_an_integer_budget(tmp_path):
    from my_coding_agent.dream_rsi_contract import verify_leaf

    source = tmp_path / "trace.json"
    source.write_text(json.dumps(tree()))
    parameters = {"max_width": 1, "max_refine": 0}
    leaf = tmp_path / "leaf"
    summary = fake_evaluator(source, leaf, budget=2, parameters=parameters)
    summary["result"]["budget"] = True
    with pytest.raises(ValueError):
        verify_leaf(tree(), summary, json.loads((leaf / "decisions.json").read_bytes()), parameters, 2, 1)


def test_global_deadline_stops_dispatch_and_preserves_old_active(tmp_path, monkeypatch):
    from my_coding_agent import dream_rsi_optimizer as optimizer
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    path = dataset(tmp_path / "data", "observed")
    registry = PolicyRegistry(tmp_path / "registry")
    optimizer.optimize(
        path, tmp_path / "seed", registry_path=registry.root, budgets=[2], max_candidates=2, _evaluator=fake_evaluator
    )
    previous = registry.status()["version"]
    clock, calls = [0.0], []
    monkeypatch.setattr(optimizer.time, "monotonic", lambda: clock[0])

    def advancing(*args, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        result = fake_evaluator(*args, **kwargs)
        clock[0] += 2
        return result

    with pytest.raises(TimeoutError):
        optimizer.optimize(
            path,
            tmp_path / "expired",
            registry_path=registry.root,
            budgets=[2],
            max_candidates=2,
            max_seconds=1,
            timeout_seconds=30,
            _evaluator=advancing,
        )
    assert calls == [1]
    assert registry.status()["version"] == previous
    assert json.loads((tmp_path / "expired/status.json").read_bytes())["status"] == "failed"


def test_no_gain_does_not_promote_and_holdout_never_searches_other_candidates(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize

    path = dataset(tmp_path / "data", "observed")
    value = json.loads(path.read_bytes())
    for entry, score in zip(value["entries"], (1.0, 1.5)):
        trace = tree(score)
        raw = json.dumps(trace).encode()
        (path.parent / entry["file"]).write_bytes(raw)
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(value))
    result = optimize(
        path,
        tmp_path / "run",
        registry_path=tmp_path / "registry",
        budgets=[2],
        max_candidates=2,
        _evaluator=fake_evaluator,
    )
    assert result["selected"]["role"] == "baseline" and result["validation_gain"] == 0
    assert not result["promotion_eligible"] and not result["promoted"]
    rows = json.loads((tmp_path / "run/evaluations.json").read_bytes())
    assert [r["role"] for r in rows if r["split"] == "holdout"] == ["baseline"]


def test_failed_pointer_commit_keeps_active_and_seals_failure_artifacts(tmp_path, monkeypatch):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(tmp_path / "registry")
    path = dataset(tmp_path / "data", "observed")
    optimize(
        path, tmp_path / "seed", registry_path=registry.root, budgets=[2], max_candidates=2, _evaluator=fake_evaluator
    )
    active = registry.status()["version"]

    def fail(*args, **kwargs):
        raise OSError("fixture atomic-write failure")

    monkeypatch.setattr(PolicyRegistry, "_replace", fail)
    second = deeper_dataset(tmp_path / "data2")
    run = tmp_path / "failed"
    with pytest.raises(OSError):
        optimize(second, run, registry_path=registry.root, budgets=[3], max_candidates=3, _evaluator=fake_evaluator)
    assert registry.status()["version"] == active
    assert json.loads((run / "promotion.json").read_bytes())["status"] == "prepared"
    assert json.loads((run / "error.json").read_bytes())["error_type"] == "OSError"
    listed = {r["file"] for r in json.loads((run / "manifest.json").read_bytes())["files"]}
    assert listed == {
        p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file() and p != run / "manifest.json"
    }


def test_cli_uses_only_enabled_global_defaults_and_explicit_dataset_override(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from my_coding_agent import dream_rsi_optimizer as optimizer, dream_rsi_replay as replay, settings

    path = dataset(tmp_path / "data")
    config = SimpleNamespace(
        enabled=True,
        dataset_manifest=str(path),
        registry_path=str(tmp_path / "registry"),
        budgets=[4],
        max_candidates=16,
        max_seconds=10,
    )
    calls = []

    def load(*, cwd):
        calls.append(cwd)
        return SimpleNamespace(dream_optimization=config)

    monkeypatch.setattr(settings, "load_settings", load)
    monkeypatch.setattr(replay, "replay_trace", fake_evaluator)
    assert (
        optimizer.main(["--output", str(tmp_path / "one"), "--budgets", "2", "--max-candidates", "2", "--dry-run"]) == 0
    )
    plan = json.loads((tmp_path / "one/plan.json").read_bytes())
    assert plan["budgets"] == [2] and plan["max_candidates"] == 2 and plan["max_seconds"] == 10
    config.enabled = False
    assert optimizer.main(["--output", str(tmp_path / "none")]) == 0
    assert not (tmp_path / "none").exists()
    assert (
        optimizer.main(
            [
                "--dataset-manifest",
                str(path),
                "--output",
                str(tmp_path / "two"),
                "--budgets",
                "2",
                "--max-candidates",
                "2",
                "--dry-run",
            ]
        )
        == 0
    )
    assert calls == [None, None, None]


def test_candidate_above_baseline_but_below_active_cannot_promote(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(tmp_path / "registry")
    active = install_registry_fixture(registry, {"max_width": 1, "max_refine": 4})
    path = deeper_dataset(tmp_path / "data")
    result = optimize(
        path, tmp_path / "run", registry_path=registry.root, budgets=[3], max_candidates=2, _evaluator=fake_evaluator
    )
    assert result["validation_gain"] > 0
    assert result["incumbent_gain"] < 0
    assert not result["promotion_eligible"] and registry.status()["version"] == active
    rows = json.loads((tmp_path / "run/evaluations.json").read_bytes())
    assert {r["role"] for r in rows if r["split"] == "holdout"} == {"baseline", "c01", "incumbent"}
    assert not any(r["role"] == "incumbent" for r in rows if r["split"] == "train")


def test_registry_change_after_prepare_refuses_stale_commit(tmp_path, monkeypatch):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(tmp_path / "registry")
    install_registry_fixture(registry, {"max_width": 1, "max_refine": 1})
    path = deeper_dataset(tmp_path / "data")
    original_prepare = PolicyRegistry.prepare
    changed = []

    def prepare_then_change(self, run):
        result = original_prepare(self, run)
        changed.append(install_registry_fixture(self, {"max_width": 2, "max_refine": 64}))
        return result

    monkeypatch.setattr(PolicyRegistry, "prepare", prepare_then_change)
    with pytest.raises(ValueError, match="stale"):
        optimize(
            path,
            tmp_path / "run",
            registry_path=registry.root,
            budgets=[3],
            max_candidates=3,
            _evaluator=fake_evaluator,
        )
    assert registry.status()["version"] == changed[0]


def test_same_parameters_are_already_active_without_pointer_republication(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(tmp_path / "registry")
    install_registry_fixture(registry, {"max_width": 1, "max_refine": 1})
    before = registry.status()
    result = optimize(
        dataset(tmp_path / "data", "observed"),
        tmp_path / "run",
        registry_path=registry.root,
        budgets=[2],
        max_candidates=2,
        _evaluator=fake_evaluator,
    )
    assert result["reason"] == "already_active" and not result["promoted"]
    assert registry.status()["version"] == before["version"] and registry.status()["token"] == before["token"]


def test_registry_aba_change_is_stale_even_if_active_version_returns(tmp_path, monkeypatch):
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry

    registry = PolicyRegistry(tmp_path / "registry")
    old = install_registry_fixture(registry, {"max_width": 1, "max_refine": 1})
    token = registry.status()["token"]
    prepare = PolicyRegistry.prepare

    def aba(self, run):
        result = prepare(self, run)
        install_registry_fixture(self, {"max_width": 2, "max_refine": 4})
        with self._lock():
            self._replace({"schema_version": 1, "active": old, "previous": None})
        return result

    monkeypatch.setattr(PolicyRegistry, "prepare", aba)
    with pytest.raises(ValueError, match="stale"):
        optimize(
            deeper_dataset(tmp_path / "data"),
            tmp_path / "run",
            registry_path=registry.root,
            budgets=[3],
            max_candidates=3,
            _evaluator=fake_evaluator,
        )
    assert registry.status()["version"] == old and registry.status()["token"] != token
