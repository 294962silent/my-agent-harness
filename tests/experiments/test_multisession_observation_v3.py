"""V3 retries only bounded metadata reads and fails closed on ambiguity."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from experiments.memory_validation.multisession_observation_v3 import (
    FirstExtractReadbackV3,
    ReadbackAmbiguous,
    ReadbackError,
    ReadbackTimeout,
    ReadbackUsageUnknown,
)


def cloud(root, *, campaign="campaign", total=6, observation="obs"):
    return {
        "id": observation,
        "traceId": hashlib.sha256(root.encode()).hexdigest()[:32],
        "sessionId": campaign,
        "projectId": "project",
        "type": "GENERATION",
        "endTime": "time",
        "model": "model",
        "usageDetails": {"input": 4, "output": 2, "total": total},
        "metadata": {
            "root_id": root,
            "campaign": campaign,
            "arm": "shared_facts",
            "stage": "extract",
            "task_id": "task",
            "status": "ok",
            "provider_usage.total_tokens": total,
            "provider_usage.completion_tokens": 2,
            "provider_usage.prompt_tokens": 4,
        },
    }


def response():
    return SimpleNamespace(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})


def test_v3_polls_read_only_until_both_exact_traces_are_visible(tmp_path):
    counts = {"r1": 0, "r2": 0}
    waits = []
    now = [0.0]

    def query(root):
        counts[root] += 1
        if root == "r1" and counts[root] >= 2:
            return [cloud(root)]
        if root == "r2" and counts[root] >= 3:
            return [cloud(root)]
        return []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds

    gate = FirstExtractReadbackV3(
        SimpleNamespace(flush_and_check=lambda: True),
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "first.json",
        query=query,
        wait=wait,
        monotonic=lambda: now[0],
        window_seconds=10,
    )
    gate("r1", response(), task_id="task")
    gate("r2", response(), task_id="task")

    assert gate.verified
    assert waits == [1.0, 2.0]
    assert counts == {"r1": 2, "r2": 3}
    value = json.loads((tmp_path / "first.json").read_text(encoding="utf-8"))
    assert value["protocol"] == "locomo-multisession-four-arm-v3"
    assert value["readback_mode"] == "bounded_polling"
    assert value["attempts_per_trace"] == {"r1": 2, "r2": 3}
    assert value["extra_model_requests"] == 0
    assert len({row["trace_id"] for row in value["rows"]}) == 2


def test_v3_timeout_stays_inside_window_and_writes_isolated_failure(tmp_path):
    waits = []
    now = [0.0]
    attempts = []

    def query(root):
        attempts.append(root)
        return []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds

    gate = FirstExtractReadbackV3(
        SimpleNamespace(flush_and_check=lambda: True),
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "first.json",
        query=query,
        wait=wait,
        monotonic=lambda: now[0],
        window_seconds=4,
    )
    gate("r1", response(), task_id="task")
    with pytest.raises(ReadbackTimeout):
        gate("r2", response(), task_id="task")

    assert sum(waits) <= 4
    assert now[0] == 4
    assert attempts.count("r1") == attempts.count("r2")
    assert not gate.verified
    failed = json.loads((tmp_path / "first.failed.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["reason"] == "readback_timeout"
    assert not (tmp_path / "first.json").exists()


def test_v3_duplicate_matches_stop_before_another_read(tmp_path):
    queries = []

    def query(root):
        queries.append(root)
        return [cloud(root, observation="one"), cloud(root, observation="two")]

    gate = FirstExtractReadbackV3(
        SimpleNamespace(flush_and_check=lambda: True),
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "first.json",
        query=query,
        wait=lambda _: pytest.fail("ambiguous trace must not be retried"),
        monotonic=lambda: 0,
    )
    gate("r1", response(), task_id="task")
    with pytest.raises(ReadbackAmbiguous):
        gate("r2", response(), task_id="task")
    assert queries == ["r1"]


def test_v3_unknown_usage_stops_before_any_remote_lookup(tmp_path):
    queries = []
    gate = FirstExtractReadbackV3(
        SimpleNamespace(flush_and_check=lambda: True),
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "first.json",
        query=lambda root: queries.append(root),
        wait=lambda _: None,
    )
    with pytest.raises(ReadbackUsageUnknown):
        gate("r1", SimpleNamespace(usage={"total_tokens": 6}), task_id="task")
    assert queries == []


def test_v2_final_receipt_cannot_be_reused_as_v3_or_cross_campaign(tmp_path):
    receipt = tmp_path / "first.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "protocol": "locomo-multisession-four-arm-v2",
                "status": "verified",
                "campaign": "campaign",
                "project": "project",
                "model": "model",
                "sample_count": 2,
                "extra_model_requests": 0,
                "rows": [{}, {}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReadbackError):
        FirstExtractReadbackV3(
            SimpleNamespace(flush_and_check=lambda: True),
            campaign="campaign",
            project="project",
            model="model",
            receipt=receipt,
        )


def test_v3_exact_match_excludes_same_root_from_preserved_v1_trace():
    root = "extract.v3.s_demo"
    rows = [
        cloud(root, campaign="preserved-v1", total=8, observation="v1"),
        cloud(root, campaign="campaign", total=6, observation="v3"),
    ]
    gate = FirstExtractReadbackV3(
        SimpleNamespace(flush_and_check=lambda: True),
        campaign="campaign",
        project="project",
        model="model",
        receipt="/unused",
        query=lambda _root: rows,
    )
    found = gate._attempt(root, "task", {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
    assert found["observation_id"] == "v3"
