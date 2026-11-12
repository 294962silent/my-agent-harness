"""Campaign reproducibility/reentry with fake solver and scorer only."""

import json
from pathlib import Path

import pytest

from experiments.benchmark_live import swe_campaign as campaign
from experiments.benchmark_live.budget import BudgetLedger


def official_summary(instance, kind):
    report = {"schema_version": 2, "total_instances": 1, "incomplete_ids": [], "failure_reasons": {}}
    present = {"submitted", "completed", "resolved"} if kind == "resolved" else {"submitted", "empty_patch"}
    for name in (
        "submitted",
        "completed",
        "resolved",
        "unresolved",
        "empty_patch",
        "error",
        "infra_failure",
        "ambiguous_failure",
    ):
        report[name + "_instances"] = int(name in present)
        report[name + "_ids"] = [instance] if name in present else []
    return report


def make_inputs(tmp_path, n=2, blocked=False):
    tasks = []
    inputs = []
    for i in range(n):
        instance = f"sympy__sympy-{20590 + i}"
        base = f"{i + 1:040x}"
        image = f"swebench/sweb.eval.x86_64.sympy_1776_sympy-{20590 + i}:latest"
        image_id = "sha256:" + f"{i + 1:064x}"
        folder = tmp_path / instance
        folder.mkdir()
        metadata = folder / "metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "instance_id": instance,
                    "repo": "sympy/sympy",
                    "base_commit": base,
                    "image": image,
                    "problem_statement": "visible issue",
                }
            )
        )
        report = folder / "gold-report.json"
        report.write_text(
            json.dumps(
                {
                    "resolved_ids": [instance] if not blocked else [],
                    "completed_instances": 1,
                    "resolved_instances": 0 if blocked else 1,
                    "error_instances": 0,
                }
            )
        )
        gold = folder / "gold.json"
        gold.write_text(
            json.dumps(
                {
                    "instance_id": instance,
                    "base_commit": base,
                    "image": image,
                    "image_id": image_id,
                    "metadata_sha256": campaign.sha(metadata),
                    "resolved": not blocked,
                    "official_report": str(report),
                    "official_report_sha256": campaign.sha(report),
                    "task_repository_revision": "a" * 40,
                }
            )
        )
        dataset = folder / "dataset.json"
        dataset.write_text('[{"patch":"SYNTHETIC_GOLD_NOT_FOR_SOLVER"}]')
        official = folder / "tasks" / instance
        official.mkdir(parents=True)
        for name in (
            "task.yaml",
            "eval.sh",
            "gold.patch",
            "test.patch",
            "tests.json",
            "Dockerfile",
            "problem_statement.md",
        ):
            (official / name).write_text("SYNTHETIC_OFFICIAL_" + name)
        tasks.append(
            {
                "instance_id": instance,
                "repo": "sympy/sympy",
                "base_commit": base,
                "image": image,
                "image_id": image_id,
                "metadata_sha256": campaign.sha(metadata),
            }
        )
        inputs.append(
            {
                "instance_id": instance,
                "metadata": str(metadata),
                "gold_evidence": str(gold),
                "evaluation_dataset": str(dataset),
                "evaluation_cwd": str(folder),
            }
        )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {"repository": "https://github.com/SWE-bench/swe-bench-tasks", "revision": "a" * 40},
                "tasks": tasks,
            }
        )
    )
    task_inputs = tmp_path / "inputs.json"
    task_inputs.write_text(json.dumps({"tasks": inputs}))
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    return dict(
        task_inputs=task_inputs,
        catalog_path=catalog,
        catalog_sha256=campaign.sha(catalog),
        ledger_path=ledger,
        output=tmp_path / "campaign",
    )


def test_plan_balanced_freeze_and_changed_input_rejected(tmp_path):
    args = make_inputs(tmp_path, n=6)
    root = campaign.plan_campaign(**args)
    plan = json.loads((root / "plan.json").read_text())
    assert len(plan["trials"]) == 12
    first = [plan["trials"][i]["arm"] for i in range(0, 12, 2)]
    assert first.count("on") == first.count("off") == 3
    assert plan["policy"]["seed"] == 23 and plan["policy"]["batch_budget_usd"] == "3"
    assert plan["policy"]["max_requests"] == 16 and plan["policy"]["max_output_tokens"] == 4096
    task = Path(plan["tasks"][0]["metadata"])
    task.write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        campaign.summarize_campaign(root)


