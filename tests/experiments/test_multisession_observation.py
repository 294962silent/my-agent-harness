"""Metadata-only first-two normal-request cloud gate, no paid probe."""

import hashlib
from types import SimpleNamespace
import pytest
from experiments.memory_validation.multisession_observation import FirstExtractReadback, match_cloud


def cloud(root, session="campaign", parent=None):
    return {
        "traceId": hashlib.sha256(root.encode()).hexdigest()[:32],
        "sessionId": session,
        "projectId": "project",
        "type": "GENERATION",
        "endTime": "time",
        "model": "model",
        "usageDetails": {"input": 4, "output": 2, "total": 6},
        "metadata": {
            "root_id": root,
            "campaign": "campaign",
            "arm": "shared_facts",
            "stage": "extract",
            "task_id": "task",
            "status": "ok",
            "parent_id": parent,
        },
    }


@pytest.mark.parametrize("parent", [None, {}, '{"round":1}'])
def test_remote_metadata_accepts_native_object_or_string_parent(parent):
    assert match_cloud(
        [cloud("r1", parent=parent)],
        "r1",
        campaign="campaign",
        project="project",
        model="model",
        task_id="task",
        usage={"total_tokens": 6},
    )


def test_remote_usage_and_campaign_must_match():
    row = cloud("r1", session="wrong")
    assert not match_cloud(
        [row], "r1", campaign="campaign", project="project", model="model", task_id="task", usage={"total_tokens": 6}
    )


def test_gate_queries_first_two_ordinary_calls_then_allows_third(tmp_path):
    roots = []

    def query(root):
        roots.append(root)
        return [cloud(root)]

    owner = SimpleNamespace(flush_and_check=lambda: True)
    gate = FirstExtractReadback(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "readback.json",
        query=query,
        wait=lambda _: None,
    )
    response = SimpleNamespace(usage={"total_tokens": 6})
    gate("r1", response, task_id="task")
    assert not roots
    gate("r2", response, task_id="task")
    assert roots == ["r1", "r2"] and gate.verified
    gate("r3", response, task_id="task")
    assert roots == ["r1", "r2"]


def test_missing_cloud_second_call_blocks_without_paid_retry(tmp_path):
    owner = SimpleNamespace(flush_and_check=lambda: True)
    gate = FirstExtractReadback(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=tmp_path / "r.json",
        query=lambda root: [],
        wait=lambda _: None,
    )
    gate("r1", SimpleNamespace(usage={"total_tokens": 6}), task_id="task")
    with pytest.raises(RuntimeError):
        gate("r2", SimpleNamespace(usage={"total_tokens": 6}), task_id="task")
    assert not gate.verified and len(gate.calls) == 2


def test_verified_receipt_is_reused_by_the_next_dev_or_test_process(tmp_path):
    owner = SimpleNamespace(flush_and_check=lambda: True)
    receipt = tmp_path / "readback.json"
    gate = FirstExtractReadback(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=receipt,
        query=lambda root: [cloud(root)],
        wait=lambda _: None,
    )
    response = SimpleNamespace(usage={"total_tokens": 6})
    gate("r1", response, task_id="task")
    gate("r2", response, task_id="task")

    def forbidden_query(_root):
        raise AssertionError("verified first-call receipt must avoid a second cloud gate")

    resumed = FirstExtractReadback(
        owner,
        campaign="campaign",
        project="project",
        model="model",
        receipt=receipt,
        query=forbidden_query,
        wait=lambda _: None,
    )
    assert resumed.verified
    resumed("r3", response, task_id="task")
