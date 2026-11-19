"""Strict three-task, three-arm public long-horizon pilot driver.

The CLI separates zero-model preparation from paid execution. ``run`` is a
dry preview unless ``--execute`` is supplied. No command in freeze, preflight,
oracle, transport, or summary constructs an application LLM.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit

from filelock import FileLock

from experiments.benchmark_live.budget import BudgetLedger, BudgetStop, LIVE_BASE_URL, LIVE_MODEL
from experiments.benchmark_live.long_horizon import collect
from experiments.benchmark_live.tasks import oracle_gate, runtime_constraints


REPO = Path(__file__).resolve().parents[2]
SOURCE = "https://github.com/harbor-framework/terminal-bench-2-1"
REVISION = "12b7565679ce5006230b820666353673a133db93"
CACHE_ROOT = Path("/home/mll/.cache/my-agent-bench/long-horizon-20261113")
TASKS_ROOT = CACHE_ROOT / "tb21-source/tasks"
MANIFEST = CACHE_ROOT / "tb21-manifest.json"
ORACLE_MAP = CACHE_ROOT / "oracle-map.json"
ORACLE_CONFIGS = CACHE_ROOT / "oracle-https-configs"
HARBOR = Path("/home/mll/.cache/my-agent-bench/venv/bin/harbor")
DELIVERY_ROOT = Path(
    "/mnt/c/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/long-horizon-code-20261115-v3"
)
DEFAULT_OUTPUT = DELIVERY_ROOT / "campaign"
DEFAULT_LEDGER = DELIVERY_ROOT / "budget.json"
WINDOWS_LEDGER = (
    r"C:\Users\MLL\Desktop\my-agent-harness\.git\codex-delivery"
    r"\long-horizon-code-20261115-v3\budget.json"
)
PROXY_HELPER = Path(
    "/mnt/c/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/bench-proxy.py"
)
DEFAULT_RUNTIME_PROXY = "http://172.19.80.1:17897"
CAMPAIGN_ID = "lhpp-20261115-v3"
LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
LANGFUSE_ENV_NAMES = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
)
SPEND_CEILING_USD = 30.0
SET_ASIDE_SECONDS = 120
MAX_REQUESTS = 64
CONTEXT_BUDGET = 32000
OUTPUT_TOKENS = 4096
SHELL_TIMEOUT = 120

TASKS = (
    "fix-git",
    "large-scale-text-editing",
    "custom-memory-heap-crash",
)
TASK_ROLES = {
    "fix-git": "version-control recovery",
    "large-scale-text-editing": "long-output and context management",
    "custom-memory-heap-crash": "debugging and recovery",
}
ARMS = {
    "baseline": {"artifact_projection": "off", "compaction_mode": "baseline"},
    "enhanced": {"artifact_projection": "on", "compaction_mode": "layered"},
    "loo_artifact": {"artifact_projection": "off", "compaction_mode": "layered"},
}
ORDER = (("fix-git", "baseline"), ("fix-git", "enhanced"), ("fix-git", "loo_artifact"),
         ("large-scale-text-editing", "enhanced"),
         ("large-scale-text-editing", "loo_artifact"),
         ("large-scale-text-editing", "baseline"),
         ("custom-memory-heap-crash", "loo_artifact"),
         ("custom-memory-heap-crash", "baseline"),
         ("custom-memory-heap-crash", "enhanced"))
EXPECTED_RUN_IDS = {
    ("fix-git", "baseline"): "lhpp-b95d1455eda27d1ace406a62ae00f31c",
    ("fix-git", "enhanced"): "lhpp-a085b1f71aceadf15a03afe22c378146",
    ("fix-git", "loo_artifact"): "lhpp-1e48d21ee7df3ead287578d5b17a2db0",
    ("large-scale-text-editing", "baseline"): "lhpp-e85eb04e9f889fe3f4b18d29fbaf240c",
    ("large-scale-text-editing", "enhanced"): "lhpp-11948d52e39e061f4836e533d001670a",
    ("large-scale-text-editing", "loo_artifact"): "lhpp-43122717991033891519a3d63ffb5cd2",
    ("custom-memory-heap-crash", "baseline"): "lhpp-e816c19a5a84831b3cf3104519628e69",
    ("custom-memory-heap-crash", "enhanced"): "lhpp-8e4ed9ffb4df7e8fd6da9a390b5b10bd",
    ("custom-memory-heap-crash", "loo_artifact"): "lhpp-f49f3a45da67234cff98a2c0948c3d39",
}


class PilotStop(RuntimeError):
    """A frozen pilot invariant or execution gate failed."""


def sha_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: str | Path, value, *, replace: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not replace:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pilot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_sealed(path: str | Path, value) -> str:
    path = Path(path)
    write_json(path, value, replace=True)
    receipt_sha = sha_file(path)
    write_json(path.with_suffix(path.suffix + ".sha256.json"), {"sha256": receipt_sha}, replace=True)
    return receipt_sha


def read_sealed(path: str | Path):
    path = Path(path)
    seal = read_json(path.with_suffix(path.suffix + ".sha256.json"))
    if set(seal) != {"sha256"} or sha_file(path) != seal["sha256"]:
        raise PilotStop(f"receipt hash mismatch: {path}")
    return read_json(path)


def _runtime_sources() -> tuple[Path, ...]:
    paths = [
        *(REPO / "src").glob("**/*.py"),
        REPO / "experiments/benchmark_live/budget.py",
        REPO / "experiments/benchmark_live/harbor_agent.py",
        REPO / "experiments/benchmark_live/long_horizon.py",
        REPO / "experiments/benchmark_live/tasks.py",
        REPO / "experiments/long_horizon_code/public_pilot.py",
        REPO / "pyproject.toml",
        REPO / "uv.lock",
    ]
    return tuple(sorted({path.resolve() for path in paths if path.is_file()}))


def source_hashes() -> dict[str, str]:
    return {path.relative_to(REPO).as_posix(): sha_file(path) for path in _runtime_sources()}


def official_hashes(tasks_root: str | Path, manifest: str | Path) -> dict[str, str]:
    proof = read_json(manifest)
    if proof.get("revision") != REVISION or proof.get("source") != SOURCE:
        raise PilotStop("official source/revision mismatch")
    hashes = {}
    for name in TASKS:
        task = Path(tasks_root) / name
        expected = {
            entry["path"][len(f"tasks/{name}/"):]: entry
            for entry in proof["entries"]
            if entry["path"].startswith(f"tasks/{name}/")
        }
        paths = list(task.rglob("*"))
        if not expected or task.is_symlink() or any(path.is_symlink() for path in paths):
            raise PilotStop(f"missing or linked task files: {name}")
        actual = {path.relative_to(task).as_posix(): path for path in paths if path.is_file()}
        if actual.keys() != expected.keys():
            raise PilotStop(f"official file inventory differs: {name}")
        for relative, path in actual.items():
            data, entry = path.read_bytes(), expected[relative]
            blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if (len(data), blob, sha_file(path)) != (
                entry["bytes"], entry["git_blob"], entry["sha256"]
            ):
                raise PilotStop(f"official bytes differ: {name}/{relative}")
        hashes[name] = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return hashes


def ledger_policy(data: dict) -> dict:
    return {key: data[key] for key in ("limit_micro_usd", "pricing", "max_requests", "time_cap")}


def ledger_run_definition(run: dict) -> dict:
    return {key: run.get(key) for key in ("campaign_id", "limits", "blocked")}


def image_info(image: str) -> dict:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .RepoTags}}|{{.Id}}|{{.Size}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PilotStop(f"required local image is unavailable: {image}")
    tags, image_id, size = result.stdout.strip().split("|")
    return {"tag": image, "tags": json.loads(tags), "id": image_id, "size_bytes": int(size)}


def run_id_for(task: str, arm: str) -> str:
    return "lhpp-" + digest([CAMPAIGN_ID, REVISION, task, arm])[:32]


def _oracle_history(task: str, oracle_map: str | Path) -> list[str]:
    value = read_json(oracle_map).get(task, [])
    history = [value] if isinstance(value, str) else value
    return [str(Path(path).resolve()) for path in history]


def freeze(
    output: str | Path,
    *,
    tasks_root: str | Path = TASKS_ROOT,
    manifest: str | Path = MANIFEST,
    oracle_map: str | Path = ORACLE_MAP,
    ledger: str | Path = DEFAULT_LEDGER,
    campaign_id: str = CAMPAIGN_ID,
    spend_ceiling_usd: float = SPEND_CEILING_USD,
    runtime_proxy: str = DEFAULT_RUNTIME_PROXY,
    image_inspector=image_info,
) -> dict:
    if campaign_id != CAMPAIGN_ID or spend_ceiling_usd != SPEND_CEILING_USD:
        raise PilotStop("campaign identity or $30 ceiling differs from the frozen protocol")
    output, tasks_root = Path(output).resolve(), Path(tasks_root).resolve()
    manifest, oracle_map = Path(manifest).resolve(), Path(oracle_map).resolve()
    ledger = Path(ledger)
    if not ledger.is_absolute():
        raise PilotStop("ledger path must be absolute")
    if output.exists():
        raise PilotStop("freeze requires a new output directory")
    parsed = urlsplit(runtime_proxy)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise PilotStop("runtime proxy must be an unauthenticated HTTP(S) origin")
    if not PROXY_HELPER.is_file():
        raise PilotStop("scoped proxy helper is missing")

    official = official_hashes(tasks_root, manifest)
    book = BudgetLedger(
        ledger,
        limit_usd="30",
        max_requests=16,
        time_cap=600,
        spend_ceiling_usd=spend_ceiling_usd,
        campaign_id=campaign_id,
    )
    prefix = book.snapshot()
    gates = {}
    oracle_inputs = {}
    images = {}
    constraints = {}
    for task in TASKS:
        history = _oracle_history(task, oracle_map)
        oracle_inputs.update({path: sha_file(path) for path in history if Path(path).is_file()})
        constraints[task] = runtime_constraints(tasks_root / task, require_cpu=True)
        images[task] = image_inspector(constraints[task]["docker_image"])
        gates[task] = None
        if history and Path(history[-1]).is_file():
            try:
                gates[task] = oracle_gate(tasks_root / task, history[-1])
            except ValueError:
                gates[task] = None

    slots = []
    for task, arm in ORDER:
        limits = {
            "max_requests": MAX_REQUESTS,
            "max_seconds": constraints[task]["time_cap"] - SET_ASIDE_SECONDS,
            "context_budget": CONTEXT_BUDGET,
            "output_tokens": OUTPUT_TOKENS,
            "shell_timeout": SHELL_TIMEOUT,
            "compaction_mode": ARMS[arm]["compaction_mode"],
            "observation_mode": "langfuse",
            "observation_required": True,
        }
        run_id = run_id_for(task, arm)
        if run_id != EXPECTED_RUN_IDS[(task, arm)]:
            raise PilotStop("pre-registered run identity changed")
        history = _oracle_history(task, oracle_map)
        slots.append({
            "task": task,
            "role": TASK_ROLES[task],
            "arm": arm,
            **ARMS[arm],
            "observation_mode": "langfuse",
            "observation_required": True,
            "observation_base_url": LANGFUSE_BASE_URL,
            "observation_stage": "final",
            "run_id": run_id,
            "blocked": None if gates[task] else "oracle_failed",
            "oracle_history": history,
            "oracle_gate": gates[task],
            "constraints": constraints[task],
            "image": images[task],
            "limits": limits,
        })
        book.register_run(run_id, max_requests=limits["max_requests"], time_cap=limits["max_seconds"])

    input_hashes = {
        str(manifest): sha_file(manifest),
        str(oracle_map): sha_file(oracle_map),
        str(PROXY_HELPER): sha_file(PROXY_HELPER),
        **oracle_inputs,
    }
    for task in TASKS:
        path = ORACLE_CONFIGS / f"{task}.json"
        if not path.is_file():
            raise PilotStop(f"oracle config template missing: {task}")
        input_hashes[str(path)] = sha_file(path)
    final_book = book.snapshot()
    plan = {
        "protocol": "long-horizon-public-pilot-v1",
        "revision": REVISION,
        "source": SOURCE,
        "model": LIVE_MODEL,
        "endpoint": LIVE_BASE_URL,
        "campaign_id": campaign_id,
        "spend_ceiling_usd": spend_ceiling_usd,
        "ledger": str(ledger.resolve()),
        "ledger_windows_path": WINDOWS_LEDGER,
        "ledger_reason": "new isolated user-authorized experiment budget; old ledger is not read or modified",
        "ledger_policy": ledger_policy(final_book),
        "ledger_prefix_count": len(prefix["requests"]),
        "ledger_prefix_sha256": digest(prefix["requests"]),
        "ledger_runs": {
            run_id: ledger_run_definition(run) for run_id, run in final_book["runs"].items()
        },
        "tasks_root": str(tasks_root),
        "manifest": str(manifest),
        "oracle_map": str(oracle_map),
        "input_hashes": input_hashes,
        "source_hashes": source_hashes(),
        "official_hashes": official,
        "slot_order": [[task, arm] for task, arm in ORDER],
        "slots": slots,
        "runtime_proxy": runtime_proxy,
        "runtime_proxy_policy": "https_only_http_direct; scoped env only",
        "set_aside_seconds": SET_ASIDE_SECONDS,
        "langfuse": {
            "enabled": True,
            "required": True,
            "base_url": LANGFUSE_BASE_URL,
            "env_names": list(LANGFUSE_ENV_NAMES),
            "public_key_env": "LANGFUSE_PUBLIC_KEY",
            "secret_key_env": "LANGFUSE_SECRET_KEY",
            "base_url_env": "LANGFUSE_BASE_URL",
            "host_env": "LANGFUSE_HOST",
            "close_timeout": 15,
            "stage": "final",
            "env_presence": {
                name: bool(os.environ.get(name)) for name in LANGFUSE_ENV_NAMES
            },
            "remote_readback": False,
        },
    }
    output.mkdir(parents=True)
    write_json(output / "plan.json", plan)
    (output / "plan.sha256").write_text(sha_file(output / "plan.json") + "\n", encoding="ascii")
    return plan


def _load_plan(output: str | Path, expected_sha: str):
    output = Path(output).resolve()
    plan_path = output / "plan.json"
    actual = sha_file(plan_path)
    if actual != expected_sha:
        raise PilotStop("explicit plan SHA mismatch")
    return output, read_json(plan_path)


def validate_plan(output: str | Path, expected_sha: str) -> tuple[Path, dict, BudgetLedger]:
    output, plan = _load_plan(output, expected_sha)
    if source_hashes() != plan["source_hashes"]:
        raise PilotStop("runtime source changed after freeze")
    if any(not Path(path).is_file() or sha_file(path) != sha for path, sha in plan["input_hashes"].items()):
        raise PilotStop("frozen input changed after freeze")
    if official_hashes(plan["tasks_root"], plan["manifest"]) != plan["official_hashes"]:
        raise PilotStop("official tasks changed after freeze")
    if any(image_info(slot["constraints"]["docker_image"])["id"] != slot["image"]["id"]
           for slot in plan["slots"]):
        raise PilotStop("local image identity changed after freeze")
    book = BudgetLedger(
        plan["ledger"],
        limit_usd="30",
        max_requests=16,
        time_cap=600,
        spend_ceiling_usd=plan["spend_ceiling_usd"],
        campaign_id=plan["campaign_id"],
    )
    data = book.snapshot()
    prefix_count = plan["ledger_prefix_count"]
    if (ledger_policy(data) != plan["ledger_policy"]
            or len(data["requests"]) < prefix_count
            or digest(data["requests"][:prefix_count]) != plan["ledger_prefix_sha256"]
            or any(ledger_run_definition(data["runs"].get(run_id, {})) != run
                   for run_id, run in plan["ledger_runs"].items())):
        raise PilotStop("isolated ledger identity changed after freeze")
    if any(campaign["blocked"] for campaign in data.get("campaigns", {}).values()):
        raise BudgetStop("isolated pilot campaign is blocked")
    registered = {run_id for run_id, run in data["runs"].items() if run.get("campaign_id")}
    if any(row["status"] == "pending" and row["run_id"] in registered for row in data["requests"]):
        raise BudgetStop("isolated pilot has an unsettled request")
    return output, plan, book


def install_live_credential() -> str:
    """Install the configured OpenAI credential in memory without logging it."""
    if os.environ.get("LIVE_BENCH_API_KEY"):
        return "live_environment"
    from my_agent_llm.auth import AuthManager
    from my_coding_agent.paths import AgentPaths

    paths = [AgentPaths()]
    windows_legacy = Path("/mnt/c/Users/MLL/.my-pi-agent")
    if windows_legacy.is_dir() and all(path.auth_path != windows_legacy / "auth.json" for path in paths):
        paths.append(AgentPaths(home=windows_legacy))
    credential = None
    for candidate in paths:
        found = AuthManager(auth_path=candidate.auth_path).get_credential("openai")
        if found is not None:
            credential = found
            break
    if credential is None:
        raise PilotStop("production openai credential is unavailable")
    base_url = (getattr(credential, "base_url", None) or "").rstrip("/")
    if base_url != LIVE_BASE_URL.rstrip("/"):
        raise PilotStop("production openai credential endpoint does not match the frozen endpoint")
    resolve_key = getattr(credential, "resolve_key", None)
    key = resolve_key() if callable(resolve_key) else ""
    if not key:
        raise PilotStop("production openai credential resolves to an empty key")
    os.environ["LIVE_BENCH_API_KEY"] = key
    return "production_auth_manager"


def proxy_reachable(proxy: str, timeout: float = 3) -> bool:
    parsed = urlsplit(proxy)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_env(proxy: str) -> dict[str, str]:
    no_proxy = "github.com,release-assets.githubusercontent.com"
    return {
        "HTTPS_PROXY": proxy,
        "https_proxy": proxy,
        "HTTP_PROXY": "",
        "http_proxy": "",
        "ALL_PROXY": "",
        "all_proxy": "",
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def oracle_config(plan: dict, slot: dict, directory: Path) -> dict:
    task = slot["task"]
    config = read_json(ORACLE_CONFIGS / f"{task}.json")
    config["job_name"] = f"lhpp-v2-oracle-{task}-20261115"
    config["jobs_dir"] = str(directory / "jobs")
    config["n_attempts"] = 1
    config["n_concurrent_trials"] = 1
    config["skip_credential_checks"] = True
    config["retry"] = {"max_retries": 0}
    env = proxy_env(plan["runtime_proxy"])
    config["environment"]["env"] = env
    config["environment"]["force_build"] = False
    config["environment"]["delete"] = True
    config["environment"]["override_gpus"] = 0
    config["verifier"]["disable"] = False
    config["verifier"]["env"] = env
    config["agents"] = [{"name": "oracle"}]
    config["tasks"] = [{"path": str(Path(plan["tasks_root"]) / task)}]
    return config


def trial_config(plan: dict, slot: dict, directory: Path, oracle_result: str) -> dict:
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
            "env": proxy_env(plan["runtime_proxy"]),
        },
        "verifier": {"disable": False, "env": proxy_env(plan["runtime_proxy"])},
        "agents": [{
            "name": None,
            "import_path": "experiments.benchmark_live.harbor_agent:BudgetedHarborAgent",
            "max_timeout_sec": slot["limits"]["max_seconds"],
            "kwargs": {
                "task_dir": str(Path(plan["tasks_root"]) / slot["task"]),
                "oracle_result": oracle_result,
                "ledger_path": plan["ledger"],
                "campaign_id": plan["campaign_id"],
                "run_id": slot["run_id"],
                "spend_ceiling_usd": plan["spend_ceiling_usd"],
                "artifact_projection": slot["artifact_projection"],
                "compaction_mode": slot["compaction_mode"],
                "observation_mode": "langfuse",
                "observation_required": True,
                "observation_base_url": plan["langfuse"]["base_url"],
                "observation_public_key_env": plan["langfuse"]["public_key_env"],
                "observation_secret_key_env": plan["langfuse"]["secret_key_env"],
                "observation_close_timeout": plan["langfuse"]["close_timeout"],
                **slot["limits"],
            },
        }],
        "tasks": [{"path": str(Path(plan["tasks_root"]) / slot["task"])}],
    }


def execute_harbor(config: dict, directory: Path, timeout: float) -> int:
    if os.name != "posix":
        raise PilotStop("run Harbor inside WSL")
    config_path = directory / "job.json"
    write_json(config_path, config, replace=True)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="2", LITELLM_LOCAL_MODEL_COST_MAP="True", PYTHONPATH="src:.")
    with (directory / "harbor.log").open("w") as log:
        process = subprocess.Popen(
            [str(HARBOR), "run", "--config", str(config_path)],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PilotStop("Harbor task exceeded its outer timeout") from exc
        finally:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue


def _oracle_config_validate(config: dict) -> None:
    try:
        from harbor.models.job.config import JobConfig
    except ImportError as exc:
        raise PilotStop("Harbor config schema is unavailable") from exc
    JobConfig.model_validate(config)


def _trial_config_validate(config: dict) -> None:
    _oracle_config_validate(config)


def _result_files(directory: Path, expected_name: str) -> list[Path]:
    results = []
    for path in sorted(directory.glob("jobs/**/result.json")):
        try:
            if read_json(path).get("task_name") == expected_name:
                results.append(path)
        except (OSError, ValueError):
            continue
    return results


def _oracle_record(path: Path, slot: dict, return_code: int = 0) -> dict:
    result = read_json(path)
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    expected_name = slot["oracle_gate"]["task_name"]
    ctrf = path.parent / "verifier/ctrf.json"
    if (result.get("task_name") != expected_name
            or (result.get("agent_info") or {}).get("name") != "oracle"
            or result.get("exception_info") is not None
            or reward != 1
            or result.get("task_checksum") != slot["oracle_gate"]["harbor_task_checksum"]
            or not ctrf.is_file()):
        raise PilotStop(f"fresh oracle gate failed: {slot['task']}")
    return {
        "status": "pass",
        "task_name": expected_name,
        "return_code": return_code,
        "result_path": str(path),
        "result_sha256": sha_file(path),
        "task_checksum": result["task_checksum"],
        "reward": reward,
        "ctrf_sha256": sha_file(ctrf),
        "model_requests": 0,
    }


def run_oracles(output: str | Path, *, expected_sha: str, executor=execute_harbor) -> dict:
    output, plan, _ = validate_plan(output, expected_sha)
    if any(slot["blocked"] for slot in plan["slots"]):
        raise PilotStop("frozen slot has missing or invalid existing oracle evidence")
    if not proxy_reachable(plan["runtime_proxy"]):
        raise PilotStop("scoped proxy is unavailable; oracle network verification is blocked")
    root = output / "oracles"
    already = output / "readiness/oracle.json"
    if already.exists():
        prior = read_sealed(already)
        if prior.get("plan_sha256") == expected_sha and prior.get("status") == "pass":
            return prior
    records = {}
    failed_task = None
    try:
        for task in TASKS:
            failed_task = task
            directory = root / task
            slot = next(slot for slot in plan["slots"] if slot["task"] == task)
            expected_name = slot["oracle_gate"]["task_name"]
            existing = _result_files(directory, expected_name) if directory.exists() else []
            if len(existing) == 1:
                records[task] = _oracle_record(existing[0], slot)
                continue
            if len(existing) > 1 or (directory.exists() and any(directory.iterdir())):
                raise PilotStop(f"oracle output exists without one valid official result: {task}")
            directory.mkdir(parents=True)
            config = oracle_config(plan, slot, directory)
            _oracle_config_validate(config)
            write_json(directory / "job.json", config)
            installed_key = os.environ.pop("LIVE_BENCH_API_KEY", None)
            try:
                return_code = executor(config, directory, slot["constraints"]["time_cap"] + 900)
            finally:
                if installed_key is not None:
                    os.environ["LIVE_BENCH_API_KEY"] = installed_key
            results = _result_files(directory, expected_name)
            if len(results) != 1:
                raise PilotStop(f"oracle did not produce exactly one official result: {task}")
            records[task] = _oracle_record(results[0], slot, return_code)
    except Exception as exc:
        blocked = {
            "protocol": "long-horizon-public-pilot-v1-oracles",
            "status": "blocked",
            "plan_sha256": expected_sha,
            "model_requests": 0,
            "tasks": records,
            "failed_task": failed_task,
            "error_type": type(exc).__name__,
        }
        write_sealed(output / "readiness/oracle.json", blocked)
        raise
    receipt = {
        "protocol": "long-horizon-public-pilot-v1-oracles",
        "status": "pass" if set(records) == set(TASKS) else "blocked",
        "plan_sha256": expected_sha,
        "model_requests": 0,
        "tasks": records,
    }
    write_sealed(output / "readiness/oracle.json", receipt)
    if receipt["status"] != "pass":
        raise PilotStop("fresh oracle gate incomplete")
    return receipt


def docker_transport_probe(image: str, proxy: str, *, timeout: float = 90) -> dict:
    parsed = urlsplit(proxy)
    proxy_address = f"{parsed.hostname}:{parsed.port or 80}"
    code = """