@pytest.mark.anyio
async def test_run_resume_scores_and_hash_protection_no_duplicate_solver(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path))
    calls = []

    async def executor(*, trial, plan, output):
        calls.append(trial["trial_id"])
        assert "evaluation_dataset" not in trial
        output.mkdir()
        (output / "run.json").write_text(
            json.dumps(
                {
                    "adapter_protocol": "v2",
                    "run_id": trial["run_id"],
                    "status": "end_turn",
                    "requests": 1,
                    "usage_available": True,
                    "usage": [],
                    "cost_usd": None,
                }
            )
        )
        (output / "model_patch.jsonl").write_text(
            json.dumps(
                {
                    "instance_id": trial["instance_id"],
                    "model_name_or_path": "fixture",
                    "model_patch": "visible model patch",
                }
            )
            + "\n"
        )

    await campaign.run_campaign(root, executor=executor)
    await campaign.run_campaign(root, executor=executor)
    assert len(calls) == 4

    async def scorer(*, trial, task, output, prediction):
        assert prediction.is_file()
        output.mkdir()
        path = output / "official.json"
        path.write_text(json.dumps(official_summary(trial["instance_id"], "resolved")))
        return path

    await campaign.score_campaign(root, scorer=scorer)
    summary = campaign.summarize_campaign(root)
    assert summary["selected_tasks"] == summary["eligible_tasks"] == 2
    assert summary["attempted_trials"] == summary["scored_trials"] == summary["resolved_trials"] == 4
    assert summary["complete_pairs"] == 2
    assert summary["budget"]["usage_complete"] is False
    plan = json.loads((root / "plan.json").read_text())
    patch = root / "trials" / plan["trials"][0]["trial_id"] / "agent/model_patch.jsonl"
    patch.write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        await campaign.run_campaign(root, executor=executor)
    assert len(calls) == 4


@pytest.mark.anyio
async def test_blocked_gold_retained_and_started_never_retried(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path, blocked=True))

    async def forbidden(**kwargs):
        raise AssertionError("blocked task must not call solver")

    await campaign.run_campaign(root, executor=forbidden)
    summary = campaign.summarize_campaign(root)
    assert summary["blocked_tasks"] == 2 and summary["attempted_trials"] == 0
    assert summary["scored_trials"] == 0
    plan = json.loads((root / "plan.json").read_text())
    trial = plan["trials"][0]
    state = root / "trials" / trial["trial_id"] / "state.json"
    row = json.loads(state.read_text())
    row["status"] = "started"
    state.write_text(json.dumps(row))
    result = await campaign.run_campaign(root, executor=forbidden)
    assert result["requires_reconciliation"] == 1


def test_missing_failed_gold_report_blocks_only_that_selected_task(tmp_path):
    args = make_inputs(tmp_path)
    rows = json.loads(Path(args["task_inputs"]).read_text())["tasks"]
    gold = Path(rows[1]["gold_evidence"])
    evidence = json.loads(gold.read_text())
    evidence["resolved"] = False
    gold.write_text(json.dumps(evidence))
    Path(evidence["official_report"]).unlink()
    root = campaign.plan_campaign(**args)
    summary = campaign.summarize_campaign(root)
    assert summary["selected_tasks"] == 2 and summary["eligible_tasks"] == summary["blocked_tasks"] == 1
    assert summary["budget"]["usage_complete"] is False


@pytest.mark.anyio
async def test_unknown_usage_stops_current_and_reentered_campaign(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path))
    calls = []

    async def executor(*, trial, plan, output):
        calls.append(trial["run_id"])
        BudgetLedger(plan["ledger_path"]).reserve(trial["run_id"], input_bound=1, max_output=1, purpose="agent")
        output.mkdir()
        (output / "run.json").write_text(
            json.dumps({"adapter_protocol": "v2", "run_id": trial["run_id"], "status": "BudgetStop", "requests": 1})
        )
        (output / "model_patch.jsonl").write_text("{}\n")

    await campaign.run_campaign(root, executor=executor)
    result = await campaign.run_campaign(root, executor=executor)
    assert len(calls) == 1 and result["halted_unknown_usage"]
    summary = campaign.summarize_campaign(root)
    assert summary["budget"]["usage_complete"] is False and summary["budget"]["campaign_cost_usd"] is None


