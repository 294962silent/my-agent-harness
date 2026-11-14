"""V2 first-two readback is exact-once per trace and fail closed."""

import json
from types import SimpleNamespace

import pytest

from experiments.memory_validation.multisession_observation_v2 import FirstExtractReadbackV2
from tests.experiments.test_multisession_observation import cloud


def test_v2_reads_each_first_trace_once_and_records_no_retry(tmp_path):
    queries = []

    def query(root):
        queries.append(root)
        return [cloud(root)]

    owner = SimpleNamespace(flush_and_check=lambda: True)
    receipt = tmp_path / "readback.json"
    gate = FirstExtractReadbackV2(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=receipt,
        query=query,
        wait=lambda _: pytest.fail("v2 readback must not wait or retry"),
    )
    response = SimpleNamespace(usage={"total_tokens": 6})
    gate("r1", response, task_id="task")
    gate("r2", response, task_id="task")

    assert queries == ["r1", "r2"]
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["readback_attempts_per_trace"] == 1
    assert value["extra_model_requests"] == 0


def test_v2_missing_cloud_trace_stops_after_one_lookup_per_trace(tmp_path):
    queries = []

    def query(root):
        queries.append(root)
        return []

    owner = SimpleNamespace(flush_and_check=lambda: True)
    gate = FirstExtractReadbackV2(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "readback.json",
        query=query,
        wait=lambda _: pytest.fail("v2 readback must not wait or retry"),
    )
    response = SimpleNamespace(usage={"total_tokens": 6})
    gate("r1", response, task_id="task")
    with pytest.raises(RuntimeError):
        gate("r2", response, task_id="task")

    assert queries == ["r1", "r2"]
    assert not gate.verified
