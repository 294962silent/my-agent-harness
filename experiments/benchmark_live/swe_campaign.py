"""Frozen small SWE campaign; serial solvers, independent official scoring, no auto-retries."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import re
import signal
import sys
import tempfile
from uuid import uuid4, uuid5, NAMESPACE_URL

from filelock import FileLock

from .budget import BudgetLedger

ROOT = Path(__file__).resolve().parents[2]
TASK_FIELDS = {"instance_id", "metadata", "gold_evidence", "evaluation_dataset", "evaluation_cwd"}
OFFICIAL_FILES = (
    "task.yaml",
    "eval.sh",
    "gold.patch",
    "test.patch",
    "tests.json",
    "Dockerfile",
    "problem_statement.md",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".campaign-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _absolute(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("campaign input paths must be absolute")
    return path.resolve()


def _file(path):
    path = _absolute(path)
    if not path.is_file():
        raise ValueError("campaign input file is missing")
    return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}


def _optional_file(path):
    path = _absolute(path)
    return _file(path) if path.is_file() else {"path": str(path), "missing": True}


def _charged(rows):
    return sum(r["cost_micro_usd"] if r["cost_micro_usd"] is not None else r["held_micro_usd"] for r in rows)


def plan_campaign(*, task_inputs, catalog_path, catalog_sha256, ledger_path, output, seed=23, batch_budget_usd=3):
    from .swe_catalog import load_catalog
    from .swe_agent import gated_task

    if seed != 23:
        raise ValueError("this campaign protocol fixes selection/order seed 23")
    batch = Decimal(str(batch_budget_usd))
    if not batch.is_finite() or not 0 < batch <= 3:
        raise ValueError("batch budget must be positive and at most 3 USD")
    catalog = load_catalog(catalog_path, expected_sha256=catalog_sha256)
    entries = _json(task_inputs).get("tasks")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 6:
        raise ValueError("select 1 to 6 fixed official tasks")
    if len({r.get("instance_id") for r in entries}) != len(entries):
        raise ValueError("duplicate selected task")
    ledger_path = _absolute(ledger_path)
    if not ledger_path.is_file():
        raise ValueError("reuse the existing authorized ledger; do not create a new campaign ledger")
    ledger = BudgetLedger(ledger_path).snapshot()
    if any(r["cost_micro_usd"] is None or r["status"] == "pending" for r in ledger["requests"]):
        raise ValueError("existing ledger has unknown usage; reconcile before a new campaign")
    baseline = _charged(ledger["requests"])
    ceiling = min(ledger["limit_micro_usd"], baseline + int(batch * 1_000_000))
    files = [_file(task_inputs), _file(catalog_path)]
    tasks = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != TASK_FIELDS:
            raise ValueError("task inputs require exactly five approved fields")
        task = dict(entry)
        spec = catalog.get(task["instance_id"])
        for field in ("metadata", "gold_evidence"):
            task[field] = str(_absolute(task[field]))
            files.append(_file(task[field]))
        task["evaluation_dataset"] = str(_absolute(task["evaluation_dataset"]))
        evaluation_files = [_optional_file(task["evaluation_dataset"])]
        if sha(task["metadata"]) != spec.metadata_sha256:
            raise ValueError("safe metadata differs from catalog")
        task["evaluation_cwd"] = str(_absolute(task["evaluation_cwd"]))
        candidates = [
            Path(task["evaluation_cwd"]) / "tasks" / task["instance_id"],
            Path(task["evaluation_cwd"]) / "canonical",
        ]
        source = next((p for p in candidates if any((p / n).is_file() for n in OFFICIAL_FILES)), candidates[0])
        evaluation_files.extend(_optional_file(source / name) for name in OFFICIAL_FILES)
        evaluation_files.extend(
            _file(p) for p in sorted(source.rglob("*")) if p.is_file() and p.name not in OFFICIAL_FILES
        )
        task["canonical_path"] = str(source)
        files.extend(evaluation_files)
        evidence = _json(task["gold_evidence"])
        report = evidence.get("official_report_path") or evidence.get("official_report")
        if report is not None:
            files.append(_optional_file(report))
        try:
            gated_task(task["metadata"], task["gold_evidence"], catalog=catalog)
            task.update(eligible=True, blocked_reason=None)
        except (ValueError, KeyError, FileNotFoundError):
            task.update(eligible=False, blocked_reason="gold_gate_rejected")
        if any(r.get("missing") for r in evaluation_files):
            task.update(eligible=False, blocked_reason="evaluation_inputs_missing")
        tasks.append(task)
    # Hash runtime sources as inputs; the growing cost ledger is intentionally separate.
    runtime = list((ROOT / "src").rglob("*.py")) + list(Path(__file__).parent.glob("*.py"))
    files.extend(_file(path) for path in sorted(runtime))
    rng = random.Random(seed)
    rng.shuffle(tasks)
    first = {}
    for eligible in (True, False):
        group = [t for t in tasks if t["eligible"] == eligible]
        arms = ["on"] * ((len(group) + 1) // 2) + ["off"] * (len(group) // 2)
        rng.shuffle(arms)
        first.update(zip((t["instance_id"] for t in group), arms))
    campaign_id = uuid4().hex
    trials = []
    for task in tasks:
        first_arm = first[task["instance_id"]]
        for arm in (first_arm, "off" if first_arm == "on" else "on"):
            ordinal = len(trials) + 1
            identity = f"{ordinal:02d}-{task['instance_id']}-{arm}"
            trials.append(
                {
                    "trial_id": identity,
                    "instance_id": task["instance_id"],
                    "arm": arm,
                    "run_id": uuid5(NAMESPACE_URL, campaign_id + "/" + identity).hex,
                    "score_run_id": f"campaign-{campaign_id[:12]}-{ordinal:02d}",
                }
            )
    plan = {
        "schema_version": 1,
        "adapter_protocol": "v2",
        "campaign_id": campaign_id,
        "catalog_path": str(_absolute(catalog_path)),
        "catalog_sha256": catalog_sha256,
        "task_inputs": str(_absolute(task_inputs)),
        "ledger_path": str(ledger_path),
        "baseline_ledger_sha256": sha(ledger_path),
        "baseline_charged_micro_usd": baseline,
        "spend_ceiling_usd": str(Decimal(ceiling) / 1_000_000),
        "policy": {
            "seed": seed,
            "batch_budget_usd": str(batch),
            "max_requests": 16,
            "max_output_tokens": 4096,
            "max_seconds": 600,
            "temperature": 0.7,
            "model": "deepseek-v4.1-flash",
            "harness_version": "5.0.2",
            "repeats_per_arm": 1,
            "concurrency": 1,
        },
        "tasks": tasks,
        "trials": trials,
        "input_files": files,
    }
    root = _absolute(output)
    root.mkdir(parents=True, exist_ok=False)
    _write(root / "plan.json", plan)
    (root / "plan.sha256").write_text(sha(root / "plan.json") + "\n", encoding="ascii")
    return root


def _load(root):
    root = _absolute(root)
    if sha(root / "plan.json") != (root / "plan.sha256").read_text().strip():
        raise ValueError("frozen plan changed")
    plan = _json(root / "plan.json")
    for row in plan["input_files"]:
        path = Path(row["path"])
        if row.get("missing"):
            if path.is_file():
                raise ValueError("frozen campaign input changed: " + row["path"])
            continue
        if not path.is_file() or path.stat().st_size != row["bytes"] or sha(path) != row["sha256"]:
            raise ValueError("frozen campaign input changed: " + row["path"])
    return root, plan, sha(root / "plan.json")


def _verify_outputs(state):
    for row in state.get("outputs", []):
        path = Path(row["path"])
        if not path.is_file() or sha(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise ValueError("saved trial result changed: " + str(path))


def _outputs(path):
    return [_file(p) for p in sorted(Path(path).rglob("*")) if p.is_file()]


async def _execute(*, trial, plan, output):
    from .swe_catalog import load_catalog
    from .swe_agent import solve

    task = next(t for t in plan["tasks"] if t["instance_id"] == trial["instance_id"])
    catalog = load_catalog(plan["catalog_path"], expected_sha256=plan["catalog_sha256"])
    await solve(
        metadata=task["metadata"],
        gold_evidence=task["gold_evidence"],
        ledger_path=plan["ledger_path"],
        arm=trial["arm"],
        output=output,
        catalog=catalog,
        spend_ceiling_usd=float(plan["spend_ceiling_usd"]),
        run_id=trial["run_id"],
    )


async def run_campaign(root, *, executor=None):
    root, plan, plan_hash = _load(root)
    execute = executor or _execute
    result = {
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "blocked": 0,
        "requires_reconciliation": 0,
        "halted_unknown_usage": False,
    }
    with FileLock(str(root / ".campaign.lock"), timeout=0):
        ledger_rows = BudgetLedger(plan["ledger_path"]).snapshot()["requests"]
        if any(r["cost_micro_usd"] is None for r in ledger_rows):
            result.update(requires_reconciliation=1, halted_unknown_usage=True)
            return result
        for trial in plan["trials"]:
            path = root / "trials" / trial["trial_id"]
            state_path = path / "state.json"
            if state_path.exists():
                state = _json(state_path)
                if state.get("plan_sha256") != plan_hash or state.get("run_id") != trial["run_id"]:
                    raise ValueError("trial identity changed")
                _verify_outputs(state)
                if state["status"] in ("completed", "blocked"):
                    result["skipped"] += 1
                else:
                    result["requires_reconciliation"] += 1
                continue
            if path.exists():
                raise ValueError("untracked trial directory requires reconciliation")
            path.mkdir(parents=True)
            task = next(t for t in plan["tasks"] if t["instance_id"] == trial["instance_id"])
            state = {"plan_sha256": plan_hash, "run_id": trial["run_id"], "status": "started", "outputs": []}
            if not task["eligible"]:
                state.update(status="blocked", error_code=task["blocked_reason"])
                _write(state_path, state)
                result["blocked"] += 1
                continue
            _write(state_path, state)  # Durable identity before any chargeable action.
            try:
                await execute(trial=trial, plan=plan, output=path / "agent")
                run = _json(path / "agent/run.json")
                if run.get("run_id") != trial["run_id"]:
                    raise ValueError("solver run id differs from journal")
                state["status"] = "completed"
                result["completed"] += 1
            except asyncio.CancelledError:
                # Leave durable started state; a rerun must reconcile, never silently retry.
                raise
            except Exception as exc:
                state.update(status="failed", error_type=type(exc).__name__)
                result["failed"] += 1
            state["outputs"] = _outputs(path / "agent")
            _write(state_path, state)
            # Unknown usage blocks the remaining campaign, retaining every reservation.
            rows = BudgetLedger(plan["ledger_path"]).snapshot()["requests"]
            if any(r["cost_micro_usd"] is None for r in rows):
                result["halted_unknown_usage"] = True
                break
    return result


async def _score(*, trial, task, output, prediction):
    if importlib.metadata.version("swebench") != "5.0.2":
        raise ValueError("official scoring requires SWE-bench 5.0.2")
    output.mkdir()
    command = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        task["evaluation_dataset"],
        "--predictions_path",
        str(prediction),
        "--instance_ids",
        trial["instance_id"],
        "--max_workers",
        "1",
        "--timeout",
        "600",
        "--run_id",
        trial["score_run_id"],
        "--report_dir",
        str(output),
    ]
    _write(
        output / "command.json",
        {"argv": command, "cwd": task["evaluation_cwd"], "harness": "swebench", "version": "5.0.2"},
    )
    env = os.environ.copy()
    for name in ("LIVE_BENCH_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    with (output / "score.log").open("x", encoding="utf-8") as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=task["evaluation_cwd"],
            env=env,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            await process.wait()
        except BaseException:
            cleanup = {"process_tree": None, "containers": None, "errors": []}
            try:
                await _stop_score_process(process)
                cleanup["process_tree"] = "reaped"
            except BaseException as exc:
                cleanup["errors"].append("process:" + type(exc).__name__)
            try:
                cleanup["containers"] = await _cleanup_score_containers(trial, env)
            except BaseException as exc:
                cleanup["errors"].append("containers:" + type(exc).__name__)
            _write(output / "cancel-cleanup.json", cleanup)
            raise
    candidates = [p for p in output.glob("*.json") if p.name != "command.json" and "resolved_ids" in _json(p)]
    if len(candidates) != 1:
        raise ValueError("official scoring did not produce one unambiguous report")
    return candidates[0]


async def _stop_score_process(process):
    """Terminate only this invocation's process group; always reap its parent."""
    pid = getattr(process, "pid", None)
    group = os.name == "posix" and pid is not None
    try:
        if group:
            os.killpg(pid, signal.SIGTERM)
        elif os.name == "nt" and pid is not None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), 10)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        if group:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()
    finally:
        if group:
            # Parent exit alone does not prove its descendants exited.
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _docker_output(args, env):
    process = await asyncio.create_subprocess_exec(
        "docker", *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError("owned score container cleanup command failed")
    return out.decode("utf-8", errors="replace").strip()


async def _cleanup_score_containers(trial, env):
    # SWE-bench 5.0.2 run_evaluation.create_container names the instance and run_id,
    # with an optional numeric ghost-container suffix. No other run is selected.
    name = "sweb.eval." + trial["instance_id"].lower() + "." + trial["score_run_id"]
    pattern = re.escape(name) + r"(\.[0-9]+)?"
    ids = await _docker_output(["ps", "-aq", "--filter", "name=^/" + pattern + "$"], env)
    removed = []
    for identity in ids.splitlines():
        if not re.fullmatch(r"[0-9a-f]{12,64}", identity):
            raise ValueError("unexpected Docker container identity")
        actual = await _docker_output(["inspect", "--format", "{{.Name}}", identity], env)
        if not re.fullmatch(pattern, actual.lstrip("/")):
            raise ValueError("score container does not belong to this trial")
        await _docker_output(["rm", "-f", identity], env)
        removed.append(actual.lstrip("/"))
    return removed


async def score_campaign(root, *, scorer=None):
    root, plan, plan_hash = _load(root)
    execute = scorer or _score
    result = {"scored": 0, "skipped": 0, "failed": 0, "requires_reconciliation": 0}
    with FileLock(str(root / ".campaign.lock"), timeout=0):
        for trial in plan["trials"]:
            path = root / "trials" / trial["trial_id"]
            state_path = path / "state.json"
            if not state_path.exists():
                continue
            state = _json(state_path)
            _verify_outputs(state)
            if state["status"] != "completed":
                continue
            prediction = path / "agent/model_patch.jsonl"
            if not prediction.is_file():
                continue
            journal = path / "score-state.json"
            if journal.exists():
                score = _json(journal)
                _verify_outputs(score)
                if score.get("plan_sha256") != plan_hash:
                    raise ValueError("scoring identity changed")
                result["skipped" if score["status"] == "completed" else "requires_reconciliation"] += 1
                continue
            score = {"plan_sha256": plan_hash, "status": "started", "outputs": []}
            _write(journal, score)
            task = next(t for t in plan["tasks"] if t["instance_id"] == trial["instance_id"])
            try:
                report = Path(await execute(trial=trial, task=task, output=path / "score", prediction=prediction))
                if not report.resolve().is_relative_to((path / "score").resolve()):
                    raise ValueError("official report outside trial score directory")
                score.update(status="completed", official_report=str(report), outputs=_outputs(path / "score"))
                result["scored"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                score.update(status="failed", error_type=type(exc).__name__, outputs=_outputs(path / "score"))
                result["failed"] += 1
            _write(journal, score)
    return result


def _artifact_facts(agent, run):
    facts = {"externalizations": None, "artifact_read_responses": None, "artifact_search_responses": None}
    if run.get("adapter_protocol") == "v2":
        facts["externalizations"] = len(list((agent / "artifacts").rglob("*.txt")))
    session = agent / "session.jsonl"
    if session.exists():
        try:
            from my_agent_core.session import Session

            messages = Session.load(session).get_current_path_messages()
            for name in ("artifact_read", "artifact_search"):
                facts[name + "_responses"] = sum(
                    m.role == "tool" and (m.metadata or {}).get("tool_name") == name for m in messages
                )
        except (ValueError, KeyError, OSError):
            pass
    return facts


def summarize_campaign(root):
    root, plan, plan_hash = _load(root)
    rows = []
    completed = {}
    expected = 0
    expected_known = True
    for trial in plan["trials"]:
        path = root / "trials" / trial["trial_id"]
        state_path = path / "state.json"
        row = {**trial, "generation_status": "not_started", "official_resolved": None, "score_category": "unscored"}
        if state_path.exists():
            state = _json(state_path)
            _verify_outputs(state)
            if state.get("plan_sha256") != plan_hash:
                raise ValueError("trial identity changed")
            row["generation_status"] = state["status"]
            if (path / "agent/run.json").exists():
                run = _json(path / "agent/run.json")
                requests = run.get("requests")
                if isinstance(requests, int) and not isinstance(requests, bool) and requests >= 0:
                    expected += requests
                else:
                    expected_known = False
                row.update(
                    model_requests=run.get("requests"),
                    solver_status=run.get("status"),
                    artifacts=_artifact_facts(path / "agent", run),
                )
        journal = path / "score-state.json"
        if journal.exists():
            score = _json(journal)
            _verify_outputs(score)
            if score.get("plan_sha256") != plan_hash:
                raise ValueError("scoring identity changed")
            if score["status"] == "completed":
                report = _json(score["official_report"])
                if trial["instance_id"] in report.get("completed_ids", []) and trial["instance_id"] not in report.get(
                    "error_ids", []
                ):
                    row.update(
                        score_category="scored",
                        official_resolved=trial["instance_id"] in report.get("resolved_ids", []),
                    )
                    completed.setdefault(trial["instance_id"], set()).add(trial["arm"])
                else:
                    row["score_category"] = "environment_or_execution_error"
        rows.append(row)
    ledger = BudgetLedger(plan["ledger_path"]).snapshot()
    ids = {r["run_id"] for r in plan["trials"]}
    campaign_rows = [r for r in ledger["requests"] if r["run_id"] in ids]
    complete = (
        len(campaign_rows) == expected
        and expected_known
        and all(r["cost_micro_usd"] is not None for r in campaign_rows)
        and all(r["generation_status"] in ("completed", "blocked") for r in rows)
    )
    unknown_global = any(r["cost_micro_usd"] is None for r in ledger["requests"])
    return {
        "schema_version": 1,
        "adapter_protocol": "v2",
        "plan_sha256": plan_hash,
        "selected_tasks": len(plan["tasks"]),
        "eligible_tasks": sum(t["eligible"] for t in plan["tasks"]),
        "blocked_tasks": sum(not t["eligible"] for t in plan["tasks"]),
        "attempted_trials": sum(r["generation_status"] not in ("not_started", "blocked") for r in rows),
        "attempted_trials_definition": "execution attempts, including pre-agent zero-request failures",
        "model_attempted_trials": sum(
            isinstance(r.get("model_requests"), int) and r["model_requests"] > 0 for r in rows
        ),
        "scored_trials": sum(r["score_category"] == "scored" for r in rows),
        "resolved_trials": sum(r["official_resolved"] is True for r in rows),
        "complete_pairs": sum(arms == {"on", "off"} for arms in completed.values()),
        "budget": {
            "usage_complete": complete,
            "campaign_cost_usd": sum(r["cost_micro_usd"] for r in campaign_rows) / 1_000_000 if complete else None,
            "global_settled_usd": sum(r["cost_micro_usd"] for r in ledger["requests"]) / 1_000_000
            if not unknown_global
            else None,
            "global_charged_or_held_usd": _charged(ledger["requests"]) / 1_000_000,
            "spend_ceiling_usd": plan["spend_ceiling_usd"],
            "baseline_charged_micro_usd": plan["baseline_charged_micro_usd"],
        },
        "trials": rows,
        "limits": "Small fixed paired sample; unknown usage stays null. No causal artifact conclusion without observed activation.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("plan")
    for name in ("task-inputs", "catalog-path", "catalog-sha256", "ledger-path", "output"):
        create.add_argument("--" + name, required=True)
    create.add_argument("--batch-budget-usd", type=float, default=3)
    for action in ("run", "score", "summarize"):
        sub.add_parser(action).add_argument("--campaign", required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    if action == "plan":
        result = str(plan_campaign(**args))
    elif action == "run":
        result = asyncio.run(run_campaign(args["campaign"]))
    elif action == "score":
        result = asyncio.run(score_campaign(args["campaign"]))
    else:
        result = summarize_campaign(args["campaign"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