@pytest.mark.anyio
async def test_default_scorer_uses_official_command_without_model_credentials(tmp_path, monkeypatch):
    args = make_inputs(tmp_path, n=1)
    task = json.loads(Path(args["task_inputs"]).read_text())["tasks"][0]
    prediction = tmp_path / "prediction.jsonl"
    prediction.write_text("{}\n")
    output = tmp_path / "score"
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setattr(campaign.importlib.metadata, "version", lambda name: "5.0.2")
    captured = {}

    async def create(*command, **kwargs):
        captured.update(command=command, **kwargs)
        (output / "official.json").write_text(json.dumps(official_summary(task["instance_id"], "resolved")))

        class Process:
            async def wait(self):
                return 0

        return Process()

    monkeypatch.setattr(campaign.asyncio, "create_subprocess_exec", create)
    report = await campaign._score(
        trial={"instance_id": task["instance_id"], "score_run_id": "frozen-id"},
        task=task,
        output=output,
        prediction=prediction,
    )
    assert report == output / "official.json"
    assert captured["command"][1:3] == ("-m", "swebench.harness.run_evaluation")
    assert "--predictions_path" in captured["command"] and "--dataset_name" in captured["command"]
    assert not any(k in captured["env"] for k in ("LIVE_BENCH_API_KEY", "OPENAI_API_KEY"))
    assert "SYNTHETIC_SECRET" not in (output / "command.json").read_text()
    derived = json.loads((output / "analysis-outcome.json").read_text())
    assert derived["analysis_parser_version"] and derived["outcome"]["resolved"] is True


