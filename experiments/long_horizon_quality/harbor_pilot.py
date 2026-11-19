"""Strict three-task, two-arm Harbor pilot for long-horizon quality."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from filelock import FileLock

from experiments.benchmark_live import long_horizon as live
from experiments.benchmark_live.budget import (
    BudgetLedger,
    BudgetStop,
    LIVE_BASE_URL,
    LIVE_MODEL,
)
from experiments.benchmark_live.tasks import oracle_gate, runtime_constraints
from experiments.long_horizon_code import public_pilot
from experiments.long_horizon_quality.analysis import analyze, parse_outcomes
from experiments.long_horizon_quality.protocol import (
    DEFAULT_TRIALS,
    PROTOCOL_ID,
    TERMINAL_BENCH_REVISION,
    freeze_campaign,
    load_plan,
)

REPO = Path(__file__).resolve().parents[2]
CACHE_ROOT = Path("/home/mll/.cache/my-agent-bench/long-horizon-20261113")
TASKS_ROOT = CACHE_ROOT / "tb21-source/tasks"
MANIFEST = CACHE_ROOT / "tb21-manifest.json"
ORACLE_MAP = CACHE_ROOT / "oracle-map.json"
HARBOR = Path("/home/mll/.cache/my-agent-bench/venv/bin/harbor")
TASK_MANIFEST = REPO / "docs/experiments/plans/terminal-bench-21-quality-pilot-v1.json"
DELIVERY_ROOT = Path(
    os.environ.get(
        "LHQ_DELIVERY_ROOT",
        str(Path.home() / ".cache/my-agent-bench/long-horizon-quality/pilot-next"),
    )
).resolve()
DEFAULT_OUTPUT = DELIVERY_ROOT / "campaign"
DEFAULT_LEDGER_ROOT = DELIVERY_ROOT / "ledgers"
PILOT_SEED = 20261120
RUNTIME_PROXY = os.environ.get("LHQ_RUNTIME_PROXY", "http://172.19.80.1:17897")
SLOT_SPEND_CEILING_USD = 1.0
PILOT_SPEND_CEILING_USD = 18.0
LANGUSE_BASE_URL = "https://cloud.langfuse.com"
LANGFUSE_ENV_NAMES = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
)
MAX_REQUESTS = 64
MAX_ROUNDS = 3
CONTEXT_BUDGET = 32000
OUTPUT_TOKENS = 4096
SHELL_TIMEOUT = 120
PROVIDER_TIMEOUT_SECONDS = 90


class PilotError(RuntimeError):
    """The frozen quality pilot cannot continue safely."""


def _resolve_campaign_id(value: str | None) -> str:
    campaign_id = value or os.environ.get("LHQ_CAMPAIGN_ID")
    if not campaign_id or not campaign_id.strip():
        raise PilotError(
            "a unique campaign id is required via --campaign-id or LHQ_CAMPAIGN_ID"
        )
    return campaign_id


def _write_json(path: str | Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".quality-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _source_hashes() -> dict[str, str]:
    paths = {
        REPO / "experiments/benchmark_live/budget.py",
        REPO / "experiments/benchmark_live/harbor_agent.py",
        REPO / "experiments/benchmark_live/tasks.py",
        REPO / "experiments/benchmark_live/observation.py",
        REPO / "experiments/long_horizon_quality/analysis.py",
        REPO / "experiments/long_horizon_quality/harbor_pilot.py",
        REPO / "experiments/long_horizon_quality/orchestrator.py",
        REPO / "experiments/long_horizon_quality/protocol.py",
    }
    for root in (
        REPO / "src/my_agent_core",
        REPO / "src/my_agent_llm",
        REPO / "src/my_coding_agent/longhorizon",
    ):
        paths.update(root.rglob("*.py"))
    return {
        path.relative_to(REPO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _bootstrap_bundle(manifest_path: str | Path) -> tuple[dict, str, dict[str, str]]:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise PilotError("runtime bootstrap manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotError("runtime bootstrap manifest is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("name") != "terminal-bench-verifier-network-bootstrap-v3"
        or value.get("provider_request_timeout_seconds") != PROVIDER_TIMEOUT_SECONDS
        or not isinstance(value.get("files"), list)
    ):
        raise PilotError("runtime bootstrap manifest is unsupported")
    root = path.parent
    expected: dict[str, dict] = {}
    for item in value["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise PilotError("runtime bootstrap file entry is invalid")
        relative = item["path"]
        candidate = (root / relative).resolve()
        if candidate == root or root not in candidate.parents:
            raise PilotError("runtime bootstrap path escapes its bundle")
        expected[relative] = item
    actual_paths = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.name != "manifest.json" and (item.is_file() or item.is_symlink())
    }
    if actual_paths != set(expected):
        raise PilotError("runtime bootstrap file inventory changed")
    actual: dict[str, str] = {}
    for relative, item in sorted(expected.items()):
        candidate = root / relative
        if item.get("kind") == "symlink":
            target = candidate.readlink().as_posix()
            if target != item.get("target"):
                raise PilotError(f"runtime bootstrap symlink changed: {relative}")
            actual[relative] = target
        elif item.get("kind") == "file":
            payload = candidate.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != item.get("sha256") or len(payload) != item.get("bytes"):
                raise PilotError(f"runtime bootstrap file changed: {relative}")
            if bool(candidate.stat().st_mode & 0o111) != bool(item.get("executable")):
                raise PilotError(f"runtime bootstrap mode changed: {relative}")
            actual[relative] = digest
        else:
            raise PilotError("runtime bootstrap entry kind is invalid")
    payload = json.dumps(
        [expected[relative] for relative in sorted(expected)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != value.get("tree_sha256"):
        raise PilotError("runtime bootstrap tree hash changed")
    selected = {
        key: value[key]
        for key in (
            "name",
            "apt_mirror",
            "apt_proxy",
            "uv_version",
            "python_version",
            "pytest_version",
            "pytest_ctrf_version",
            "provider_request_timeout_seconds",
            "tree_sha256",
        )
    }
    return selected, live.sha_file(path), actual


def _official_hashes(task_ids: list[str]) -> dict[str, str]:
    proof = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        proof.get("revision") != TERMINAL_BENCH_REVISION
        or proof.get("source")
        != "https://github.com/harbor-framework/terminal-bench-2-1"
    ):
        raise PilotError("official Terminal-Bench identity differs")
    hashes: dict[str, str] = {}
    for name in task_ids:
        task_root = TASKS_ROOT / name
        prefix = f"tasks/{name}/"
        expected = {
            entry["path"][len(prefix) :]: entry
            for entry in proof["entries"]
            if entry["path"].startswith(prefix)
        }
        paths = list(task_root.rglob("*"))
        if (
            not expected
            or task_root.is_symlink()
            or any(path.is_symlink() for path in paths)
        ):
            raise PilotError(f"official task source is missing or contains links: {name}")
        actual = {
            path.relative_to(task_root).as_posix(): path
            for path in paths
            if path.is_file()
        }
        if actual.keys() != expected.keys():
            raise PilotError(f"official file inventory differs: {name}")
        for relative, path in actual.items():
            data = path.read_bytes()
            entry = expected[relative]
            blob = hashlib.sha1(
                f"blob {len(data)}\0".encode() + data
            ).hexdigest()
            sha = hashlib.sha256(data).hexdigest()
            if (
                len(data) != entry["bytes"]
                or blob != entry["git_blob"]
                or sha != entry["sha256"]
            ):
                raise PilotError(f"official task bytes differ: {name}/{relative}")
            hashes[f"{name}/{relative}"] = sha
    return hashes


def _oracle_history(oracle_map: dict, task: str) -> list[str]:
    raw = oracle_map.get(task)
    values = [raw] if isinstance(raw, str) else list(raw or [])
    history = [str(Path(value).resolve()) for value in values]
    if not history or not Path(history[-1]).is_file():
        raise PilotError(f"oracle receipt is missing: {task}")
    return history


def _policy(data: dict) -> dict:
    return {
        key: data[key]
        for key in ("limit_micro_usd", "pricing", "max_requests", "time_cap")
    }


def _new_slot_ledger(
    path: Path,
    *,
    campaign_id: str,
    spend_ceiling_usd: float,
) -> BudgetLedger:
    return BudgetLedger(
        path,
        limit_usd="30",
        max_requests=16,
        time_cap=600,
        spend_ceiling_usd=spend_ceiling_usd,
        campaign_id=campaign_id,
    )


def freeze(
    *,
    output: str | Path = DEFAULT_OUTPUT,
    ledger_root: str | Path = DEFAULT_LEDGER_ROOT,
    task_manifest: str | Path = TASK_MANIFEST,
    campaign_id: str | None = None,
    trials: int = DEFAULT_TRIALS,
    seed: int = PILOT_SEED,
    bootstrap_manifest: str | Path | None = None,
) -> dict:
    campaign_id = _resolve_campaign_id(campaign_id)
    output = Path(output).resolve()
    ledger_root = Path(ledger_root).resolve()
    if output.exists():
        raise PilotError("freeze requires a new campaign directory")
    if not ledger_root.is_absolute():
        raise PilotError("slot ledger root must be absolute")
    ledger_root.mkdir(parents=True, exist_ok=True)
    bootstrap_path = bootstrap_manifest or os.environ.get("LHQ_BOOTSTRAP_MANIFEST")
    if not bootstrap_path:
        raise PilotError("runtime bootstrap manifest is required")
    bootstrap, bootstrap_sha, bootstrap_files = _bootstrap_bundle(bootstrap_path)
    plan = freeze_campaign(
        task_manifest=task_manifest,
        output=output,
        campaign_id=campaign_id,
        stage="pilot",
        trials=trials,
        seed=seed,
    )
    manifest_tasks = [item["id"] for item in plan["tasks"]]
    mapping = json.loads(ORACLE_MAP.read_text(encoding="utf-8"))
    official = _official_hashes(manifest_tasks)
    input_hashes = {
        str(Path(task_manifest).resolve()): live.sha_file(Path(task_manifest).resolve()),
        str(MANIFEST): live.sha_file(MANIFEST),
        str(ORACLE_MAP): live.sha_file(ORACLE_MAP),
    }
    for task in manifest_tasks:
        history = _oracle_history(mapping, task)
        input_hashes[history[-1]] = live.sha_file(history[-1])

    slots = []
    expected_keys = {
        (slot["task_id"], slot["arm"], slot["trial"])
        for slot in plan["slots"]
    }
    for slot in plan["slots"]:
        task = slot["task_id"]
        history = _oracle_history(mapping, task)
        gate = oracle_gate(TASKS_ROOT / task, history[-1])
        constraints = runtime_constraints(TASKS_ROOT / task, require_cpu=True)
        limits = {
            "max_requests": MAX_REQUESTS,
            "max_seconds": constraints["time_cap"],
            "context_budget": CONTEXT_BUDGET,
            "output_tokens": OUTPUT_TOKENS,
            "shell_timeout": SHELL_TIMEOUT,
            "max_rounds": MAX_ROUNDS,
        }
        slot_campaign = f"{campaign_id}-{slot['run_id']}"
        slot_ledger_path = ledger_root / f"{slot['run_id']}.json"
        slot_ledger = _new_slot_ledger(
            slot_ledger_path,
            campaign_id=slot_campaign,
            spend_ceiling_usd=SLOT_SPEND_CEILING_USD,
        )
        slot_book = slot_ledger.snapshot()
        if slot_book["requests"]:
            raise PilotError(f"new slot ledger is not empty: {slot_ledger_path}")
        slots.append(
            {
                **slot,
                "task": task,
                "artifact_projection": "on",
                "compaction_mode": "layered",
                "oracle_history": history,
                "oracle_gate": gate,
                "constraints": constraints,
                "limits": limits,
                "ledger_path": str(slot_ledger_path),
                "ledger_campaign_id": slot_campaign,
                "ledger_policy": _policy(slot_book),
                "ledger_spend_ceiling_usd": SLOT_SPEND_CEILING_USD,
            }
        )
    if {
        (slot["task_id"], slot["arm"], slot["trial"])
        for slot in slots
    } != expected_keys:
        raise PilotError("slot identity changed while attaching Harbor metadata")

    plan.update(
        {
            "job_protocol": "lhq-harbor-pilot-v1",
            "campaign_id": campaign_id,
            "model": LIVE_MODEL,
            "endpoint": LIVE_BASE_URL,
            "runtime_proxy": RUNTIME_PROXY,
            "runtime_bootstrap": {
                **bootstrap,
                "manifest_path": str(Path(bootstrap_path).resolve()),
                "manifest_sha256": bootstrap_sha,
                "files": bootstrap_files,
            },
            "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
            "tasks_root": str(TASKS_ROOT),
            "manifest": str(MANIFEST),
            "oracle_map": str(ORACLE_MAP),
            "ledger_root": str(ledger_root),
            "slot_spend_ceiling_usd": SLOT_SPEND_CEILING_USD,
            "pilot_spend_ceiling_usd": PILOT_SPEND_CEILING_USD,
            "source_hashes": _source_hashes(),
            "official_hashes": official,
            "input_hashes": input_hashes,
            "slots": slots,
            "langfuse": {
                "enabled": True,
                "required": True,
                "stage": "final",
                "base_url": LANGUSE_BASE_URL,
                "public_key_env": "LANGFUSE_PUBLIC_KEY",
                "secret_key_env": "LANGFUSE_SECRET_KEY",
                "close_timeout": 15,
                "env_names": list(LANGFUSE_ENV_NAMES),
                "env_presence": {
                    name: bool(os.environ.get(name)) for name in LANGFUSE_ENV_NAMES
                },
            },
        }
    )
    plan_payload = (
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _write_json(output / "plan.json", json.loads(plan_payload.decode()))
    (output / "plan.sha256").write_text(
        hashlib.sha256(plan_payload).hexdigest() + "\n",
        encoding="ascii",
    )
    return plan


def _ledger_for_slot(slot: dict) -> BudgetLedger:
    return BudgetLedger(
        slot["ledger_path"],
        limit_usd="30",
        max_requests=16,
        time_cap=600,
        spend_ceiling_usd=slot["ledger_spend_ceiling_usd"],
        campaign_id=slot["ledger_campaign_id"],
    )


def _validate_current(plan: dict) -> list[tuple[dict, BudgetLedger, dict]]:
    task_ids = [item["id"] for item in plan["tasks"]]
    if _source_hashes() != plan["source_hashes"]:
        raise PilotError("runtime source changed after freeze")
    frozen_bootstrap = plan.get("runtime_bootstrap") or {}
    bootstrap, bootstrap_sha, bootstrap_files = _bootstrap_bundle(
        frozen_bootstrap.get("manifest_path", "")
    )
    if (
        bootstrap_sha != frozen_bootstrap.get("manifest_sha256")
        or bootstrap_files != frozen_bootstrap.get("files")
        or bootstrap["tree_sha256"] != frozen_bootstrap.get("tree_sha256")
        or plan.get("provider_timeout_seconds") != PROVIDER_TIMEOUT_SECONDS
    ):
        raise PilotError("runtime bootstrap changed after freeze")
    if _official_hashes(task_ids) != plan["official_hashes"]:
        raise PilotError("official Terminal-Bench tasks changed after freeze")
    for path, expected in plan["input_hashes"].items():
        if not Path(path).is_file() or live.sha_file(path) != expected:
            raise PilotError(f"frozen input changed: {path}")
    result: list[tuple[dict, BudgetLedger, dict]] = []
    total_known_cost = 0
    for slot in plan["slots"]:
        ledger = _ledger_for_slot(slot)
        book = ledger.snapshot()
        if _policy(book) != slot["ledger_policy"]:
            raise PilotError(f"slot ledger policy changed: {slot['run_id']}")
        if any(row["status"] == "pending" for row in book["requests"]):
            raise BudgetStop(f"slot ledger has an unsettled request: {slot['run_id']}")
        if any(row["run_id"] != slot["run_id"] for row in book["requests"]):
            raise PilotError(f"slot ledger contains another run: {slot['run_id']}")
        total_known_cost += sum(
            row["cost_micro_usd"] or 0 for row in book["requests"]
        )
        history = slot["oracle_history"]
        current = oracle_gate(TASKS_ROOT / slot["task"], history[-1])
        if current != slot["oracle_gate"]:
            raise PilotError(f"oracle gate changed: {slot['task']}")
        result.append((slot, ledger, book))
    if total_known_cost > int(plan["pilot_spend_ceiling_usd"] * 1_000_000):
        raise BudgetStop("pilot spend ceiling reached")
    return result


def preflight(output: str | Path = DEFAULT_OUTPUT) -> dict:
    output = Path(output).resolve()
    plan = load_plan(output / "plan.json")
    ledgers = _validate_current(plan)
    credential_source = public_pilot.install_live_credential()
    if not public_pilot.proxy_reachable(plan["runtime_proxy"]):
        raise PilotError("scoped runtime proxy is unavailable")
    langfuse = public_pilot.langfuse_status(plan)
    if not langfuse.get("ready"):
        raise PilotError("Langfuse environment is not ready")
    images = sorted({slot["constraints"]["docker_image"] for slot in plan["slots"]})
    transport = {}
    for image in images:
        attempts = []
        selected = None
        for attempt in range(2):
            result = public_pilot.docker_transport_probe(
                image,
                plan["runtime_proxy"],
            )
            attempts.append(result)
            selected = result
            if result.get("status") == "pass":
                break
            if attempt == 0:
                time.sleep(2)
        transport[image] = {
            **(selected or {"status": "blocked"}),
            "attempts": attempts,
        }
    if any(item.get("status") != "pass" for item in transport.values()):
        raise PilotError("Docker transport preflight failed")
    receipt = {
        "status": "ready",
        "plan_sha256": live.sha_file(output / "plan.json"),
        "credential_source": credential_source,
        "proxy_reachable": True,
        "langfuse": langfuse,
        "transport": transport,
        "ledger_requests": sum(len(book["requests"]) for _slot, _ledger, book in ledgers),
        "ledger_shards": len(ledgers),
        "model_requests_started": 0,
        "runtime_bootstrap": plan["runtime_bootstrap"],
        "tasks": [item["id"] for item in plan["tasks"]],
        "slots": len(plan["slots"]),
    }
    _write_json(output / "readiness/preflight.json", receipt)
    return receipt


def _oracle_result(slot: dict) -> str:
    return slot["oracle_history"][-1]


def job_config(plan: dict, slot: dict, directory: Path) -> dict:
    env = public_pilot.proxy_env(plan["runtime_proxy"])
    kwargs = {
        "task_dir": str(TASKS_ROOT / slot["task"]),
        "oracle_result": _oracle_result(slot),
        "ledger_path": slot["ledger_path"],
        "campaign_id": slot["ledger_campaign_id"],
        "run_id": slot["run_id"],
        "spend_ceiling_usd": slot["ledger_spend_ceiling_usd"],
        "artifact_projection": slot["artifact_projection"],
        "compaction_mode": slot["compaction_mode"],
        "orchestration_mode": slot["arm"],
        "experiment_arm": slot["arm"],
        "observation_mode": "langfuse",
        "observation_required": True,
        "observation_base_url": plan["langfuse"]["base_url"],
        "observation_public_key_env": plan["langfuse"]["public_key_env"],
        "observation_secret_key_env": plan["langfuse"]["secret_key_env"],
        "observation_close_timeout": plan["langfuse"]["close_timeout"],
        "provider_timeout": plan["provider_timeout_seconds"],
        **slot["limits"],
    }
    return {
        "job_name": "harbor",
        "jobs_dir": str(directory / "jobs"),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "timeout_multiplier": 1.0,
        "skip_credential_checks": True,
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "override_gpus": 0,
            "env": env,
        },
        "verifier": {"disable": False, "env": env},
        "agents": [
            {
                "name": None,
                "import_path": (
                    "experiments.benchmark_live.harbor_agent:BudgetedHarborAgent"
                ),
                "max_timeout_sec": slot["limits"]["max_seconds"],
                "kwargs": kwargs,
            }
        ],
        "tasks": [{"path": str(TASKS_ROOT / slot["task"])}],
    }


def _usage(ledger: BudgetLedger, run_id: str) -> dict:
    rows = [
        row for row in ledger.snapshot()["requests"] if row["run_id"] == run_id
    ]
    known = bool(rows) and all(row["cost_micro_usd"] is not None for row in rows)
    prompt = sum(row["usage"]["prompt_tokens"] for row in rows) if known else None
    completion = (
        sum(row["usage"]["completion_tokens"] for row in rows) if known else None
    )
    cache_read = (
        sum(row["usage"].get("cache_read_tokens", 0) for row in rows)
        if known
        else None
    )
    cache_write = (
        sum(row["usage"].get("cache_write_tokens", 0) for row in rows)
        if known
        else None
    )
    total = (
        (prompt or 0) + (completion or 0) + (cache_read or 0) + (cache_write or 0)
        if known
        else None
    )
    cache_denominator = (
        (prompt or 0) + (cache_read or 0) + (cache_write or 0)
        if known
        else None
    )
    return {
        "requests": len(rows),
        "usage_complete": known,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": total,
        "cost_micro_usd": (
            sum(row["cost_micro_usd"] for row in rows) if known else None
        ),
        "cache_hit_rate": (
            (cache_read or 0) / cache_denominator
            if cache_denominator
            else None
        ),
    }


def _live_metadata(directory: Path, slot: dict) -> dict:
    paths = list(directory.glob("jobs/**/live_run.json"))
    if len(paths) != 1:
        raise PilotError(
            f"expected one live_run.json for {slot['run_id']}, found {len(paths)}"
        )
    value = json.loads(paths[0].read_text(encoding="utf-8"))
    checks = {
        "run_id": value.get("run_id") == slot["run_id"],
        "arm": value.get("arm") == slot["arm"],
        "orchestration_mode": value.get("orchestration_mode") == slot["arm"],
        "model": value.get("model") == plan_model(slot),
        "observation": (value.get("observation") or {}).get("ready") is True,
    }
    if not all(checks.values()):
        raise PilotError(f"live-run contract mismatch: {slot['run_id']} {checks}")
    return value


def plan_model(slot: dict) -> str:
    return LIVE_MODEL


def _enrich_finished_state(
    state: dict,
    *,
    directory: Path,
    slot: dict,
    ledger: BudgetLedger,
) -> dict:
    if state.get("status") != "finished":
        return state
    state["live_metadata"] = _live_metadata(directory, slot)
    state["usage"] = _usage(ledger, slot["run_id"])
    state["task"] = slot["task"]
    state["arm"] = slot["arm"]
    state["trial"] = slot["trial"]
    return state


def run(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    execute: bool = False,
    max_new_trials: int = 1,
) -> dict:
    output = Path(output).resolve()
    if type(max_new_trials) is not int or not 1 <= max_new_trials <= 18:
        raise PilotError("max_new_trials must be in 1..18")
    with FileLock(str(output / "driver.lock"), timeout=0):
        plan = load_plan(output / "plan.json")
        ledgers = {
            slot["run_id"]: ledger
            for slot, ledger, _book in _validate_current(plan)
        }
        readiness = json.loads(
            (output / "readiness/preflight.json").read_text(encoding="utf-8")
        )
        if readiness.get("status") != "ready":
            raise PilotError("preflight is not ready")
        if not execute:
            return {"preview": True, "slots": plan["slots"]}
        journal_path = output / "journal.json"
        journal = (
            json.loads(journal_path.read_text(encoding="utf-8"))
            if journal_path.exists()
            else {}
        )
        new = 0
        spent_before = sum(
            sum(
                row["cost_micro_usd"] or 0
                for row in ledger.snapshot()["requests"]
            )
            for ledger in ledgers.values()
        )
        for index, slot in enumerate(plan["slots"]):
            run_id = slot["run_id"]
            ledger = ledgers[run_id]
            directory = output / (
                f"{index:02d}-{slot['task']}-{slot['arm']}-t{slot['trial']}"
            )
            state = journal.get(run_id)
            if state is not None:
                if state.get("status") == "started":
                    collected = live.collect(
                        directory,
                        slot["task"],
                        expected_task_name=slot["oracle_gate"]["task_name"],
                    )
                    journal[run_id] = (
                        _enrich_finished_state(
                            collected,
                            directory=directory,
                            slot=slot,
                            ledger=ledger,
                        )
                        if collected is not None
                        else {"status": "interrupted_unknown"}
                    )
                    _write_json(journal_path, journal)
                continue
            if new >= max_new_trials:
                break
            book = ledger.snapshot()
            campaign = book.get("campaigns", {}).get(slot["ledger_campaign_id"])
            if campaign and campaign.get("blocked"):
                journal[run_id] = {
                    "status": "slot_blocked",
                    "reason": "coordinator review required for this slot ledger",
                }
                _write_json(journal_path, journal)
                continue
            if (
                book["runs"].get(run_id, {}).get("trial_started")
                or any(row["run_id"] == run_id for row in book["requests"])
            ):
                journal[run_id] = {"status": "consumed_elsewhere"}
                _write_json(journal_path, journal)
                continue
            public_pilot.install_live_credential()
            if not public_pilot.proxy_reachable(plan["runtime_proxy"]):
                raise PilotError("runtime proxy became unavailable")
            if (
                spent_before
                + int(slot["ledger_spend_ceiling_usd"] * 1_000_000)
                > int(plan["pilot_spend_ceiling_usd"] * 1_000_000)
            ):
                raise BudgetStop("pilot cumulative spend ceiling reached")
            ledger.register_run(
                run_id,
                max_requests=slot["limits"]["max_requests"],
                time_cap=slot["limits"]["max_seconds"],
            )
            directory.mkdir(exist_ok=True)
            config = job_config(plan, slot, directory)
            public_pilot._trial_config_validate(config)  # noqa: SLF001
            _write_json(directory / "job.json", config)
            journal[run_id] = {"status": "started"}
            _write_json(journal_path, journal)
            with ledger.transaction() as data:
                if data["runs"][run_id].get("trial_started"):
                    raise BudgetStop("trial was already started")
                data["runs"][run_id]["trial_started"] = True
            new += 1
            spent_before += int(slot["ledger_spend_ceiling_usd"] * 1_000_000)
            try:
                public_pilot.execute_harbor(
                    config,
                    directory,
                    slot["limits"]["max_seconds"] + 3600,
                )
            except Exception as exc:
                collected = live.collect(
                    directory,
                    slot["task"],
                    expected_task_name=slot["oracle_gate"]["task_name"],
                )
                journal[run_id] = (
                    _enrich_finished_state(
                        collected,
                        directory=directory,
                        slot=slot,
                        ledger=ledger,
                    )
                    if collected is not None
                    else {
                    "status": "infrastructure_error",
                    "error_type": type(exc).__name__,
                    }
                )
            else:
                collected = live.collect(
                    directory,
                    slot["task"],
                    expected_task_name=slot["oracle_gate"]["task_name"],
                )
                journal[run_id] = (
                    _enrich_finished_state(
                        collected,
                        directory=directory,
                        slot=slot,
                        ledger=ledger,
                    )
                    if collected is not None
                    else {"status": "missing_official_result"}
                )
            _write_json(journal_path, journal)
        return summary(output)


def _outcome_rows(plan: dict, journal: dict) -> list[dict]:
    rows = []
    for slot in plan["slots"]:
        task_id = slot["task"] if "task" in slot else slot["task_id"]
        state = journal.get(slot["run_id"])
        usage = _usage(_ledger_for_slot(slot), slot["run_id"])
        if state is None:
            rows.append(
                {
                    "task_id": task_id,
                    "arm": slot["arm"],
                    "trial": slot["trial"],
                    "run_id": slot["run_id"],
                    "status": "not_started",
                    **usage,
                }
            )
            continue
        rows.append(
            {
                "task_id": task_id,
                "arm": slot["arm"],
                "trial": slot["trial"],
                "run_id": slot["run_id"],
                **state,
                "status": (
                    "scored"
                    if state.get("status") == "finished"
                    and state.get("category") == "scored"
                    else (
                        state.get("category") or state.get("status")
                        if state.get("status") == "finished"
                        else state.get("status")
                    )
                ),
                **(state.get("usage") or usage),
            }
        )
    return rows


def summary(output: str | Path = DEFAULT_OUTPUT) -> dict:
    output = Path(output).resolve()
    plan = load_plan(output / "plan.json")
    _validate_current(plan)
    journal = (
        json.loads((output / "journal.json").read_text(encoding="utf-8"))
        if (output / "journal.json").exists()
        else {}
    )
    rows = _outcome_rows(plan, journal)
    complete = len(journal) == len(plan["slots"])
    analysis = None
    if complete:
        try:
            analysis = analyze(parse_outcomes(rows), plan=plan)
        except ValueError as exc:
            analysis = {"error": str(exc)}
    result = {
        "protocol": PROTOCOL_ID,
        "campaign_id": plan["campaign_id"],
        "stage": plan["stage"],
        "claim_ceiling": plan["claim_ceiling"],
        "plan_sha256": live.sha_file(output / "plan.json"),
        "slots": len(plan["slots"]),
        "started": len(journal),
        "complete": complete,
        "categories": dict(Counter(row.get("status") for row in rows)),
        "rows": rows,
        "analysis": analysis,
        "claim_boundaries": [
            "This is a three-task pilot and cannot support a benchmark-quality claim.",
            "Unknown usage or non-scored official results invalidate quality claims.",
            "MEA and react use the same model, context budget, request cap and verifier.",
        ],
    }
    _write_json(output / "summary.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="long-horizon-quality-harbor")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_command = commands.add_parser("freeze")
    freeze_command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    freeze_command.add_argument("--ledger-root", default=str(DEFAULT_LEDGER_ROOT))
    freeze_command.add_argument("--campaign-id")
    preflight_command = commands.add_parser("preflight")
    preflight_command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    run_command = commands.add_parser("run")
    run_command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    run_command.add_argument("--execute", action="store_true")
    run_command.add_argument("--max-new-trials", type=int, default=1)
    summary_command = commands.add_parser("summary")
    summary_command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(
                output=args.output,
                ledger_root=args.ledger_root,
                campaign_id=args.campaign_id,
            )
        elif args.command == "preflight":
            result = preflight(args.output)
        elif args.command == "run":
            result = run(
                args.output,
                execute=args.execute,
                max_new_trials=args.max_new_trials,
            )
        else:
            result = summary(args.output)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (OSError, ValueError, PilotError, BudgetStop) as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
