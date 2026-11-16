"""Full zero-provider contract, not evidence of model task-quality gains."""
import asyncio
import importlib.util
import json

import pytest

from my_coding_agent.rsi_runtime import TaskSplits, run_contract_demo


def test_task_split_overlap_is_rejected():
    with pytest.raises(ValueError):
        TaskSplits(train=("same",), val=("same",), test=("other",))


@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None, reason="pinned optional Dream SDK required")
def test_real_tools_to_trace_to_dryrun_optimizer_to_frozen_execution(tmp_path):
    summary = asyncio.run(run_contract_demo(tmp_path / "closed-loop"))
    assert summary["actual_provider_calls"] == 0 and summary["quality"] == "unmeasured"
    assert summary["optimizer"]["promoted"] is False
    assert summary["optimizer"]["kind"] == "synthetic"
    assert set(summary["arms"]) == {"A", "B", "C", "random"}
    assert len({json.dumps(r["budget"]["limits"], sort_keys=True) for r in summary["arms"].values()}) == 1
    schemas = {a["tool_schema_sha256"] for r in summary["arms"].values() for a in r["attempts"]}
    assert len(schemas) == 1
    assert all(r["final_test"]["raw_passed"] is True for r in summary["arms"].values())
    assert all(r["quality"] == "unmeasured" and r["auto_promoted"] is False for r in summary["arms"].values())
    for arm in ("B", "C"):
        assert sum(a["context_activity"]["phase_fold_views"] for a in summary["arms"][arm]["attempts"]) > 0
        assert sum(a["context_activity"]["micro_groups"] for a in summary["arms"][arm]["attempts"]) > 0
    dataset = json.loads((tmp_path / "closed-loop/dataset/manifest.json").read_text())
    assert {e["split"] for e in dataset["entries"]} == {"train", "holdout"}
    assert all(e["task_id"] != "contract-test" for e in dataset["entries"])
    assert (tmp_path / "closed-loop/test-source/answer.txt").read_text() == "0"