@pytest.mark.anyio
async def test_cancelled_official_scorer_reaps_its_process(tmp_path, monkeypatch):
    import asyncio

    args = make_inputs(tmp_path, n=1)
    task = json.loads(Path(args["task_inputs"]).read_text())["tasks"][0]
    monkeypatch.setattr(campaign.importlib.metadata, "version", lambda name: "5.0.2")
    entered = asyncio.Event()
    released = asyncio.Event()

    class Process:
        pid = None

        def __init__(self):
            self.terminated = False
            self.waits = 0

        async def wait(self):
            self.waits += 1
            entered.set()
            await released.wait()
            return -15

        def terminate(self):
            self.terminated = True
            released.set()

        def kill(self):
            released.set()

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(campaign.asyncio, "create_subprocess_exec", create)
    cleaned = []

    async def cleanup(trial, env):
        cleaned.append(trial["score_run_id"])
        return []

    monkeypatch.setattr(campaign, "_cleanup_score_containers", cleanup)
    pending = asyncio.create_task(
        campaign._score(
            trial={"instance_id": task["instance_id"], "score_run_id": "frozen"},
            task=task,
            output=tmp_path / "score",
            prediction=tmp_path / "prediction",
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.terminated and process.waits >= 2 and cleaned == ["frozen"]


@pytest.mark.anyio
async def test_container_cleanup_never_removes_another_run(monkeypatch):
    trial = {"instance_id": "sympy__sympy-20590", "score_run_id": "campaign-frozen-01"}
    name = "sweb.eval.sympy__sympy-20590.campaign-frozen-01"
    own = "a" * 12
    other = "b" * 12
    removed = []

    async def output(args, env):
        if args[0] == "ps":
            assert args[-1].startswith("name=^/") and "campaign" in args[-1]
            return own + "\n" + other
        if args[0] == "inspect":
            return "/" + (name + ".123" if args[-1] == own else name + "-other-run")
        if args[0] == "rm":
            removed.append(args[-1])
            return ""
        raise AssertionError("unexpected Docker operation")

    monkeypatch.setattr(campaign, "_docker_output", output)
    with pytest.raises(ValueError, match="does not belong"):
        await campaign._cleanup_score_containers(trial, {})
    assert removed == [own]


def test_campaign_lock_excludes_a_second_process(tmp_path):
    import os
    import subprocess
    import sys
    from filelock import FileLock

    root = campaign.plan_campaign(**make_inputs(tmp_path, n=1))
    code = """import asyncio, sys
from pathlib import Path
from filelock import Timeout
from experiments.benchmark_live.swe_campaign import run_campaign
async def forbidden(**kwargs):
    Path(sys.argv[2]).write_text("DUPLICATE_EXECUTOR")
try:
    asyncio.run(run_campaign(sys.argv[1],executor=forbidden))
except Timeout:
    raise SystemExit(0)
raise SystemExit(1)
"""
    env = os.environ.copy()
    env.pop("LIVE_BENCH_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join((str(campaign.ROOT / "src"), str(campaign.ROOT)))
    marker = tmp_path / "duplicate"
    with FileLock(str(root / ".campaign.lock"), timeout=0):
        result = subprocess.run(
            [sys.executable, "-c", code, str(root), str(marker)], env=env, capture_output=True, timeout=30
        )
    assert result.returncode == 0 and not marker.exists(), result.stderr.decode(errors="replace")


def test_growing_shared_ledger_is_not_a_frozen_input(tmp_path):
    args = make_inputs(tmp_path, n=1)
    root = campaign.plan_campaign(**args)
    ledger = BudgetLedger(args["ledger_path"])
    request = ledger.reserve("outside", input_bound=10, max_output=1, purpose="agent")
    ledger.settle(request, usage={"prompt_tokens": 10, "completion_tokens": 0}, status="ok")
    summary = campaign.summarize_campaign(root)
    assert summary["budget"]["global_settled_usd"] == 0.000003
    plan = json.loads((root / "plan.json").read_text())
    assert not any(Path(r["path"]) == args["ledger_path"] for r in plan["input_files"])


@pytest.mark.anyio
async def test_bounded_run_stops_after_one_pair_then_resumes_without_duplicate_execution(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path, n=2))
    calls = []

    async def executor(*, trial, plan, output):
        calls.append(trial["trial_id"])
        output.mkdir()
        (output / "run.json").write_text(
            json.dumps({"adapter_protocol": "v2", "run_id": trial["run_id"], "status": "end_turn", "requests": 0})
        )
        (output / "model_patch.jsonl").write_text("{}\n")

    plan = json.loads((root / "plan.json").read_text())
    frozen = campaign.sha(root / "plan.json")
    first = await campaign.run_campaign(root, executor=executor, max_new_trials=2)
    assert first["completed"] == 2 and len(calls) == 2
    assert {t["arm"] for t in plan["trials"][:2]} == {"on", "off"}
    assert plan["trials"][0]["instance_id"] == plan["trials"][1]["instance_id"]
    second = await campaign.run_campaign(root, executor=executor, max_new_trials=2)
    assert second["completed"] == 2 and second["skipped"] == 2
    assert len(calls) == len(set(calls)) == 4 and campaign.sha(root / "plan.json") == frozen


@pytest.mark.anyio
async def test_run_rejects_nonpositive_new_trial_limit_before_execution(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path, n=1))

    async def forbidden(**kwargs):
        raise AssertionError("invalid limit must not execute")

    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            await campaign.run_campaign(root, executor=forbidden, max_new_trials=limit)
    assert not (root / "trials").exists()


@pytest.mark.anyio
async def test_summary_rederives_empty_official_report_without_rewriting_old_state(tmp_path):
    root = campaign.plan_campaign(**make_inputs(tmp_path, n=1))

    async def executor(*, trial, plan, output):
        output.mkdir()
        (output / "run.json").write_text(
            json.dumps(
                {"adapter_protocol": "v2", "run_id": trial["run_id"], "status": "budget_exhausted", "requests": 0}
            )
        )
        (output / "model_patch.jsonl").write_text(
            json.dumps({"instance_id": trial["instance_id"], "model_name_or_path": "fake", "model_patch": ""}) + "\n"
        )

    await campaign.run_campaign(root, executor=executor)

    async def scorer(*, trial, task, output, prediction):
        output.mkdir()
        report = {"schema_version": 2, "total_instances": 1, "incomplete_ids": [], "failure_reasons": {}}
        for name in (
            "submitted",
            "completed",
            "resolved",
            "unresolved",
            "empty_patch",
            "error",
            "infra_failure",
            "ambiguous_failure",
        ):
            report[name + "_instances"] = int(name in ("submitted", "empty_patch"))
            report[name + "_ids"] = [trial["instance_id"]] if name in ("submitted", "empty_patch") else []
        path = output / "official.json"
        path.write_text(json.dumps(report))
        return path

    await campaign.score_campaign(root, scorer=scorer)
    states = list((root / "trials").glob("*/score-state.json"))
    for path in states:
        cached = json.loads(path.read_text())
        cached["score_category"] = "environment_or_execution_error"
        path.write_text(json.dumps(cached))
    snapshots = {path: path.read_bytes() for path in states}
    summary = campaign.summarize_campaign(root)
    assert all(
        row["official_resolved"] is False and row["score_category"] == "empty_patch" for row in summary["trials"]
    )
    assert summary["known_official_outcomes"] == 2 and summary["test_executed_trials"] == summary["scored_trials"] == 0
    assert summary["complete_known_pairs"] == 1 and summary["complete_pairs"] == 0
    assert summary["analysis_parser_version"]
    assert all(row["cached_score_category_audit"] == "environment_or_execution_error" for row in summary["trials"])
    assert all(path.read_bytes() == body for path, body in snapshots.items())
    # An otherwise valid report is not trusted merely because a cached path points
    # to it: it must be one of the original hash-bound scoring outputs.
    cached = json.loads(states[0].read_text())
    unbound = root / "unbound-report.json"
    unbound.write_bytes(Path(cached["official_report"]).read_bytes())
    cached["official_report"] = str(unbound)
    states[0].write_text(json.dumps(cached))
    with pytest.raises(ValueError, match="not bound"):
        campaign.summarize_campaign(root)
