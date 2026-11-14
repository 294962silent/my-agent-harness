"""Independent fixed SWE-EVO seam; safe solver metadata and separate official scoring."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from types import SimpleNamespace
from uuid import uuid4

from filelock import FileLock

from my_agent_core.agent import Agent
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.events import AgentEnd
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, BudgetStop, GuardedLLM, LIVE_BASE_URL, LIVE_MODEL, create_live_llm
from .long_horizon import source_hashes, write_json
from .swe_agent import DockerTask, SetupError

REVISION = "9b83d5af943ba7a17567336f5b18239f73960219"
SOURCE_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
FORK_MANIFEST_SHA256 = "8abe773cd47e75a0d0f7c778ece57c1919cef40fbbb7f8ef6c48614b9c6291f9"
CACHE_ADAPTER_SHA256 = "dda7435e019dde8468e3954b012f5abf637824f785a160f8a8ad4b576f15d855"
PREFLIGHT_ADAPTER_SHA256 = "60672af1f6605e048bd539ef130e812e92e7bf96ec7eb5f508bec2e373f5a56d"
CAMPAIGN = "longhorizon-20261113-code"
SPEND_CEILING = 15.888355
LIMITS = {"max_requests": 64, "max_seconds": 1800, "context_budget": 32000, "output_tokens": 4096}
SAFE_FIELDS = {"instance_id", "repo", "base_commit", "image", "problem_statement"}
PINS = {
    "conan-io__conan_2.0.14_2.0.15": {
        "short": "conan", "repo": "conan-io/conan", "base_commit": "4614b3abbff15627b3fabdd98bee419721f423ce",
        "image": "xingyaoww/sweb.eval.x86_64.conan-io_s_conan-15109:latest",
        "image_id": "sha256:0d0885fcb5607347cabc7256fd30ce4dfb092063c54a420b7f4ed445aeda20e3",
        "gold_report_sha256": "95fe670b2cab3c8d0e1ffd1254e8edf3edea8614f92ed94a173ff26633f2dd00",
        "empty_report_sha256": "a036bf380b286c9c3ed7d4d58ca079ccf82e02c01a6812ac0b5e4ec9f5dbfd84",
    },
    "dask__dask_2022.9.2_2022.10.0": {
        "short": "dask", "repo": "dask/dask", "base_commit": "3ef47422b9f830f81562960ef549778819498aa1",
        "image": "xingyaoww/sweb.eval.x86_64.dask_s_dask-9531:latest",
        "image_id": "sha256:3bfe422ab58729fc9f4765f79076d3f22db8dba3c16d63101d83ac339f7c44b4",
        "gold_report_sha256": "54eb51e59ac3819e181cc42d5b66a6002803443e441122e601ccb9ccadf2255f",
        "empty_report_sha256": "053b84ab8b63ff35db674c82676735a6f58c6e13512365328eb5ca41547c7dbd",
    },
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_rows(source):
    from datasets import Dataset
    dataset = Dataset.from_file(str(source))
    if len(dataset) != 48:
        raise ValueError("official EVO source must contain the frozen 48 records")
    return [row for row in dataset if row["instance_id"] in PINS]


def _clean_env():
    env = {k: v for k, v in os.environ.items() if not re.search(
        r"API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|OPENAI|ANTHROPIC|DEEPSEEK|LIVE_BENCH", k, re.I)}
    env["CUDA_VISIBLE_DEVICES"] = "2"
    return env


def _no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
        raise ValueError("symlink/junction in trusted artifact path")
    return path


def _scorer_hashes(root):
    root = _no_links(root).resolve()
    if sha(root / "source-manifest.json") != FORK_MANIFEST_SHA256:
        raise ValueError("fixed official scorer manifest differs")
    manifest = _json(root / "source-manifest.json")
    if manifest["revision"] != REVISION:
        raise ValueError("scorer revision differs")
    proofs = {}
    expected_executable = set()
    for entry in manifest["entries"]:
        relative = entry["path"].removeprefix("SWE-bench/")
        if not entry["path"].startswith("SWE-bench/") or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("scorer source escaped fixed root")
        path = _no_links(root / relative).resolve()
        data = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if sha(path) != entry["sha256"] or blob != entry["git_blob"] or len(data) != entry["bytes"]:
            raise ValueError("official scorer bytes differ")
        proofs[str(path)] = entry["sha256"]
        expected_executable.add(relative)
    for path in root.rglob("*"):
        _no_links(path)
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        # Existing interpreter caches are not trusted or copied. Only pyc in
        # __pycache__ is treated as a runtime artifact, never a source module.
        if "__pycache__" in path.relative_to(root).parts and path.suffix == ".pyc":
            continue
        executable = (path.suffix.lower() in {".py", ".pyw", ".pyc", ".pyo", ".pth", ".so", ".pyd", ".dll", ".dylib"}
                      or ".so." in path.name.lower())
        if executable and relative not in expected_executable:
            raise ValueError("scorer executable inventory differs from official manifest")
    proofs[str(root / "source-manifest.json")] = FORK_MANIFEST_SHA256
    return proofs


def _report(preflight, iid, pin, arm):
    paths = list((Path(preflight) / (pin["short"] + "-" + arm)).glob("**/report.json"))
    if len(paths) != 1 or sha(paths[0]) != pin[arm + "_report_sha256"]:
        raise ValueError("fixed gold/empty official report hash differs")
    raw = _json(paths[0])
    if set(raw) != {iid}:
        raise ValueError("preflight report has a different instance")
    return raw[iid], str(paths[0].resolve())


def _double_gate(gold, empty):
    def statuses(report, kind):
        return (report.get("tests_status") or {}).get(kind) or {}
    gf, gp = statuses(gold, "FAIL_TO_PASS"), statuses(gold, "PASS_TO_PASS")
    ef, ep = statuses(empty, "FAIL_TO_PASS"), statuses(empty, "PASS_TO_PASS")
    return bool(gold.get("resolved") is True and empty.get("resolved") is False
                and gold.get("patch_successfully_applied") is True and empty.get("patch_successfully_applied") is True
                and gf.get("success") and gf.get("failure") == [] and gp.get("failure") == []
                and ef.get("success") == [] and ef.get("failure") and ep.get("failure") == [])


def _safe(row, iid, pin):
    if (row.get("instance_id") != iid or row.get("repo") != pin["repo"]
            or row.get("base_commit") != pin["base_commit"]
            or row.get("image", "").removesuffix(":latest") != pin["image"].removesuffix(":latest")
            or not isinstance(row.get("problem_statement"), str) or not row["problem_statement"].strip()):
        raise ValueError("official row identity or original problem statement differs")
    return {"instance_id": iid, "repo": pin["repo"], "base_commit": pin["base_commit"],
            "image": pin["image"], "problem_statement": row["problem_statement"]}


def run_id(iid, arm):
    if iid not in PINS or arm not in ("on", "off"):
        raise ValueError("unknown EVO task/arm")
    return "evo-" + hashlib.sha256(json.dumps([CAMPAIGN, REVISION, iid, arm]).encode()).hexdigest()[:32]


def freeze(*, source, preflight, output, ledger, scorer_root, cache_adapter=None):
    cache_adapter = cache_adapter or Path(__file__).with_name("evo_cache_adapter.py")
    source, root, ledger = Path(source).resolve(), Path(output).resolve(), Path(ledger)
    if root.exists() or not ledger.is_absolute() or not ledger.is_file():
        raise ValueError("new output and existing absolute shared ledger required")
    if sha(source) != SOURCE_SHA256 or sha(cache_adapter) != CACHE_ADAPTER_SHA256:
        raise ValueError("fixed official Arrow or cache transport hash differs")
    scorer = _scorer_hashes(scorer_root)
    rows = _load_rows(source)
    if {r["instance_id"] for r in rows} != set(PINS) or len(rows) != 2:
        raise ValueError("both frozen EVO selections must be retained")
    book = BudgetLedger(ledger, campaign_id=CAMPAIGN, spend_ceiling_usd=SPEND_CEILING).snapshot()
    registered = {key for key, value in book["runs"].items() if value.get("campaign_id")}
    if any(r["status"] == "pending" and r["run_id"] in registered for r in book["requests"]):
        raise ValueError("wait for the shared campaign request to settle before freezing EVO")
    tasks, safe_values, proofs = [], {}, {str(source): SOURCE_SHA256,
                                        str(Path(cache_adapter).resolve()): CACHE_ADAPTER_SHA256, **scorer}
    for iid, pin in PINS.items():
        row = next(r for r in rows if r["instance_id"] == iid)
        safe = _safe(row, iid, pin)
        gold, gold_path = _report(preflight, iid, pin, "gold")
        empty, empty_path = _report(preflight, iid, pin, "empty")
        proofs[gold_path], proofs[empty_path] = pin["gold_report_sha256"], pin["empty_report_sha256"]
        metadata = pin["short"] + "-safe.json"
        safe_values[metadata] = safe
        tasks.append({"instance_id": iid, "metadata": metadata,
                      "gate_passed": _double_gate(gold, empty), "gold_report": gold_path, "empty_report": empty_path,
                      "runs": {arm: run_id(iid, arm) for arm in ("on", "off")}})
    root.mkdir(parents=True)
    for name, safe in safe_values.items():
        write_json(root / name, safe)
    for task in tasks:
        task["metadata_sha256"] = sha(root / task["metadata"])
    plan = {"protocol": "swe-evo-artifact-projection-v1", "revision": REVISION,
            "source": str(source), "source_sha256": SOURCE_SHA256, "tasks": tasks,
            "ledger": str(ledger.resolve()), "campaign_id": CAMPAIGN, "spend_ceiling_usd": SPEND_CEILING,
            "ledger_policy": {k: book[k] for k in ("limit_micro_usd", "pricing", "max_requests", "time_cap")},
            "ledger_prefix": book["requests"], "historical_runs": book["runs"],
            "scorer_root": str(Path(scorer_root).absolute()), "cache_adapter": str(Path(cache_adapter).resolve()),
            "preflight_adapter_sha256": PREFLIGHT_ADAPTER_SHA256,
            "cache_adapter_sha256": CACHE_ADAPTER_SHA256,
            "input_hashes": proofs, "source_hashes": source_hashes(),
            "limits": dict(LIMITS),
            "git_policy": "single_root_same_tree_no_history", "model": LIVE_MODEL, "endpoint": LIVE_BASE_URL}
    write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(sha(root / "plan.json") + "\n")
    return plan


def verify_plan(root, expected_sha):
    root = Path(root).resolve()
    if sha(root / "plan.json") != expected_sha:
        raise ValueError("explicit EVO plan SHA differs")
    plan = _json(root / "plan.json")
    if (plan["protocol"] != "swe-evo-artifact-projection-v1" or plan["revision"] != REVISION
            or plan["source_sha256"] != SOURCE_SHA256 or plan["source_hashes"] != source_hashes()
            or plan["campaign_id"] != CAMPAIGN or plan["spend_ceiling_usd"] != SPEND_CEILING
            or plan["limits"] != LIMITS or plan["model"] != LIVE_MODEL or plan["endpoint"] != LIVE_BASE_URL
            or len(plan["tasks"]) != 2 or {t["instance_id"] for t in plan["tasks"]} != set(PINS)):
        raise ValueError("EVO frozen source/protocol differs")
    if any(not Path(p).is_file() or sha(p) != value for p, value in plan["input_hashes"].items()):
        raise ValueError("EVO frozen source/report/scorer bytes differ")
    if any(plan["input_hashes"].get(p) != value for p, value in _scorer_hashes(plan["scorer_root"]).items()):
        raise ValueError("frozen scorer inventory differs")
    for task in plan["tasks"]:
        path = root / task["metadata"]
        safe = _json(path)
        if set(safe) != SAFE_FIELDS or sha(path) != task["metadata_sha256"]:
            raise ValueError("safe solver metadata differs")
        _safe(safe, task["instance_id"], PINS[task["instance_id"]])
        if task["gate_passed"] != _double_gate(_json(task["gold_report"])[task["instance_id"]],
                                              _json(task["empty_report"])[task["instance_id"]]):
            raise ValueError("frozen double gate differs")
    if not Path(plan["ledger"]).is_file():
        raise ValueError("shared ledger missing")
    book = BudgetLedger(plan["ledger"], campaign_id=CAMPAIGN, spend_ceiling_usd=SPEND_CEILING).snapshot()
    if (any(book[k] != v for k, v in plan["ledger_policy"].items())
            or book["requests"][:len(plan["ledger_prefix"])] != plan["ledger_prefix"]
            or any(book["runs"].get(k) != v for k, v in plan["historical_runs"].items())):
        raise ValueError("shared ledger historical policy/prefix differs")
    return plan


class EvoDockerTask(DockerTask):
    """Reuse Docker shell lifecycle, with an independently pinned EVO identity."""
    def __init__(self, iid):
        pin = PINS[iid]
        self.spec = SimpleNamespace(instance_id=iid)
        self.pin = {k: pin[k] for k in ("base_commit", "image", "image_id")}
        self.name = "harness-evo-" + uuid4().hex
        self.created, self.stage, self.initial_head = False, "image_inspect", None
        self.shell_timeout, self.baseline = 120, {}

    async def call(self, *args, timeout=90):
        proc = await asyncio.create_subprocess_exec("docker", *args, env=_clean_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            await proc.communicate()
            raise
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def exec(self, command):
        seconds = max(0.01, min(120, self.shell_timeout))
        code, out, err = await self.call("exec", "-w", "/testbed", self.name, "timeout", "--signal=TERM",
            "--kill-after=5", str(seconds), "/bin/bash", "-lc", command, timeout=seconds + 10)
        return {"exit_code": code, "stdout": out, "stderr": err}

    async def start(self):
        await super().start()
        self.stage = "baseline_git_export"
        code, owner, _ = await self.call("inspect", "--format", "{{.Name}} {{.Image}}", self.name)
        if not self.created or code or owner.strip() != "/" + self.name + " " + self.pin["image_id"]:
            raise SetupError("fresh_container_ownership_not_verified")
        original = await self.exec("git rev-parse HEAD HEAD^{tree}; git rev-list --all --not "
                                   + self.pin["base_commit"] + " | wc -l; git for-each-ref | wc -l")
        if original["exit_code"]:
            raise SetupError("baseline_git_observation_failed")
        fields = original["stdout"].splitlines()
        if len(fields) != 4 or not all(re.fullmatch("[0-9a-f]{40}", x) for x in fields[:2]):
            raise SetupError("baseline_git_observation_invalid")
        self.baseline = {"original_base": fields[0], "original_tree": fields[1],
                         "future_reachable_commits": int(fields[2]), "original_refs": int(fields[3]),
                         "policy": "single_root_same_tree_no_history"}
        # Own freshly created container, fixed absolute /testbed/.git only. Keep
        # ignored dependencies; index exactly the original tracked path list.
        exported = await self.exec("""set -eu
