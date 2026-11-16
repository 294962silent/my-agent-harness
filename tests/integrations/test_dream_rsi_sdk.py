"""Real pinned DREAM-RSI through production CLI and isolated worker, offline only."""

import hashlib
import importlib.util
import json

import pytest


def scored_tree():
    """Synthetic scored three-node schema/SDK contract; not real task scores."""

    def score(value):
        return {"evaluated": True, "combined_score": value, "n_valid": 1, "n_total": 1}

    return {
        "version": 1,
        "workspace": "synthetic-scored-contract",
        "exported_at": "2026-11-12T00:00:00+00:00",
        "n_problems": 1,
        "baseline": score(1.0),
        "nodes": [
            {
                "id": "b0_a0",
                "branch": 0,
                "attempt": 0,
                "parent_id": None,
                "intent": "",
                "tags": [],
                "path": "not-accessed/b0_a0",
                "status": "done",
                "raw": score(2.0),
            },
            {
                "id": "b0_a1",
                "branch": 0,
                "attempt": 1,
                "parent_id": "b0_a0",
                "intent": "",
                "tags": [],
                "path": "not-accessed/b0_a1",
                "status": "done",
                "raw": score(3.0),
            },
            {
                "id": "b1_a0",
                "branch": 1,
                "attempt": 0,
                "parent_id": None,
                "intent": "",
                "tags": [],
                "path": "not-accessed/b1_a0",
                "status": "done",
                "raw": score(1.5),
            },
        ],
    }


@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None, reason="optional real Dream SDK not installed")
def test_real_pinned_sdk_replay_reference_and_manifest(tmp_path, monkeypatch):
    from my_coding_agent.dream_rsi_replay import SOURCE_COMMIT, main

    source = tmp_path / "trace.json"
    source.write_text(json.dumps(scored_tree()), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "MUST_NOT_BE_INHERITED")
    output = tmp_path / "result"
    assert main(["--trace", str(source), "--output", str(output), "--budget", "2", "--max-workers", "2"]) == 0
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["status"] == "completed" and summary["mode"] == "offline_replay"
    assert summary["policy_kind"] == "fixed_reference"
    assert summary["upstream"]["commit"] == SOURCE_COMMIT
    assert summary["upstream"]["version"] == "0.1.0"
    assert summary["result"]["budget"] == 2 and summary["result"]["best"] == 2.0
    assert summary["policy"] == "baseline" and "beta" not in summary
    episode = json.loads((output / "decisions.json").read_bytes())
    assert episode["mode"] == "replay" and episode["final"]["budget"] == 2
    manifest = json.loads((output / "manifest.json").read_bytes())
    for row in manifest["files"]:
        body = (output / row["file"]).read_bytes()
        assert row["sha256"] == hashlib.sha256(body).hexdigest() and row["bytes"] == len(body)
    assert "MUST_NOT_BE_INHERITED" not in "".join(p.read_text() for p in output.iterdir() if p.is_file())


@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None, reason="optional real Dream SDK not installed")
def test_real_sdk_parameter_search_synthetic_only_and_registry_consumption(tmp_path, monkeypatch):
    """Real SDK function check, not observed learning or an improvement benchmark.

    The temporary registry below is explicitly injected fixture state. Synthetic
    optimizer results cannot create it through production promotion.
    """
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.dream_rsi_registry import PolicyRegistry
    from my_coding_agent.dream_rsi_contract import SOURCE_COMMIT, canonical, checksum, policy_identity
    from my_coding_agent.dream_rsi_replay import replay_trace

    data = tmp_path / "data"
    data.mkdir()
    entries = []
    for split, score in (("train", 3.0), ("holdout", 3.5)):
        trace = scored_tree()
        trace["nodes"][1]["raw"]["combined_score"] = score
        raw = canonical(trace)
        (data / (split + ".json")).write_bytes(raw)
        entries.append(
            {
                "id": split,
                "task_id": split + "-synthetic-sdk-contract",
                "split": split,
                "file": split + ".json",
                "sha256": checksum(raw),
                "provenance": "synthetic SDK function check",
            }
        )
    dataset = data / "dataset.json"
    dataset.write_bytes(
        canonical({"schema_version": 1, "kind": "synthetic", "source": "SDK contract fixture", "entries": entries})
    )
    registry = PolicyRegistry(tmp_path / "registry")
    result = optimize(
        dataset, tmp_path / "search", registry_path=registry.root, budgets=[2], max_candidates=2, dry_run=True
    )
    assert result["status"] == "completed" and not result["promoted"] and not result["promotion_eligible"]
    assert result["selected"]["parameters"] == {"max_width": 1, "max_refine": 1}
    assert registry.status()["reason"] == "no_active_policy"
    rows = json.loads((tmp_path / "search/evaluations.json").read_bytes())
    assert {r["role"] for r in rows if r["split"] == "holdout"} == {"baseline", result["selected"]["role"]}
    parameters = result["selected"]["parameters"]
    fixture_version = {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "parameters": parameters,
        "policy_hash": policy_identity(parameters),
        "receipt": {"evidence_sha256": "0" * 64, "plan_sha256": "1" * 64, "summary_sha256": "2" * 64},
    }
    version_raw = canonical(fixture_version)
    version = checksum(version_raw)
    with registry._lock():
        (registry.root / "versions").mkdir()
        (registry.root / "versions" / (version + ".json")).write_bytes(version_raw)
        registry._replace({"schema_version": 1, "active": version, "previous": None})
    from types import SimpleNamespace
    from my_coding_agent import settings

    calls = []

    def global_settings(*, cwd):
        calls.append(cwd)
        return SimpleNamespace(dream_optimization=SimpleNamespace(enabled=False, registry_path=registry.root))

    monkeypatch.setattr(settings, "load_settings", global_settings)
    active = replay_trace(data / "train.json", tmp_path / "active", budget=2)
    assert calls == [None]  # Optimization disabled does not revoke an active policy.
    assert active["result"]["best"] == 3.0  # actually refines b0 instead of opening b1
    assert active["policy_kind"] == "controlled_parameters" and active["policy_version"] == version
    assert active["parameters"]["effective"] == parameters
    clamped = replay_trace(
        data / "train.json",
        tmp_path / "clamped",
        budget=2,
        parameters={"max_width": 64, "max_refine": 64},
        use_active=False,
    )
    assert clamped["parameters"]["requested"] == {"max_width": 64, "max_refine": 64}
    assert clamped["parameters"]["effective"] == {"max_width": 2, "max_refine": 1}
    (registry.root / "versions" / (version + ".json")).write_text("{}")
    fallback = replay_trace(data / "train.json", tmp_path / "fallback", budget=2, registry_path=registry.root)
    assert fallback["result"]["best"] == 2.0 and fallback["selection_reason"] == "invalid_registry"
