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
