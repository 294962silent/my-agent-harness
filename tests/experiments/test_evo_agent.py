"""Official EVO gate and production-agent contracts with fake provider/container."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from experiments.benchmark_live import evo_agent as evo
from experiments.benchmark_live.budget import BudgetLedger, BudgetStop


def prepared(tmp_path, monkeypatch):
    rows, pins = [], deepcopy(evo.PINS)
    preflight = tmp_path / "preflight"
    for iid, pin in pins.items():
        rows.append({"instance_id": iid, "repo": pin["repo"], "base_commit": pin["base_commit"],
                     "image": pin["image"].removesuffix(":latest"), "problem_statement": "Original safe problem",
                     "patch": "SECRET_GOLD", "test_patch": "SECRET_TEST", "FAIL_TO_PASS": ["SECRET_LABEL"]})
        for arm in ("gold", "empty"):
            report = preflight / (pin["short"] + "-" + arm) / "report.json"
            report.parent.mkdir(parents=True)
            resolved = arm == "gold" and pin["short"] == "dask"
            report.write_text(json.dumps({iid: {"resolved": resolved, "patch_successfully_applied": True,
                "tests_status": {"FAIL_TO_PASS": {"success": ["x"] if resolved else [], "failure": [] if resolved else ["x"]},
                                 "PASS_TO_PASS": {"success": ["y"], "failure": []}}}}))
            pin[arm + "_report_sha256"] = evo.sha(report)
    source = tmp_path / "official.arrow"
    source.write_text("fixed fixture source")
    monkeypatch.setattr(evo, "SOURCE_SHA256", evo.sha(source))
    monkeypatch.setattr(evo, "PINS", pins)
    monkeypatch.setattr(evo, "_load_rows", lambda _: rows)
    scorer = tmp_path / "scorer"
    scorer.mkdir()
    (scorer / "evaluate_instance.py").write_text("# official fixture")
    data = (scorer / "evaluate_instance.py").read_bytes()
    (scorer / "source-manifest.json").write_text(json.dumps({"revision": evo.REVISION, "entries": [{
        "path": "SWE-bench/evaluate_instance.py", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "git_blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()}]}))
    monkeypatch.setattr(evo, "FORK_MANIFEST_SHA256", evo.sha(scorer / "source-manifest.json"))
    adapter = tmp_path / "cache_adapter.py"
    adapter.write_text("# verified cache transport fixture")
    monkeypatch.setattr(evo, "CACHE_ADAPTER_SHA256", evo.sha(adapter))
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    output = tmp_path / "plan"
    plan = evo.freeze(source=source, preflight=preflight, output=output, ledger=ledger,
                      scorer_root=scorer, cache_adapter=adapter)
    return output, plan, rows


def test_only_dask_double_gate_and_safe_projection(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    assert [t["gate_passed"] for t in plan["tasks"]] == [False, True]
    assert plan["spend_ceiling_usd"] == 15.888355
    for task in plan["tasks"]:
        safe = json.loads((output / task["metadata"]).read_text())
        assert set(safe) == {"instance_id", "repo", "base_commit", "image", "problem_statement"}
        assert not any(secret in json.dumps(safe) for secret in ["SECRET_GOLD", "SECRET_TEST", "SECRET_LABEL"])
    assert evo.verify_plan(output, evo.sha(output / "plan.json"))["revision"] == evo.REVISION


def test_mutated_reference_or_safe_metadata_fails_before_ledger_change(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    before = evo.sha(plan["ledger"])
    (output / plan["tasks"][1]["metadata"]).write_text('{"patch":"leak"}')
    with pytest.raises(ValueError):
        evo.verify_plan(output, evo.sha(output / "plan.json"))
    assert evo.sha(plan["ledger"]) == before


class Environment:
    initial_head = "original-base"
    baseline = {"policy": "single_root_same_tree_no_history", "tree_equal": True, "root_commit": "0" * 40}
    started = 0
    closed = False

    async def start(self):
        self.started += 1

    async def exec(self, command):
        return {"exit_code": 0, "stdout": "visible", "stderr": ""}

    async def patch(self):
        return "diff --git a/a b/a\n"

    async def close(self):
        self.closed = True


def test_production_agent_shared_schemas_resume_and_no_hidden_prompt(tmp_path, monkeypatch):
    from my_agent_llm import Config, LLM, Response
    output, plan, _ = prepared(tmp_path, monkeypatch)
    calls = []

    class Provider:
        async def achat(self, messages, **kwargs):
            calls.append(messages)
            return Response(model=evo.LIVE_MODEL, content="done", usage={"prompt_tokens": 10, "completion_tokens": 1})

    def factory(**kw):
        llm = LLM(Config(provider="openai", model=evo.LIVE_MODEL, api_key="SYNTHETIC", max_retries=0,
                         max_tokens=kw["max_output_tokens"]))
        llm._provider = Provider()
        return llm

    monkeypatch.setattr(evo, "create_live_llm", factory)
    task = plan["tasks"][1]
    reports = []
    for arm in ("on", "off"):
        env = Environment()
        report = asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"),
                            instance_id=task["instance_id"], arm=arm, output=tmp_path / arm, environment=env))
        assert env.closed and env.started == 1
        assert report["requests"] == 1 and report["cache_tokens"] is None
        reports.append(report)
    assert reports[0]["schema_sha256"] == reports[1]["schema_sha256"]
    assert len(calls) == 2
    prompts = "\n".join(m.text_content or "" for messages in calls for m in messages)
    assert "Original safe problem" in prompts and "SECRET_" not in prompts
    for arm in ("on", "off"):
        for path in (tmp_path / arm).rglob("*"):
            if path.is_file():
                assert "SYNTHETIC" not in path.read_text(errors="ignore")
    asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=task["instance_id"],
                         arm="on", output=tmp_path / "on", resume=True, environment=Environment()))
    assert len(calls) == 2
    with pytest.raises(BudgetStop):
        asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=task["instance_id"],
                             arm="on", output=tmp_path / "new-output", environment=Environment()))


def test_conan_gate_blocks_before_environment_or_provider(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    env = Environment()
    with pytest.raises(ValueError, match="gate"):
        asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"),
                             instance_id=plan["tasks"][0]["instance_id"], arm="on", output=tmp_path / "blocked",
                             environment=env))
    assert env.started == 0


def test_gold_report_drift_is_rejected_before_execution(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    path = evo.Path(plan["tasks"][1]["gold_report"])
    path.write_text('{"forged":true}')
    with pytest.raises(ValueError, match="frozen source/report"):
        evo.verify_plan(output, evo.sha(output / "plan.json"))


def test_unknown_usage_stops_campaign_without_retry_or_next_arm(tmp_path, monkeypatch):
    from my_agent_llm import Config, LLM, Response
    output, plan, _ = prepared(tmp_path, monkeypatch)
    calls = []

    class Provider:
        async def achat(self, messages, **kwargs):
            calls.append(1)
            return Response(model=evo.LIVE_MODEL, content="done", usage=None)

    def factory(**kw):
        llm = LLM(Config(provider="openai", model=evo.LIVE_MODEL, api_key="SYNTHETIC", max_retries=0))
        llm._provider = Provider()
        return llm

    monkeypatch.setattr(evo, "create_live_llm", factory)
    iid = plan["tasks"][1]["instance_id"]
    with pytest.raises((BudgetStop, RuntimeError)):
        asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=iid, arm="on",
                             output=tmp_path / "unknown", environment=Environment()))
    env = Environment()
    with pytest.raises(BudgetStop):
        asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=iid, arm="off",
                             output=tmp_path / "must-not-run", environment=env))
    assert calls == [1] and env.started == 0


def test_pending_other_campaign_blocks_before_consuming_evo_run(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    ledger = BudgetLedger(plan["ledger"], campaign_id="memory-fixture", spend_ceiling_usd=3)
    ledger.register_run("inflight", max_requests=1, time_cap=180)
    ledger.reserve("inflight", input_bound=1, max_output=1, purpose="answer")
    env = Environment()
    iid = plan["tasks"][1]["instance_id"]
    with pytest.raises(BudgetStop, match="unsettled"):
        asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=iid, arm="on",
                             output=tmp_path / "must-not-run", environment=env))
    assert env.started == 0 and evo.run_id(iid, "on") not in ledger.snapshot()["runs"]


def test_interrupted_resume_only_collects_without_starting_environment(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    iid = plan["tasks"][1]["instance_id"]
    target = tmp_path / "interrupted"
    target.mkdir()
    evo.write_json(target / "started.json", {"run_id": evo.run_id(iid, "on"), "instance_id": iid,
                   "arm": "on", "plan_sha256": evo.sha(output / "plan.json")})
    env = Environment()
    result = asyncio.run(evo.solve(output, expected_sha=evo.sha(output / "plan.json"), instance_id=iid, arm="on",
                                  output=target, resume=True, environment=env))
    assert result["status"] == "interrupted_no_retry" and env.started == 0


@pytest.mark.skipif(os.name != "posix", reason="official scorer runs in prepared WSL")
@pytest.mark.parametrize("case", ["valid", "foreign_path", "foreign_iid", "parent_symlink", "multiple"])
def test_scoring_binds_official_namespace_and_iid(tmp_path, monkeypatch, case):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    legacy_cache = evo.Path(plan["scorer_root"]) / "__pycache__"
    legacy_cache.mkdir()
    (legacy_cache / "sitecustomize.cpython-312.pyc").write_bytes(b"untrusted old bytecode")
    iid = plan["tasks"][1]["instance_id"]
    generation = tmp_path / "generation"
    generation.mkdir()
    evo.write_json(generation / "preds.json", {iid: {"model_patch": "model edit only"}})
    evo.write_json(generation / "run.json", {"instance_id": iid, "arm": "on", "run_id": evo.run_id(iid, "on"),
        "plan_sha256": evo.sha(output / "plan.json"), "prediction_sha256": evo.sha(generation / "preds.json")})
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "MUST_NOT_REACH_GRADER")
    seen = []

    class Process:
        pid = 999999
        returncode = 0
        def __init__(self, command, *, cwd, env, **kwargs):
            seen.append((command, env))
            assert "LIVE_BENCH_API_KEY" not in env and env["CUDA_VISIBLE_DEVICES"] == "2"
            assert command[1:3] == ["-I", "-B"]
            snapshot = evo.Path(env["EVO_SCORER_ROOT"])
            assert snapshot != evo.Path(plan["scorer_root"])
            assert not list(snapshot.rglob("*.pyc"))
            path = cwd / "logs" / "run_evaluation" / evo.run_id(iid, "on") / evo.run_id(iid, "on") / iid / "report.json"
            if case == "foreign_path":
                path = cwd / "unexpected-namespace" / "report.json"
            if case == "parent_symlink":
                external = tmp_path / "external-logs"
                external.mkdir()
                (cwd / "logs").symlink_to(external, target_is_directory=True)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"foreign_instance" if case == "foreign_iid" else iid: {"resolved": False}}))
            if case == "multiple":
                (cwd / "extra").mkdir()
                (cwd / "extra" / "report.json").write_text(json.dumps({iid: {"resolved": True}}))
        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(evo.subprocess, "Popen", Process)
    monkeypatch.setattr(evo.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=b""))
    monkeypatch.setattr(evo.os, "killpg", lambda *args: None, raising=False)
    target = tmp_path / "score"
    result = evo.score(output, expected_sha=evo.sha(output / "plan.json"), instance_id=iid,
                       generation=generation, output=target)
    assert result["status"] == ("official_report_available" if case == "valid" else "environment_blocked")
    assert len(seen) == 1
    assert json.loads((target / "output_final" / (iid + ".json")).read_text())["test_patch"] == "SECRET_TEST"
    assert "--max_workers" in seen[0][0]
    if case == "valid":
        assert result["reports"][0]["raw"][iid]["resolved"] is False
    else:
        assert result["reports"] == []


@pytest.mark.parametrize("extra", ["sitecustomize.py", "hidden.pyc", "native.so", "startup.pth"])
def test_new_executable_scorer_file_is_rejected_after_freeze(tmp_path, monkeypatch, extra):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    (evo.Path(plan["scorer_root"]) / extra).write_bytes(b"unfrozen executable")
    before = evo.sha(plan["ledger"])
    with pytest.raises(ValueError, match="inventory"):
        evo.verify_plan(output, evo.sha(output / "plan.json"))
    assert evo.sha(plan["ledger"]) == before


@pytest.mark.skipif(os.name != "posix", reason="Linux parent symlink boundary")
def test_scorer_parent_symlink_is_rejected(tmp_path, monkeypatch):
    output, plan, _ = prepared(tmp_path, monkeypatch)
    scorer = evo.Path(plan["scorer_root"])
    moved = tmp_path / "moved-scorer"
    scorer.rename(moved)
    scorer.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        evo.verify_plan(output, evo.sha(output / "plan.json"))


def test_bundled_adapter_matches_its_frozen_hash_and_has_no_user_path():
    path = evo.Path(evo.__file__).with_name("evo_cache_adapter.py")
    assert evo.sha(path) == evo.CACHE_ADAPTER_SHA256
    assert "/home/" not in path.read_text() and "C:/Users/" not in path.read_text()