timeout 30 openssl s_client -connect github.com:443 -servername github.com -brief </dev/null >/tmp/github-tls 2>&1
github=$?
if grep -q 'Verification: OK' /tmp/github-tls; then github=0; else github=1; fi
timeout 30 openssl s_client -connect release-assets.githubusercontent.com:443 -servername release-assets.githubusercontent.com -brief </dev/null >/tmp/release-tls 2>&1
release=$?
if grep -q 'Verification: OK' /tmp/release-tls; then release=0; else release=1; fi
printf '{"github_tls":%s,"release_assets_tls":%s}\n' "$github" "$release"
test "$github" -eq 0 && test "$release" -eq 0
"""
    command = [
        "docker", "run", "--rm", "--pull=never", "--network", "bridge", "--entrypoint", "sh",
        "-e", f"HTTPS_PROXY={proxy}", "-e", f"https_proxy={proxy}",
        "-e", "HTTP_PROXY=", "-e", "http_proxy=",
        "-e", "ALL_PROXY=", "-e", "all_proxy=",
        "-e", "NO_PROXY=github.com,release-assets.githubusercontent.com",
        "-e", "no_proxy=github.com,release-assets.githubusercontent.com",
        image, "-c", code, "probe", proxy_address,
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    payload = None
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    passed = (result.returncode == 0 and payload
              and payload.get("github_tls") == 0 and payload.get("release_assets_tls") == 0)
    return {
        "status": "pass" if passed else "blocked",
        "return_code": result.returncode,
        "github_tls": (payload or {}).get("github_tls"),
        "release_assets_tls": (payload or {}).get("release_assets_tls"),
        "stderr_tail": result.stderr[-1000:],
    }


def transport_probe(output: str | Path, *, expected_sha: str, probe=docker_transport_probe) -> dict:
    output, plan, _ = validate_plan(output, expected_sha)
    if not proxy_reachable(plan["runtime_proxy"]):
        blocked = {
            "protocol": "long-horizon-public-pilot-v1-transport",
            "status": "blocked",
            "plan_sha256": expected_sha,
            "proxy": plan["runtime_proxy"],
            "proxy_reachable": False,
            "model_requests": 0,
            "tasks": {},
        }
        write_sealed(output / "readiness/transport.json", blocked)
        raise PilotStop("scoped proxy is unavailable")
    task_records = {}
    for task in TASKS:
        slot = next(slot for slot in plan["slots"] if slot["task"] == task)
        record = probe(slot["constraints"]["docker_image"], plan["runtime_proxy"])
        record.update(task=task, image=slot["image"], proxy=plan["runtime_proxy"], model_requests=0)
        task_records[task] = record
    receipt = {
        "protocol": "long-horizon-public-pilot-v1-transport",
        "status": "pass" if all(row["status"] == "pass" for row in task_records.values()) else "blocked",
        "plan_sha256": expected_sha,
        "proxy": plan["runtime_proxy"],
        "proxy_reachable": True,
        "model_requests": 0,
        "tasks": task_records,
    }
    write_sealed(output / "readiness/transport.json", receipt)
    if receipt["status"] != "pass":
        raise PilotStop("same-image transport probe failed")
    return receipt


def _validate_oracle_receipt(output: Path, plan: dict, expected_sha: str) -> dict:
    path = output / "readiness/oracle.json"
    if not path.is_file():
        raise PilotStop("fresh oracle receipt is missing")
    receipt = read_sealed(path)
    if receipt.get("plan_sha256") != expected_sha or receipt.get("status") != "pass":
        raise PilotStop("fresh oracle receipt is not valid for this plan")
    for task in TASKS:
        row = receipt.get("tasks", {}).get(task, {})
        result = Path(row.get("result_path", ""))
        if (row.get("status") != "pass" or not result.is_file()
                or sha_file(result) != row.get("result_sha256")):
            raise PilotStop(f"fresh oracle evidence changed: {task}")
        gate = oracle_gate(Path(plan["tasks_root"]) / task, result)
        if gate["harbor_task_checksum"] != row.get("task_checksum"):
            raise PilotStop(f"fresh oracle checksum differs: {task}")
    return receipt


def _validate_transport_receipt(output: Path, plan: dict, expected_sha: str) -> dict:
    path = output / "readiness/transport.json"
    if not path.is_file():
        raise PilotStop("transport receipt is missing")
    receipt = read_sealed(path)
    if receipt.get("plan_sha256") != expected_sha or receipt.get("status") != "pass":
        raise PilotStop("transport receipt is not valid for this plan")
    images = {task: next(slot for slot in plan["slots"] if slot["task"] == task)["image"]["id"]
              for task in TASKS}
    if any(receipt["tasks"].get(task, {}).get("image", {}).get("id") != images[task]
           for task in TASKS):
        raise PilotStop("transport image identity changed")
    return receipt


def langfuse_status(plan: dict) -> dict:
    expected = str(plan["langfuse"]["base_url"]).rstrip("/")
    names = tuple(plan["langfuse"]["env_names"])
    if set(names) != set(LANGFUSE_ENV_NAMES):
        raise PilotStop("Langfuse environment-name allowlist differs from the frozen protocol")
    presence = {name: bool(os.environ.get(name)) for name in names}

    def normalized(value):
        value = (value or "").strip().rstrip("/")
        if value and "://" not in value:
            value = "https://" + value
        return value

    base_matches = normalized(os.environ.get("LANGFUSE_BASE_URL")) == expected
    host_matches = normalized(os.environ.get("LANGFUSE_HOST")) == expected
    return {
        "ready": all(presence.values()) and base_matches and host_matches,
        "env_presence": presence,
        "base_url_matches": base_matches,
        "host_matches": host_matches,
        "base_url": expected,
    }


def preflight(output: str | Path, *, expected_sha: str, require_ready: bool = False) -> dict:
    output, plan, book = validate_plan(output, expected_sha)
    credential_source = install_live_credential()
    proxy_ready = proxy_reachable(plan["runtime_proxy"])
    observation = langfuse_status(plan)
    try:
        oracle_ready = _validate_oracle_receipt(output, plan, expected_sha)["status"] == "pass"
    except PilotStop:
        oracle_ready = False
    try:
        transport_ready = _validate_transport_receipt(output, plan, expected_sha)["status"] == "pass"
    except PilotStop:
        transport_ready = False
    data = book.snapshot()
    result = {
        "protocol": "long-horizon-public-pilot-v1-preflight",
        "status": "ready" if all((credential_source, proxy_ready, oracle_ready, transport_ready,
                                         observation["ready"])) else "blocked",
        "plan_sha256": expected_sha,
        "model_requests": 0,
        "wsl": platform.release(),
        "harbor": subprocess.run([str(HARBOR), "--version"], text=True, capture_output=True).stdout.strip(),
        "docker": subprocess.run(
            ["docker", "version", "--format", "{{.Client.Version}}|{{.Server.Version}}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
        "credential_source": credential_source,
        "credential_value_persisted": False,
        "proxy": plan["runtime_proxy"],
        "proxy_reachable": proxy_ready,
        "oracle_ready": oracle_ready,
        "transport_ready": transport_ready,
        "langfuse_ready": observation["ready"],
        "langfuse": observation,
        "ledger": {
            "path": plan["ledger"],
            "windows_path": plan["ledger_windows_path"],
            "reason": plan["ledger_reason"],
            "requests": len(data["requests"]),
            "registered_runs": len(data["runs"]),
            "blocked_campaigns": [
                name for name, row in data.get("campaigns", {}).items() if row.get("blocked")
            ],
        },
    }
    write_sealed(output / "readiness/preflight.json", result)
    if require_ready and result["status"] != "ready":
        raise PilotStop("preflight is not execution-ready")
    return result


def _read_existing_result(directory: Path, task: str, expected_name: str):
    return collect(directory, task, expected_task_name=expected_name)


def _live_contract(directory: Path, slot: dict) -> dict:
    paths = list(directory.glob("jobs/**/live_run.json"))
    if len(paths) != 1:
        raise PilotStop(f"expected one live_run.json: {slot['task']}/{slot['arm']}")
    metadata = read_json(paths[0])
    expected_limits = {
        "max_requests": slot["limits"]["max_requests"],
        "max_seconds": slot["limits"]["max_seconds"],
        "context_budget": slot["limits"]["context_budget"],
        "output_tokens": slot["limits"]["output_tokens"],
        "shell_timeout": slot["limits"]["shell_timeout"],
        "compaction_mode": slot["compaction_mode"],
        "observation_mode": slot["observation_mode"],
        "observation_required": slot["observation_required"],
    }
    observation = metadata.get("observation") or {}
    if (metadata.get("artifact_projection") != slot["artifact_projection"]
            or metadata.get("compaction_mode") != slot["compaction_mode"]
            or metadata.get("observation_mode") != slot["observation_mode"]
            or metadata.get("observation_required") is not slot["observation_required"]
            or metadata.get("limits") != expected_limits
            or observation.get("ready") is not True
            or observation.get("mode") != slot["observation_mode"]
            or observation.get("required") is not slot["observation_required"]
            or observation.get("base_url") != slot["observation_base_url"]
            or observation.get("arm") != slot["arm"]
            or observation.get("stage") != "final"
            or observation.get("task_id") != slot["task"]
            or (metadata.get("oracle_gate") or {}).get("harbor_task_checksum")
            != slot["oracle_gate"]["harbor_task_checksum"]
            or not metadata.get("schema_sha256")
            or metadata.get("schema_sha256") != metadata.get("tool_schema_sha256")
            or not metadata.get("experiment_identity_sha256")):
        raise PilotStop(f"live run contract mismatch: {slot['task']}/{slot['arm']}")
    return metadata


def _trial_usage(ledger: BudgetLedger, run_id: str) -> dict:
    rows = [row for row in ledger.snapshot()["requests"] if row["run_id"] == run_id]
    complete = bool(rows) and all(
        row.get("status") == "ok" and row.get("cost_micro_usd") is not None for row in rows
    )
    return {"requests": len(rows), "usage_complete": complete}


def run(
    output: str | Path,
    *,
    expected_sha: str,
    execute: bool = False,
    max_new_trials: int = 1,
    executor=execute_harbor,
) -> dict:
    if type(max_new_trials) is not int or not 1 <= max_new_trials <= 9:
        raise PilotStop("max_new_trials must be 1..9")
    output, plan, _ = validate_plan(output, expected_sha)
    if not execute:
        return {"preview": True, "model_requests": 0, "slots": plan["slots"]}
    preflight(output, expected_sha=expected_sha, require_ready=True)
    oracle_receipt = _validate_oracle_receipt(output, plan, expected_sha)
    with FileLock(str(output / "driver.lock"), timeout=0):
        with FileLock(str(Path(plan["ledger"])) + ".driver.lock", timeout=0):
            journal_path = output / "journal.json"
            journal = read_json(journal_path) if journal_path.exists() else {}
            new = 0
            for index, slot in enumerate(plan["slots"]):
                run_id = slot["run_id"]
                directory = output / f"{index:02d}-{slot['task']}-{slot['arm']}"
                state = journal.get(run_id)
                expected_name = slot["oracle_gate"]["task_name"]
                if state:
                    if state.get("status") == "started":
                        journal[run_id] = _read_existing_result(directory, slot["task"], expected_name) or {
                            "status": "interrupted_unknown"
                        }
                        if journal[run_id].get("status") == "finished":
                            try:
                                metadata = _live_contract(directory, slot)
                                journal[run_id].update(
                                    schema_sha256=metadata["schema_sha256"],
                                    compaction_mode=metadata["compaction_mode"],
                                    artifact_projection=metadata["artifact_projection"],
                                    observation_mode=metadata["observation_mode"],
                                    observation_required=metadata["observation_required"],
                                    observation_ready=metadata["observation"]["ready"],
                                    experiment_identity_sha256=metadata["experiment_identity_sha256"],
                                )
                            except PilotStop as exc:
                                journal[run_id]["contract_error"] = str(exc)
                            ledger_now = BudgetLedger(
                                plan["ledger"], limit_usd="30", max_requests=16, time_cap=600,
                                spend_ceiling_usd=plan["spend_ceiling_usd"],
                                campaign_id=plan["campaign_id"],
                            )
                            journal[run_id].update(_trial_usage(ledger_now, run_id))
                        write_json(journal_path, journal, replace=True)
                    if (journal[run_id].get("status") != "finished"
                            or journal[run_id].get("category") != "scored"
                            or journal[run_id].get("contract_error")
                            or journal[run_id].get("suspected_verifier_infra")
                            or journal[run_id].get("usage_complete") is not True):
                        break
                    continue
                if new >= max_new_trials:
                    break
                fresh_oracle = oracle_receipt["tasks"][slot["task"]]["result_path"]
                fresh_gate = oracle_gate(Path(plan["tasks_root"]) / slot["task"], fresh_oracle)
                if fresh_gate["harbor_task_checksum"] != slot["oracle_gate"]["harbor_task_checksum"]:
                    raise PilotStop("fresh oracle identity does not match the frozen existing oracle")
                ledger = BudgetLedger(
                    plan["ledger"],
                    limit_usd="30",
                    max_requests=16,
                    time_cap=600,
                    spend_ceiling_usd=plan["spend_ceiling_usd"],
                    campaign_id=plan["campaign_id"],
                )
                consumed = ledger.snapshot()["runs"].get(run_id, {})
                if consumed.get("trial_started") or any(
                        row["run_id"] == run_id for row in ledger.snapshot()["requests"]):
                    journal[run_id] = {"status": "consumed_elsewhere"}
                    write_json(journal_path, journal, replace=True)
                    continue
                config = trial_config(plan, slot, directory, fresh_oracle)
                _trial_config_validate(config)
                directory.mkdir(parents=True, exist_ok=True)
                write_json(directory / "job.json", config)
                journal[run_id] = {"status": "started"}
                write_json(journal_path, journal, replace=True)
                with ledger.transaction() as data:
                    if data["runs"][run_id].get("trial_started"):
                        raise BudgetStop("frozen trial was already started")
                    data["runs"][run_id]["trial_started"] = True
                new += 1
                try:
                    executor(config, directory, slot["limits"]["max_seconds"] + 3600)
                except BaseException as exc:
                    journal[run_id] = _read_existing_result(directory, slot["task"], expected_name) or {
                        "status": "infrastructure_error",
                        "error_type": type(exc).__name__,
                    }
                else:
                    journal[run_id] = _read_existing_result(directory, slot["task"], expected_name) or {
                        "status": "missing_official_result"
                    }
                if journal[run_id].get("status") == "finished":
                    try:
                        metadata = _live_contract(directory, slot)
                        journal[run_id].update(
                            schema_sha256=metadata["schema_sha256"],
                            compaction_mode=metadata["compaction_mode"],
                            artifact_projection=metadata["artifact_projection"],
                            observation_mode=metadata["observation_mode"],
                            observation_required=metadata["observation_required"],
                            observation_ready=metadata["observation"]["ready"],
                            experiment_identity_sha256=metadata["experiment_identity_sha256"],
                        )
                    except PilotStop as exc:
                        journal[run_id]["contract_error"] = str(exc)
                    ledger_now = BudgetLedger(
                        plan["ledger"], limit_usd="30", max_requests=16, time_cap=600,
                        spend_ceiling_usd=plan["spend_ceiling_usd"], campaign_id=plan["campaign_id"],
                    )
                    journal[run_id].update(_trial_usage(ledger_now, run_id))
                    task_indices = [i for i, candidate in enumerate(plan["slots"])
                                    if candidate["task"] == slot["task"]]
                    if index == task_indices[-1] and all(
                            journal.get(plan["slots"][i]["run_id"], {}).get("schema_sha256")
                            for i in task_indices):
                        schemas = [journal[plan["slots"][i]["run_id"]]["schema_sha256"]
                                   for i in task_indices]
                        if len(set(schemas)) != 1:
                            journal[run_id]["contract_error"] = "tool schema differs within task triplet"
                write_json(journal_path, journal, replace=True)
                if (journal[run_id].get("status") != "finished"
                        or journal[run_id].get("category") != "scored"
                        or journal[run_id].get("contract_error")
                        or journal[run_id].get("suspected_verifier_infra")
                        or journal[run_id].get("usage_complete") is not True):
                    break
                validate_plan(output, expected_sha)
            return summary(output)


def _sign_test(deltas: list[int]) -> dict:
    nonzero = [value for value in deltas if value]
    positives = sum(value > 0 for value in nonzero)
    negatives = sum(value < 0 for value in nonzero)
    n = len(nonzero)
    if not n:
        return {"positive": 0, "negative": 0, "n": 0, "two_sided_p": None}
    tail = sum(math.comb(n, index) for index in range(min(positives, negatives) + 1)) / (2 ** n)
    return {"positive": positives, "negative": negatives, "n": n,
            "two_sided_p": min(1.0, 2 * tail)}


def summary(output: str | Path) -> dict:
    output = Path(output).resolve()
    plan = read_json(output / "plan.json")
    journal = read_json(output / "journal.json") if (output / "journal.json").is_file() else {}
    book = read_json(plan["ledger"])
    rows = []
    for index, slot in enumerate(plan["slots"]):
        directory = output / f"{index:02d}-{slot['task']}-{slot['arm']}"
        requests = [row for row in book["requests"] if row["run_id"] == slot["run_id"]]
        known = bool(requests) and all(row.get("cost_micro_usd") is not None for row in requests)
        metadata = [read_json(path) for path in directory.glob("jobs/**/live_run.json")]
        meta = metadata[0] if len(metadata) == 1 else None
        state = journal.get(slot["run_id"], {"status": "not_started"})
        rows.append({
            "task": slot["task"], "arm": slot["arm"], "run_id": slot["run_id"], **state,
            "artifact_projection": meta.get("artifact_projection") if meta else None,
            "compaction_mode": meta.get("compaction_mode") if meta else None,
            "observation_mode": meta.get("observation_mode") if meta else None,
            "observation_required": meta.get("observation_required") if meta else None,
            "observation_ready": (meta.get("observation") or {}).get("ready") if meta else None,
            "schema_sha256": meta.get("schema_sha256") if meta else None,
            "experiment_identity_sha256": meta.get("experiment_identity_sha256") if meta else None,
            "limits": meta.get("limits") if meta else None,
            "requests": len(requests),
            "actual_cost_usd": sum(row["cost_micro_usd"] for row in requests) / 1_000_000 if known else None,
            "usage_complete": known,
            "cache_tokens": sum(row["usage"]["cache_read_tokens"] for row in requests)
            if known and all("cache_read_tokens" in row["usage"] for row in requests) else None,
        })
    triplets = []
    for task in TASKS:
        task_rows = {row["arm"]: row for row in rows if row["task"] == task}
        expected_modes = {arm: ARMS[arm]["compaction_mode"] for arm in ARMS}
        complete = all(task_rows.get(arm, {}).get("status") == "finished"
                       and task_rows.get(arm, {}).get("category") == "scored" for arm in ARMS)
        schemas = {arm: task_rows.get(arm, {}).get("schema_sha256") for arm in ARMS}
        schema_consistent = all(schemas.values()) and len(set(schemas.values())) == 1
        modes_ok = all(task_rows.get(arm, {}).get("compaction_mode") == expected_modes[arm] for arm in ARMS)
        projections_ok = all(task_rows.get(arm, {}).get("artifact_projection") == ARMS[arm]["artifact_projection"]
                             for arm in ARMS)
        observations_ok = all(
            task_rows.get(arm, {}).get("observation_mode") == "langfuse"
            and task_rows.get(arm, {}).get("observation_required") is True
            and task_rows.get(arm, {}).get("observation_ready") is True
            for arm in ARMS
        )
        suspected = any(task_rows.get(arm, {}).get("suspected_verifier_infra", False) for arm in ARMS)
        usage_complete = all(task_rows.get(arm, {}).get("usage_complete") is True for arm in ARMS)
        contracts_ok = not any(task_rows.get(arm, {}).get("contract_error") for arm in ARMS)
        rewards = {arm: task_rows.get(arm, {}).get("official_reward") for arm in ARMS}
        valid = (complete and schema_consistent and modes_ok and projections_ok and observations_ok
                 and usage_complete and contracts_ok and not suspected)
        triplets.append({
            "task": task,
            "complete_scored_triplet": complete,
            "schema_consistent": schema_consistent,
            "modes_match": modes_ok,
            "projection_matches": projections_ok,
            "observations_ready": observations_ok,
            "usage_complete": usage_complete,
            "contracts_valid": contracts_ok,
            "suspected_verifier_infra": suspected,
            "valid_triplet": valid,
            "rewards": rewards,
            "delta_enhanced_minus_baseline": rewards["enhanced"] - rewards["baseline"] if valid else None,
            "delta_enhanced_minus_loo": rewards["enhanced"] - rewards["loo_artifact"] if valid else None,
        })
    result = {
        "protocol": plan["protocol"],
        "model_requests": sum(row["requests"] for row in rows),
        "rows": rows,
        "triplets": triplets,
        "complete_scored_triplets": sum(row["complete_scored_triplet"] for row in triplets),
        "valid_triplets": sum(row["valid_triplet"] for row in triplets),
        "categories": dict(Counter(row.get("category", row["status"]) for row in rows)),
        "statistics": {
            "enhanced_minus_baseline_sign_test": _sign_test([
                row["delta_enhanced_minus_baseline"] for row in triplets
                if row["delta_enhanced_minus_baseline"] is not None
            ]),
            "loo_sign_test": _sign_test([
                row["delta_enhanced_minus_loo"] for row in triplets
                if row["delta_enhanced_minus_loo"] is not None
            ]),
        },
        "evidence_scope": "local ledger/live_run/oracle/transport only; Langfuse remote readback not performed",
    }
    write_json(output / "summary.json", result, replace=True)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    frozen = sub.add_parser("freeze")
    frozen.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    for name in ("preflight", "oracle", "transport"):
        command = sub.add_parser(name)
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        command.add_argument("--expected-sha", required=True)
    dispatch = sub.add_parser("run")
    dispatch.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    dispatch.add_argument("--expected-sha", required=True)
    dispatch.add_argument("--execute", action="store_true")
    dispatch.add_argument("--max-new-trials", type=int, default=1)
    report = sub.add_parser("summary")
    report.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    if command == "freeze":
        result = freeze(**args)
    elif command == "preflight":
        result = preflight(**args)
    elif command == "oracle":
        result = run_oracles(**args)
    elif command == "transport":
        result = transport_probe(**args)
    elif command == "run":
        result = run(**args)
    else:
        result = summary(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
