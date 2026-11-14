"""Frozen TB2.1 paired pilot. Dispatch is explicit, serial and never retries a slot."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit

from filelock import FileLock

from .budget import BudgetLedger, BudgetStop, LIVE_BASE_URL, LIVE_MODEL
from .tasks import oracle_gate, parse_trial_result, runtime_constraints

REVISION = "12b7565679ce5006230b820666353673a133db93"
SOURCE = "https://github.com/harbor-framework/terminal-bench-2-1"
TASKS = ("cancel-async-tasks", "large-scale-text-editing", "log-summary-date-ranges",
         "git-multibranch", "fix-git", "regex-log", "db-wal-recovery", "custom-memory-heap-crash")
REPO = Path(__file__).resolve().parents[2]
HARBOR = "/home/mll/.cache/my-agent-bench/venv/bin/harbor"


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pilot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_hashes():
    files = [*REPO.glob("src/**/*.py"), *REPO.glob("experiments/benchmark_live/*.py")]
    return {str(p.relative_to(REPO).as_posix()): sha_file(p) for p in sorted(files)}


def official_hashes(root, manifest):
    proof = json.loads(Path(manifest).read_text())
    if proof.get("revision") != REVISION or proof.get("source") != SOURCE:
        raise ValueError("official source/revision mismatch")
    hashes = {}
    for name in TASKS:
        task = Path(root) / name
        expected = {e["path"][len(f"tasks/{name}/"):]: e for e in proof["entries"]
                    if e["path"].startswith(f"tasks/{name}/")}
        paths = list(task.rglob("*"))
        if not expected or task.is_symlink() or any(p.is_symlink() for p in paths):
            raise ValueError("missing official files or source symlink")
        actual = {p.relative_to(task).as_posix(): p for p in paths if p.is_file()}
        if actual.keys() != expected.keys():
            raise ValueError("official file inventory differs")
        for relative, path in actual.items():
            data, entry = path.read_bytes(), expected[relative]
            blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            sha = hashlib.sha256(data).hexdigest()
            if len(data) != entry["bytes"] or blob != entry["git_blob"] or sha != entry["sha256"]:
                raise ValueError("official file bytes differ")
            hashes[f"{name}/{relative}"] = sha
    return hashes


def policy(data):
    return {key: data[key] for key in ("limit_micro_usd", "pricing", "max_requests", "time_cap")}


def freeze(output, *, tasks_root, manifest, oracle_map, ledger, campaign_id, spend_ceiling_usd,
           runtime_proxy=None):
    output, tasks_root = Path(output).resolve(), Path(tasks_root).resolve()
    ledger, manifest, oracle_map = Path(ledger), Path(manifest).resolve(), Path(oracle_map).resolve()
    if not ledger.is_absolute() or not ledger.is_file():
        raise ValueError("existing absolute shared ledger required")
    if output.exists():
        raise ValueError("freeze requires a new output directory")
    if runtime_proxy:
        parsed = urlsplit(runtime_proxy)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("runtime proxy must be an unauthenticated HTTP(S) origin")
    book = BudgetLedger(ledger, campaign_id=campaign_id, spend_ceiling_usd=spend_ceiling_usd).snapshot()
    official = official_hashes(tasks_root, manifest)
    mapping = json.loads(oracle_map.read_text())
    starts = ["on"] * 4 + ["off"] * 4
    random.Random(23).shuffle(starts)
    slots, oracle_inputs = [], {}
    for name, first in zip(TASKS, starts, strict=True):
        history = mapping.get(name, [])
        history = [history] if isinstance(history, str) else history
        history = [str(Path(p).resolve()) for p in history]
        blocked, gate = None, None
        for path in history:
            if Path(path).is_file():
                oracle_inputs[path] = sha_file(path)
        if not history or not Path(history[-1]).is_file():
            blocked = "oracle_missing"
        else:
            try:
                gate = oracle_gate(tasks_root / name, history[-1])
            except ValueError:
                blocked = "oracle_failed"
        constraints = runtime_constraints(tasks_root / name, require_cpu=True)
        for arm in (first, "off" if first == "on" else "on"):
            slots.append({"task": name, "arm": arm,
                          "run_id": "tb21-" + digest([campaign_id, REVISION, name, arm])[:32],
                          "blocked": blocked, "oracle_history": history, "oracle_gate": gate,
                          "constraints": constraints,
                          "limits": {"max_requests": 64, "max_seconds": constraints["time_cap"],
                                     "context_budget": 32000, "output_tokens": 4096, "shell_timeout": 120}})
    plan = {"protocol": "tb21-long-pilot-v1", "revision": REVISION, "source": SOURCE, "seed": 23,
            "campaign_id": campaign_id, "spend_ceiling_usd": spend_ceiling_usd,
            "tasks_root": str(tasks_root), "manifest": str(manifest), "oracle_map": str(oracle_map),
            "ledger": str(ledger.resolve()), "ledger_policy": policy(book),
            "ledger_prefix_count": len(book["requests"]), "ledger_prefix_sha256": digest(book["requests"]),
            "historical_runs": book["runs"],
            "source_hashes": source_hashes(), "official_hashes": official,
            "input_hashes": {str(manifest): sha_file(manifest), str(oracle_map): sha_file(oracle_map), **oracle_inputs},
            "model": LIVE_MODEL, "endpoint": LIVE_BASE_URL, "slots": slots}
    plan["runtime_proxy"] = runtime_proxy
    plan["runtime_proxy_policy"] = "https_only_http_direct"
    output.mkdir(parents=True)
    write_json(output / "plan.json", plan)
    (output / "plan.sha256").write_text(sha_file(output / "plan.json") + "\n")
    return plan


def preflight(output, expected_sha):
    output = Path(output)
    if sha_file(output / "plan.json") != expected_sha:
        raise ValueError("explicit plan SHA mismatch")
    plan = json.loads((output / "plan.json").read_text())
    if source_hashes() != plan["source_hashes"]:
        raise ValueError("source changed after freeze")
    if any(not Path(p).is_file() or sha_file(p) != sha for p, sha in plan["input_hashes"].items()):
        raise ValueError("frozen oracle/manifest input changed")
    if official_hashes(plan["tasks_root"], plan["manifest"]) != plan["official_hashes"]:
        raise ValueError("official tasks changed")
    if not Path(plan["ledger"]).is_file():
        raise ValueError("shared ledger missing")
    book = BudgetLedger(plan["ledger"], campaign_id=plan["campaign_id"],
                        spend_ceiling_usd=plan["spend_ceiling_usd"])
    data = book.snapshot()
    count = plan["ledger_prefix_count"]
    if (policy(data) != plan["ledger_policy"] or len(data["requests"]) < count
            or digest(data["requests"][:count]) != plan["ledger_prefix_sha256"]
            or any(data["runs"].get(k) != v for k, v in plan["historical_runs"].items())):
        raise ValueError("shared ledger historical prefix/policy mutated")
    campaigns = data.get("campaigns", {})
    registered = {k for k, v in data["runs"].items() if v.get("campaign_id")}
    if any(c["blocked"] for c in campaigns.values()) or any(
            r["status"] == "pending" and r["run_id"] in registered for r in data["requests"]):
        raise BudgetStop("registered campaign blocked or unsettled")
    return plan, book


def job_config(plan, slot, directory):
    proxy = plan.get("runtime_proxy")
    env = {"HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": "", "http_proxy": ""} if proxy else {}
    return {"job_name": "harbor", "jobs_dir": str(directory / "jobs"),
            "n_attempts": 1, "n_concurrent_trials": 1, "retry": {"max_retries": 0},
            "timeout_multiplier": 1.0, "skip_credential_checks": True,
            "environment": {"type": "docker", "force_build": False, "delete": True, "override_gpus": 0,
                            "env": env},
            "verifier": {"disable": False, "env": env},
            "agents": [{"name": None,
                        "import_path": "experiments.benchmark_live.harbor_agent:BudgetedHarborAgent",
                        "max_timeout_sec": slot["limits"]["max_seconds"],
                        "kwargs": {"task_dir": str(Path(plan["tasks_root"]) / slot["task"]),
                                   "oracle_result": slot["oracle_history"][-1], "ledger_path": plan["ledger"],
                                   "campaign_id": plan["campaign_id"], "run_id": slot["run_id"],
                                   "spend_ceiling_usd": plan["spend_ceiling_usd"],
                                   "artifact_projection": slot["arm"], **slot["limits"]}}],
            "tasks": [{"path": str(Path(plan["tasks_root"]) / slot["task"])}]}


def execute_harbor(config, directory, timeout):
    if os.name != "posix":
        raise ValueError("execute this driver inside prepared WSL")
    path = directory / "job.json"
    write_json(path, config)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="2", LITELLM_LOCAL_MODEL_COST_MAP="True", PYTHONPATH="src:.")
    with (directory / "harbor.log").open("w") as log:
        process = subprocess.Popen([HARBOR, "run", "--config", str(path)], cwd=REPO, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        finally:
            # Clean the whole process group even when the driver is interrupted.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()


def verifier_diagnostic(result_path, task):
    """Flag narrow verifier-bootstrap evidence without changing official rewards.

    All eight tasks at REVISION explicitly use --ctrf /logs/verifier/ctrf.json
    in their original tests/test.sh. Only Harbor's two verifier logs are read,
    at most 256 KiB each. Oversized or linked evidence cannot prove absence of
    test execution and therefore never triggers this diagnostic.
    """
    verifier = Path(result_path).parent / "verifier"
    report = verifier / "ctrf.json"
    expected = task in TASKS
    evidence, texts, complete = [], [], True
    for name in ("test-stdout.txt", "test-stderr.txt", "ctrf.json"):
        path = verifier / name
        if not path.exists():
            continue
        size = path.stat().st_size
        safe = not verifier.is_symlink() and not path.is_symlink() and path.is_file() and size <= 256 * 1024
        proof = {"path": str(path), "bytes": size, "sha256": None, "inspected": safe}
        if safe:
            with path.open("rb") as stream:
                data = stream.read(256 * 1024 + 1)
            safe = len(data) <= 256 * 1024
            proof["inspected"] = safe
            if safe:
                proof["sha256"] = hashlib.sha256(data).hexdigest()
                if name != "ctrf.json":
                    texts.append(data.decode("utf-8", errors="replace"))
        if not safe:
            complete = False
        evidence.append(proof)
    text = "\n".join(texts)
    missing_uvx = bool(re.search(
        r"^(?:/tests/test\.sh|/test\.sh|test\.sh): line \d+: uvx: command not found\s*$",
        text, re.MULTILINE,
    ))
    installer_failed = bool(re.search(
        r"^ERROR:?\s+command failed:\s+downloader\s+https://github\.com/astral-sh/uv/releases/download/[^\s]+\s*$",
        text, re.MULTILINE,
    ))
    test_execution = bool(re.search(
        r"^(?:=+\s*test session starts\s*=+|collected \d+ items?|(?:_+\s*)?ERROR collecting\b"
        r"|=+.*(?:FAILURES|ERRORS|short test summary info)|\s*\d+ (?:failed|passed|error)(?:\b|,))",
        text, re.MULTILINE,
    ))
    suspected = bool(expected and not report.exists() and complete
                     and (missing_uvx or installer_failed) and not test_execution)
    return {"suspected_verifier_infra": suspected, "verifier_diagnostic": {
        "protocol": "tb21-uv-verifier-v1", "expected_ctrf": expected,
        "expected_report": str(report), "report_present": report.exists(),
        "anchored_uvx_missing": missing_uvx, "anchored_uv_installer_failed": installer_failed,
        "test_execution_observed": test_execution, "evidence_complete": complete, "evidence": evidence,
    }}


def collect(directory, task, *, expected_task_name=None):
    allowed = {task, f"terminal-bench/{task}"}
    if expected_task_name is not None and expected_task_name not in allowed:
        raise ValueError("frozen oracle task identity is outside the selected TB task")
    results = []
    for path in directory.glob("jobs/**/result.json"):
        data = json.loads(path.read_text())
        if data.get("task_name") in allowed:
            results.append(path)
    if len(results) > 1:
        raise ValueError("multiple official trial results; do not choose a favorable result")
    if results:
        return {"status": "finished", "result_path": str(results[0]), **parse_trial_result(results[0]),
                **verifier_diagnostic(results[0], task)}
    return None


def run(output, *, expected_sha, execute=False, max_new_trials=2, executor=None):
    if type(max_new_trials) is not int or not 1 <= max_new_trials <= 16:
        raise ValueError("max_new_trials must be 1..16")
    output = Path(output).resolve()
    with FileLock(str(output / "driver.lock"), timeout=0):
        plan, ledger = preflight(output, expected_sha)
        if not execute:
            return {"preview": True, "slots": plan["slots"]}
        # Also serialize independent output folders sharing the same campaign.
        with FileLock(plan["ledger"] + ".driver.lock", timeout=0):
            journal_path = output / "journal.json"
            journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
            new = 0
            for index, slot in enumerate(plan["slots"]):
                run_id = slot["run_id"]
                directory = output / f"{index:02d}-{slot['task']}-{slot['arm']}"
                state = journal.get(run_id)
                expected_name = (slot.get("oracle_gate") or {}).get("task_name")
                if state:
                    if state["status"] == "started":
                        journal[run_id] = collect(directory, slot["task"], expected_task_name=expected_name) or {"status": "interrupted_unknown"}
                        write_json(journal_path, journal)
                    continue
                if slot["blocked"]:
                    journal[run_id] = {"status": "blocked", "reason": slot["blocked"]}
                elif (ledger.snapshot()["runs"].get(run_id, {}).get("trial_started")
                      or any(r["run_id"] == run_id for r in ledger.snapshot()["requests"])):
                    journal[run_id] = {"status": "consumed_elsewhere"}
                else:
                    if new >= max_new_trials:
                        break
                    preflight(output, expected_sha)
                    ledger.register_run(run_id, max_requests=64, time_cap=slot["limits"]["max_seconds"])
                    directory.mkdir(exist_ok=True)
                    config = job_config(plan, slot, directory)
                    write_json(directory / "job.json", config)
                    journal[run_id] = {"status": "started"}
                    write_json(journal_path, journal)
                    with ledger.transaction() as data:
                        if data["runs"][run_id].get("trial_started"):
                            raise BudgetStop("trial already started in shared ledger")
                        data["runs"][run_id]["trial_started"] = True
                    new += 1
                    try:
                        # Agent bound plus ample official build/setup/verifier overhead.
                        (executor or execute_harbor)(config, directory, slot["limits"]["max_seconds"] + 3600)
                    except Exception as exc:
                        journal[run_id] = collect(directory, slot["task"], expected_task_name=expected_name) or {
                            "status": "infrastructure_error", "error_type": type(exc).__name__}
                    else:
                        journal[run_id] = collect(directory, slot["task"], expected_task_name=expected_name) or {"status": "missing_official_result"}
                write_json(journal_path, journal)
            return summary(output)


def summary(output):
    output = Path(output)
    plan = json.loads((output / "plan.json").read_text())
    journal = json.loads((output / "journal.json").read_text()) if (output / "journal.json").exists() else {}
    book = BudgetLedger(plan["ledger"]).snapshot()
    rows, pairs = [], []
    for slot in plan["slots"]:
        requests = [r for r in book["requests"] if r["run_id"] == slot["run_id"]]
        known = bool(requests) and all(r["cost_micro_usd"] is not None for r in requests)
        state = journal.get(slot["run_id"], {"status": "not_started"})
        metadata = []
        for path in output.glob(f"*-{slot['task']}-{slot['arm']}/**/live_run.json"):
            metadata.append(json.loads(path.read_text()))
        rows.append({"task": slot["task"], "arm": slot["arm"], **state, "requests": len(requests),
                     "actual_cost_usd": sum(r["cost_micro_usd"] for r in requests) / 1e6 if known else None,
                     "usage_complete": known, "cache_tokens": sum(r["usage"]["cache_read_tokens"] for r in requests)
                     if known and all("cache_read_tokens" in r["usage"] for r in requests) else None,
                     "schema_sha256": metadata[0].get("schema_sha256") if len(metadata) == 1 else None})
    for name in TASKS:
        pair = [r for r in rows if r["task"] == name]
        complete = all(r.get("category") == "scored" for r in pair)
        schemas = [r["schema_sha256"] for r in pair]
        consistent = schemas[0] == schemas[1] if all(schemas) else None
        suspected = any(r.get("suspected_verifier_infra", False) for r in pair)
        valid = complete and consistent is True and not suspected
        pairs.append({"task": name, "complete_scored_pair": complete,
                      "schema_consistent": consistent,
                      "suspected_verifier_infra": suspected,
                      "valid_pair": valid,
                      "on_minus_off": next(r["official_reward"] for r in pair if r["arm"] == "on")
                      - next(r["official_reward"] for r in pair if r["arm"] == "off")
                      if valid else None})
    result = {"protocol": plan["protocol"], "rows": rows, "pairs": pairs,
              "complete_scored_pairs": sum(p["complete_scored_pair"] for p in pairs),
              "complete_pairs": sum(p["valid_pair"] for p in pairs),
              "suspected_verifier_infra_slots": sum(bool(r.get("suspected_verifier_infra")) for r in rows),
              "categories": dict(Counter(r.get("category", r["status"]) for r in rows))}
    write_json(output / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    frozen = sub.add_parser("freeze")
    for name in ("output", "tasks-root", "manifest", "oracle-map", "ledger", "campaign-id"):
        frozen.add_argument("--" + name, required=True)
    frozen.add_argument("--spend-ceiling-usd", type=float, required=True)
    frozen.add_argument("--runtime-proxy", help="Unauthenticated proxy for HTTPS only; HTTP stays direct; frozen for both arms")
    dispatch = sub.add_parser("run")
    dispatch.add_argument("--output", required=True)
    dispatch.add_argument("--expected-sha", required=True)
    dispatch.add_argument("--execute", action="store_true")
    dispatch.add_argument("--max-new-trials", type=int, default=2)
    report = sub.add_parser("summary")
    report.add_argument("--output", required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = {"freeze": freeze, "run": run, "summary": summary}[command](**args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
