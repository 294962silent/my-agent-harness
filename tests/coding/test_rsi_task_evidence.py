"""Public task identity contracts; scores below are synthetic, never model results."""
import asyncio
import copy
import importlib.util
import json
import sys

import pytest

from my_coding_agent.dream_rsi_contract import (
    canonical, checksum, make_task_evidence, task_content_identity, validate_task_evidence,
)
from my_coding_agent.dream_rsi_optimizer import load_dataset

# Public prompts from the pinned openai/human-eval records, not canonical solutions/tests.
PUBLIC_7 = '''from typing import List


def filter_by_substring(strings: List[str], substring: str) -> List[str]:
    """ Filter an input list of strings only for ones that contain given substring
    >>> filter_by_substring([], 'a')
    []
    >>> filter_by_substring(['abc', 'bacd', 'cde', 'array'], 'a')
    ['abc', 'bacd', 'array']
    """
'''
PUBLIC_32 = '''import math


def poly(xs: list, x: float):
    """
    Evaluates polynomial with coefficients xs at point x.
    return xs[0] + xs[1] * x + xs[1] * x^2 + .... xs[n] * x^n
    """
    return sum([coeff * math.pow(x, i) for i, coeff in enumerate(xs)])


def find_zero(xs: list):
    """ xs are coefficients of a polynomial.
    find_zero find x such that poly(x) = 0.
    find_zero returns only only zero point, even if there are many.
    Moreover, find_zero only takes list xs having even number of coefficients
    and largest non zero coefficient as it guarantees
    a solution.
    >>> round(find_zero([1, 2]), 2) # f(x) = 1 + 2x
    -0.5
    >>> round(find_zero([-6, 11, -6, 1]), 2) # (x - 1) * (x - 2) * (x - 3) = -6 + 11x - 6x^2 + x^3
    1.0
    """
'''


def proof(text=PUBLIC_7, path="solution.py"):
    return make_task_evidence(public_input=text, visible_spec="Original prompt doctests only", input_file=path)


def trace(evidence=None, score=1):
    cell = {"evaluated": True, "combined_score": score, "n_valid": int(score == 1), "n_total": 1}
    value = {"version": 1, "n_problems": 1, "baseline": copy.deepcopy(cell), "nodes": [
        {"id": "b0_a0", "branch": 0, "attempt": 0, "parent_id": None, "path": "ignored-host-path",
         "status": "done", "raw": copy.deepcopy(cell)}]}
    if evidence is not None:
        value.update(task_evidence=evidence, task_evidence_sha256=checksum(canonical(evidence)))
    return value


def dataset(root, traces):
    entries = []
    for index, value in enumerate(traces):
        name = f"trace{index}.json"
        data = canonical(value)
        (root / name).write_bytes(data)
        entry = {"id": f"trace{index}", "task_id": f"renamable-id-{index}",
                 "split": "train" if index == 0 else "holdout", "file": name,
                 "sha256": checksum(data), "provenance": "synthetic scores, public task evidence contract only"}
        if "task_evidence_sha256" in value:
            entry["task_evidence_sha256"] = value["task_evidence_sha256"]
        entries.append(entry)
    path = root / "manifest.json"
    path.write_bytes(canonical({"schema_version": 1, "kind": "synthetic", "source": "offline evidence contract", "entries": entries}))
    return path


def test_distinct_official_public_prompts_allow_same_all_one_curve(tmp_path):
    value = load_dataset(dataset(tmp_path, [trace(proof(PUBLIC_7)), trace(proof(PUBLIC_32))]))
    assert value["entries"][0]["semantic_sha256"] == value["entries"][1]["semantic_sha256"]
    assert value["entries"][0]["task_content_sha256"] != value["entries"][1]["task_content_sha256"]


@pytest.mark.parametrize("changed_score", [False, True])
def test_same_public_input_rejects_renamed_id_path_and_even_changed_scores(tmp_path, changed_score):
    second = trace(proof(path="renamed.py"))
    if changed_score:
        second["baseline"]["combined_score"] = 0
        second["baseline"]["n_valid"] = 0
    with pytest.raises(ValueError, match="content|task"):
        load_dataset(dataset(tmp_path, [trace(proof()), second]))


@pytest.mark.parametrize("field", ["public_input_sha256", "visible_spec_sha256"])
def test_self_declared_or_drifted_hash_fails(field):
    value = proof()
    value[field] = "0" * 64
    with pytest.raises(ValueError):
        validate_task_evidence(value)


def test_content_identity_ignores_input_path_and_visible_relabeling():
    first = proof()
    second = make_task_evidence(public_input=PUBLIC_7, visible_spec="Changed public check", input_file="other.py")
    assert task_content_identity(first) == task_content_identity(second)
    assert checksum(canonical(first)) != checksum(canonical(second))