test "$(pwd -P)" = /testbed
test "$(git rev-parse --show-toplevel)" = /testbed
test "$(git rev-parse --git-dir)" = .git
test -d /testbed/.git && test ! -L /testbed/.git
tree=$(git rev-parse HEAD^{tree})
paths=$(mktemp /tmp/evo-tracked.XXXXXX)
git ls-files -z > "$paths"
rm -rf -- /testbed/.git
git init -q /testbed
git config user.name EVO-Baseline
git config user.email evo-baseline@example.invalid
git config core.autocrlf false
git add --force --pathspec-from-file="$paths" --pathspec-file-nul
test "$(git write-tree)" = "$tree"
GIT_AUTHOR_NAME=EVO-Baseline GIT_AUTHOR_EMAIL=evo-baseline@example.invalid GIT_COMMITTER_NAME=EVO-Baseline GIT_COMMITTER_EMAIL=evo-baseline@example.invalid GIT_AUTHOR_DATE=2000-01-01T00:00:00+0000 GIT_COMMITTER_DATE=2000-01-01T00:00:00+0000 git -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m EVO-baseline
rm -f -- "$paths"
git reflog expire --expire=now --all
test "$(git rev-list --all --count)" = 1
test -z "$(git remote)"
test -z "$(git fsck --full --no-reflogs --unreachable 2>/dev/null)"
git rev-parse HEAD HEAD^{tree}
""")
        values = exported["stdout"].splitlines()
        if exported["exit_code"] or len(values) != 2 or values[1] != self.baseline["original_tree"]:
            raise SetupError("single_root_baseline_export_failed")
        self.baseline.update(root_commit=values[0], exported_tree=values[1], tree_equal=True,
                             original_git_objects_removed=True, remotes=0, reachable_commits=1)
        self.stage = "ready"

    async def patch(self):
        result = await self.exec("git add --intent-to-add . && git diff --binary " + self.baseline["root_commit"])
        if result["exit_code"]:
            raise SetupError("prediction_diff_extract_failed")
        return result["stdout"]


def _selected(plan, iid):
    task = next((t for t in plan["tasks"] if t["instance_id"] == iid), None)
    if task is None or not task["gate_passed"]:
        raise ValueError("EVO gold/empty gate does not pass for this frozen selection")
    return task


async def probe(plan_root, *, expected_sha, instance_id, output):
    plan = verify_plan(plan_root, expected_sha)
    _selected(plan, instance_id)
    path = Path(output)
    if path.exists():
        raise ValueError("probe output must be new")
    env = EvoDockerTask(instance_id)
    result = {"instance_id": instance_id, "model_requests": 0}
    try:
        await env.start()
        result.update(status="ready", baseline=env.baseline)
    except Exception as exc:
        result.update(status="environment_blocked", error_code=getattr(exc, "error_code", type(exc).__name__))
    finally:
        try:
            await env.close()
            result["cleanup"] = "completed"
        except Exception as exc:
            result["cleanup"] = type(exc).__name__
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, result)
    return result


async def solve(plan_root, *, expected_sha, instance_id, arm, output, resume=False, environment=None):
    plan = verify_plan(plan_root, expected_sha)
    task = _selected(plan, instance_id)
    identifier = run_id(instance_id, arm)
    if task["runs"][arm] != identifier:
        raise ValueError("frozen EVO run identity differs")
    root = Path(output).resolve()
    if root.exists():
        if not resume:
            raise ValueError("generation output exists; explicit resume only collects")
        if (root / "run.json").exists():
            report = _json(root / "run.json")
        else:
            started = _json(root / "started.json")
            report = {**started, "status": "interrupted_no_retry", "plan_sha256": expected_sha}
        if (report.get("run_id") != identifier or report.get("instance_id") != instance_id
                or report.get("arm") != arm or report.get("plan_sha256", expected_sha) != expected_sha):
            raise ValueError("resume output belongs to another run")
        if report.get("prediction_sha256") and sha(root / "preds.json") != report["prediction_sha256"]:
            raise ValueError("recorded prediction changed")
        return report
    ledger = BudgetLedger(plan["ledger"], campaign_id=CAMPAIGN, spend_ceiling_usd=SPEND_CEILING)
    with FileLock(plan["ledger"] + ".driver.lock", timeout=0):
        book = ledger.snapshot()
        if any(c["blocked"] for c in book.get("campaigns", {}).values()):
            raise BudgetStop("shared campaign blocked")
        registered = {key for key, value in book["runs"].items() if value.get("campaign_id")}
        if any(r["status"] == "pending" and r["run_id"] in registered for r in book["requests"]):
            raise BudgetStop("shared campaign has an unsettled request")
        if book["runs"].get(identifier, {}).get("trial_started") or any(r["run_id"] == identifier for r in book["requests"]):
            raise BudgetStop("frozen EVO run already started; no retries")
        ledger.register_run(identifier, max_requests=64, time_cap=1800)
        with ledger.transaction() as data:
            data["runs"][identifier]["trial_started"] = True
        env, agent = environment or EvoDockerTask(instance_id), None
        root.mkdir(parents=True)
        write_json(root / "started.json", {"run_id": identifier, "instance_id": instance_id, "arm": arm,
                   "plan_sha256": expected_sha, "container_name": getattr(env, "name", None)})
        terminal, report = {}, {"run_id": identifier, "instance_id": instance_id, "arm": arm,
                               "status": "environment_setup", "official_reward": None}
        failure = None
        try:
            await env.start()
            if not env.baseline.get("tree_equal"):
                raise SetupError("baseline_export_not_verified")
            guarded = GuardedLLM(create_live_llm(max_output_tokens=4096, timeout=120), ledger, identifier)
            deadline = asyncio.get_running_loop().time() + 1800

            @tool(is_parallel_safe=False)
            async def shell(command: str) -> str:
                """Run a command in the offline baseline repository /testbed, within remaining run time."""
                remaining = min(ledger.remaining_time(identifier), deadline - asyncio.get_running_loop().time())
                if remaining <= 0:
                    raise BudgetStop("EVO generation time cap reached")
                env.shell_timeout = min(120, remaining)
                return json.dumps(await env.exec(command), ensure_ascii=False)

            store = ArtifactStore(root / "artifacts")
            agent = Agent(llm=guarded, tools=[shell, *make_artifact_tools(store)],
                session=Session(path=root / "session.jsonl"),
                system_prompt="Implement the requested change in /testbed using shell. The container has no network. Use visible repository tests as appropriate. Finish after editing files; the host extracts your diff.",
                context_budget=32000, artifact_store=store if arm == "on" else None,
                memory_dir=False, task_store=False, skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[],
                retry_policy=AutoRetryPolicy(max_retries=0), run_limits=RunLimits(max_turns=64, max_seconds=1800))

            def record(event):
                if isinstance(event, AgentEnd):
                    terminal.update(stop_reason=event.stop_reason, limit_details=event.limit_details)

            agent.subscribe(record)
            report["schema_sha256"] = hashlib.sha256(json.dumps(agent.registry.get_schemas(), sort_keys=True).encode()).hexdigest()
            safe = _json(Path(plan_root) / task["metadata"])
            await asyncio.wait_for(agent.run(safe["problem_statement"]), timeout=1800)
            prediction = {instance_id: {"model_patch": await env.patch()}}
            write_json(root / "preds.json", prediction)
            report.update(status=terminal.get("stop_reason") or "missing_terminal_event",
                          prediction_sha256=sha(root / "preds.json"))
        except BaseException as exc:
            failure = exc
            report.update(status=type(exc).__name__, error_code=getattr(exc, "error_code", type(exc).__name__))
        finally:
            cleanup = []
            for label, target in (("agent", agent), ("container", env)):
                try:
                    if target is not None:
                        await (target.aclose() if label == "agent" else target.close())
                except BaseException as exc:
                    cleanup.append(label + ":" + type(exc).__name__)
            requests = [r for r in ledger.snapshot()["requests"] if r["run_id"] == identifier]
            known = bool(requests) and all(r["cost_micro_usd"] is not None for r in requests)
            report.update(plan_sha256=expected_sha, model=LIVE_MODEL, limits=plan["limits"], baseline=env.baseline,
                          container_name=getattr(env, "name", None),
                          image_id=PINS[instance_id]["image_id"], base_commit=PINS[instance_id]["base_commit"],
                          metadata_sha256=task["metadata_sha256"], source_revision=REVISION,
                          source_sha256=SOURCE_SHA256, artifact_projection=arm,
                          requests=len(requests), usage_available=known,
                          cost_usd=sum(r["cost_micro_usd"] for r in requests) / 1e6 if known else None,
                          cache_tokens=sum(r["usage"]["cache_read_tokens"] for r in requests)
                          if known and all("cache_read_tokens" in r["usage"] for r in requests) else None,
                          cleanup_errors=cleanup, **terminal)
            write_json(root / "run.json", report)
        if failure is not None:
            if isinstance(failure, (BudgetStop, asyncio.CancelledError)):
                raise failure
            raise RuntimeError("EVO solver stopped; inspect sanitized run.json") from None
        return report


def _scorer_snapshot(plan, destination):
    """Copy verified original bytes only, with no inherited Python caches."""
    proofs = _scorer_hashes(plan["scorer_root"])
    destination.mkdir()
    copied = {}
    for source, expected in proofs.items():
        data = Path(source).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("scorer changed during snapshot creation")
        relative = Path(source).relative_to(plan["scorer_root"])
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        copied[relative.as_posix()] = expected
    data = _no_links(plan["cache_adapter"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != plan["cache_adapter_sha256"]:
        raise ValueError("cache adapter changed before scoring")
    adapter = destination / "_evo_cache_adapter.py"
    with adapter.open("xb") as stream:
        stream.write(data)
    copied[adapter.name] = plan["cache_adapter_sha256"]
    write_json(destination.parent / "scorer-snapshot.json", {
        "files": copied, "interpreter": os.sys.executable,
        "trust_boundary": "Installed Python and third-party site-packages remain trusted; this is not a code sandbox.",
        "inherited_bytecode_copied": False,
    })
    return adapter


def _official_score_report(root, identifier, instance_id):
    expected = root / "logs" / "run_evaluation" / identifier / identifier / instance_id / "report.json"
    try:
        _no_links(expected)
        paths = list(root.glob("**/report.json"))
        if paths != [expected]:
            raise ValueError("official report path/count differs")
        raw = _json(expected)
        if not isinstance(raw, dict) or set(raw) != {instance_id} or not isinstance(raw[instance_id], dict):
            raise ValueError("official report instance differs")
        return [{"path": str(expected), "sha256": sha(expected), "raw": raw}], None
    except (OSError, ValueError) as exc:
        return [], type(exc).__name__


def score(plan_root, *, expected_sha, instance_id, generation, output):
    """Independent fresh official-fork evaluation; never uses the solver container."""
    if os.name != "posix":
        raise ValueError("EVO official scoring requires the prepared WSL runtime")
    plan = verify_plan(plan_root, expected_sha)
    _selected(plan, instance_id)
    generation, root = _no_links(generation).resolve(), _no_links(output).resolve()
    report = _json(generation / "run.json")
    if (report["instance_id"] != instance_id or report["plan_sha256"] != expected_sha
            or report["run_id"] != run_id(instance_id, report["arm"])
            or sha(generation / "preds.json") != report.get("prediction_sha256")):
        raise ValueError("generation association or prediction checksum differs")
    if root.exists():
        raise ValueError("official scoring output must be new; never rerun a started trial")
    root.mkdir(parents=True)
    row = next(r for r in _load_rows(plan["source"]) if r["instance_id"] == instance_id)
    (root / "output_final").mkdir()
    write_json(root / "output_final" / (instance_id + ".json"), row)
    trajectory = root / report["run_id"]
    trajectory.mkdir()
    write_json(trajectory / "preds.json", _json(generation / "preds.json"))
    adapter = _scorer_snapshot(plan, root / "_scorer")
    env = _clean_env()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["EVO_SCORER_ROOT"] = str(root / "_scorer")
    command = [os.sys.executable, "-I", "-B", str(adapter), "--instance", instance_id, "--scaffold", "SWE-agent",
               "--max_workers", "1", "--trajectories_path", str(trajectory)]
    name = "sweb.eval." + instance_id.lower() + "." + report["run_id"]
    process, failure = None, None
    try:
        with (root / "official-cli.log").open("x") as log:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            process.wait(timeout=1200)
    except BaseException as exc:
        failure = type(exc).__name__
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        cleanup_error = None
        try:
            cleanup = subprocess.run(["docker", "rm", "-f", name], env=env, capture_output=True, timeout=30, check=False)
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                cleanup_error = "owned_container_cleanup_failed"
        except Exception as exc:
            cleanup_error = type(exc).__name__
        results, rejection = _official_score_report(root, report["run_id"], instance_id)
        result = {"instance_id": instance_id, "run_id": report["run_id"], "model_requests": 0,
                  "status": "official_report_available" if len(results) == 1 else "environment_blocked",
                  "error_type": failure, "cleanup_error": cleanup_error,
                  "report_rejection": rejection, "process_exit_code": process.returncode if process else None,
                  "scorer_snapshot_sha256": sha(root / "scorer-snapshot.json"),
                  "official_cli_sha256": sha(root / "official-cli.log"),
                  "reports": results}
        write_json(root / "score.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    frozen = sub.add_parser("freeze")
    for name in ("source", "preflight", "output", "ledger", "scorer-root"):
        frozen.add_argument("--" + name, required=True)
    frozen.add_argument("--cache-adapter", help="Defaults to the bundled portable verified-cache adapter")
    for command in ("solve", "probe", "score"):
        p = sub.add_parser(command)
        for name in ("plan-root", "expected-sha", "instance-id", "output"):
            p.add_argument("--" + name, required=True)
        if command == "solve":
            p.add_argument("--arm", choices=("on", "off"), required=True)
            p.add_argument("--execute", action="store_true")
            p.add_argument("--resume", action="store_true")
        if command == "score":
            p.add_argument("--generation", required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "freeze":
        result = freeze(**args)
    elif command == "score":
        result = score(**args)
    elif command == "probe":
        result = asyncio.run(probe(**args))
    elif args.pop("execute"):
        result = asyncio.run(solve(**args))
    else:
        result = {"preview": True, "tasks": verify_plan(args["plan_root"], args["expected_sha"])["tasks"]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