def test_mixed_verified_and_legacy_same_curve_remains_conservative(tmp_path):
    with pytest.raises(ValueError, match="meaning"):
        load_dataset(dataset(tmp_path, [trace(proof()), trace()]))


def test_legacy_datasets_keep_prior_compatibility_and_duplicate_rule(tmp_path):
    first, second = trace(), trace()
    second["baseline"].update(combined_score=0, n_valid=0)
    assert len(load_dataset(dataset(tmp_path, [first, second]))["entries"]) == 2
    with pytest.raises(ValueError, match="meaning"):
        load_dataset(dataset(tmp_path, [first, first]))


def test_manifest_proof_digest_must_bind_the_trace(tmp_path):
    manifest = dataset(tmp_path, [trace(proof(PUBLIC_7)), trace(proof(PUBLIC_32))])
    data = json.loads(manifest.read_text())
    data["entries"][1]["task_evidence_sha256"] = "0" * 64
    manifest.write_bytes(canonical(data))
    with pytest.raises(ValueError, match="evidence"):
        load_dataset(manifest)


def test_runtime_rejects_input_drift_before_any_provider_or_evaluation(tmp_path):
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, ParentBudget, RsiRuntime
    source = tmp_path / "source"
    source.mkdir()
    (source / "solution.py").write_bytes(PUBLIC_7.encode())
    factory = LocalBranchFactory(source, tmp_path / "out")
    budget = ParentBudget(max_requests=1, max_tokens=100000, max_seconds=30, max_evaluations=2)
    evidence = proof()
    runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator((sys.executable, "-c", "pass")),
        budget=budget, policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id="input", split="train",
        task_evidence=evidence)
    evidence["public_input"] = "external mutation should not affect frozen proof"
    (factory.baseline / "solution.py").write_text("changed")
    with pytest.raises(ValueError, match="input|baseline"):
        asyncio.run(runtime.run("fixture", llm=object()))
    assert not budget.records and not budget.evaluations


def test_runtime_freezes_public_evidence_and_export_binds_both_same_curve_tasks(tmp_path):
    from my_agent_llm import Config, Response
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, ParentBudget, RsiRuntime, TaskSplits, export_dataset
    class Synthetic:
        model = "synthetic-evidence-contract"
        config = Config(provider="openai", model=model, max_tokens=64, max_retries=0)
        async def achat(self, messages, **kwargs):
            return Response(model=self.model, content="fixture", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    async def exercise():
        results = []
        for index, text in enumerate((PUBLIC_7, PUBLIC_32)):
            source = tmp_path / f"source{index}"
            source.mkdir()
            (source / "solution.py").write_bytes(text.encode())
            value = proof(text)
            frozen = copy.deepcopy(value)
            runtime = RsiRuntime(factory=LocalBranchFactory(source, tmp_path / f"out{index}"),
                evaluator=VisibleCommandEvaluator((sys.executable, "-c", "pass")),
                budget=ParentBudget(max_requests=1, max_tokens=100000, max_seconds=30, max_evaluations=2),
                policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id=f"task{index}",
                split="train" if index == 0 else "val", task_evidence=value)
            value["visible_spec"] = "external mutation"
            result = await runtime.run("synthetic evidence fixture", llm=Synthetic())
            observed_trace = json.loads(result.trace_path.read_text())
            assert observed_trace["task_evidence"] == frozen
            assert observed_trace["task_evidence_sha256"] == result.report["task_evidence_sha256"]
            results.append(result)
        splits = TaskSplits(("task0",), ("task1",), ("untouched-test",))
        manifest = export_dataset(results, splits, tmp_path / "dataset")
        loaded = load_dataset(manifest)
        assert loaded["kind"] == "synthetic"
        assert all(entry["task_content_sha256"] for entry in loaded["entries"])
        results[0].report["task_evidence_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="evidence"):
            export_dataset(results, splits, tmp_path / "tampered-export")
    asyncio.run(exercise())


@pytest.mark.skipif(importlib.util.find_spec("dream_rsi") is None, reason="optional pinned Dream SDK required")
def test_actual_offline_sdk_accepts_public_evidence_without_changing_scores(tmp_path):
    from my_coding_agent.dream_rsi_optimizer import optimize
    manifest = dataset(tmp_path, [trace(proof(PUBLIC_7)), trace(proof(PUBLIC_32))])
    result = optimize(manifest, tmp_path / "optimizer", registry_path=tmp_path / "private-registry",
        dry_run=True, budgets=(1,), max_candidates=2, max_workers=1, timeout_seconds=30, max_seconds=120)
    assert result["status"] == "completed" and result["kind"] == "synthetic"
    assert result["promoted"] is False and result["validation_gain"] == 0
