"""Independent authorized RSI campaign host. Import/CLI preflight never calls a model.

The journal is the payment boundary, not best-effort LLM observers. Never reuse
or reconcile the historical campaign here. Started trials cannot be replayed;
unsettled/unknown requests stop the entire new campaign, including new trial IDs.
This is a serial trusted-host experiment, not an OS sandbox or a transactional
filesystem apply mechanism. Dispatch requires a frozen, verified task/slot plan.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import doctest
import hashlib
import json
import os
from pathlib import Path
import random
import re
import time

from filelock import FileLock

from my_coding_agent.rsi_runtime import ParentBudget, RsiBudgetExceeded

PROTOCOL_ID = "rsi-humaneval-calibration-v2-turn12"
ATTEMPT_MAX_TURNS = 12
LIMITS = {"max_requests": 64, "max_tokens": 1000000, "max_seconds": 1800, "max_evaluations": 6}
MODEL = {"provider": "openai", "model": "deepseek-v4.1-flash",
         "base_url": "https://opencode.ai/inference/go/openai/v1", "max_tokens": 4096,
         "timeout": 180, "max_retries": 0}
CONTEXT_COMPACTION = {"model_context_window": 32000, "output_reserve": MODEL["max_tokens"],
                      "safety_margin": 512, "summary_max_tokens": 1024}
PROJECT = Path(__file__).resolve().parents[2]


def _model_secret_names():
    prefixes = ("LIVE_BENCH_", "OPENAI_", "DEEPSEEK_", "OPENCODE_", "ANTHROPIC_", "GEMINI_", "AZURE_OPENAI_")
    return [name for name, value in os.environ.items() if value and name.upper().startswith(prefixes)
            and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH"))]


def _assert_clean_model_environment():
    if _model_secret_names():
        raise JournalError("model credential environment must be sanitized before library dispatch")


def _scrub_cli_model_environment():
    # CLI owns this process. Library functions never mutate global environment.
    for name in _model_secret_names():
        os.environ.pop(name, None)


class JournalError(RuntimeError):
    """Fail closed without disclosing provider messages or credentials."""


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("bounded plain campaign/trial identity required")
    return value


def _exclusive_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    if os.name == "posix":
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def _execution_fingerprint():
    paths = list((PROJECT / "src").rglob("*.py"))
    paths += [PROJECT / "pyproject.toml", PROJECT / "uv.lock", Path(__file__).resolve(),
              Path(__file__).with_name("live_tasks.py")]
    return {path.relative_to(PROJECT).as_posix(): _sha(path.read_bytes())
            for path in sorted(set(paths)) if path.is_file()}


class Campaign:
    """Fsynced append-only hash chain with a serial interprocess lock.

    A truncated/corrupted tail is never truncated or repaired automatically.
    Started trials and pending reservations remain in place for manual review.
    Hashes detect accidental mutation, not a hostile host rewriting all files.
    """
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "campaign.json"
        self.path = self.root / "journal.jsonl"
        self.lock = FileLock(str(self.root / "journal.lock"), timeout=10)
        try:
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.manifest_sha = _sha(_bytes(self.manifest))
            if (self.manifest["schema_version"] != 1 or self.manifest["limits"] != LIMITS
                    or self.manifest["model"] != MODEL or self.manifest["protocol_id"] != PROTOCOL_ID
                    or self.manifest["attempt_max_turns"] != ATTEMPT_MAX_TURNS
                    or self.manifest["context_compaction"] != CONTEXT_COMPACTION):
                raise ValueError
            _identity(self.manifest["campaign_id"])
        except (OSError, ValueError, KeyError, TypeError):
            raise JournalError("invalid frozen campaign") from None
        self.events()

    @classmethod
    def freeze(cls, root, *, campaign_id, legacy_ledger, legacy_sha256=None):
        root = Path(root).resolve()
        # mkdir first: never even open legacy credentials/data on an overwrite.
        root.mkdir(parents=True, exist_ok=False)
        legacy = Path(legacy_ledger).resolve()
        if legacy_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", legacy_sha256):
            raise ValueError("explicit historical ledger SHA256 required")
        manifest = {"schema_version": 1, "campaign_id": _identity(campaign_id), "created_at": time.time(),
            "authorization": {"date": "2026-11-14", "claimed_tokens": 2_000_000_000,
                "provider_verified": False, "source": "Explicit user authorization for new real-model experiments",
                "quota_claim_is_not_service_balance": True},
            "historical_campaign": {"ledger_path": str(legacy), "ledger_sha256": legacy_sha256 or _sha(legacy.read_bytes()),
                "requests": 533, "known_usd": "1.218608", "unknown_reserved_usd": "0.045131",
                "status": "untouched_reference_only_not_reconciled_or_released"},
            "protocol_id": PROTOCOL_ID, "limits": dict(LIMITS), "model": dict(MODEL),
            "attempt_max_turns": ATTEMPT_MAX_TURNS, "context_compaction": dict(CONTEXT_COMPACTION),
            "execution_source": _execution_fingerprint(),
            "currency": None, "pricing": None, "retries": "No started trial or unknown request may be retried with another ID"}
        _exclusive_json(root / "campaign.json", manifest)
        genesis = {"sequence": 0, "previous": None, "manifest_sha256": _sha(_bytes(manifest)),
                   "event": "campaign_frozen", "time": time.time()}
        genesis["sha256"] = _sha(_bytes(genesis))
        with (root / "journal.jsonl").open("xb") as stream:
            stream.write(_bytes(genesis) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(root)
        _sync_directory(root.parent)
        return cls(root)

    def _read(self):
        try:
            current = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if _sha(_bytes(current)) != self.manifest_sha:
                raise ValueError
            raw = self.path.read_bytes()
            if not raw or not raw.endswith(b"\n"):
                raise ValueError
            rows, previous = [], None
            for sequence, line in enumerate(raw.splitlines()):
                row = json.loads(line)
                digest = row.pop("sha256")
                if (row["sequence"] != sequence or row["previous"] != previous
                        or row["manifest_sha256"] != self.manifest_sha or _sha(_bytes(row)) != digest):
                    raise ValueError
                row["sha256"] = digest
                rows.append(row)
                previous = digest
            if rows[0]["event"] != "campaign_frozen":
                raise ValueError
            return rows
        except (OSError, ValueError, KeyError, TypeError):
            raise JournalError("journal/manifest incomplete or changed; manual review required") from None

    def events(self):
        with self.lock:
            return self._read()

    def assert_execution_pin(self):
        if _execution_fingerprint() != self.manifest["execution_source"]:
            raise JournalError("execution source differs from frozen authorization; never continue this plan")

    @staticmethod
    def _state(rows):
        trials, requests = {}, {}
        for row in rows:
            event = row["event"]
            if event == "trial_started":
                trials[row["trial_id"]] = {"status": "started", "metadata": row["metadata"]}
            elif event == "trial_finished":
                trials[row["trial_id"]]["status"] = row["status"]
            elif event == "request_reserved":
                requests[row["request_id"]] = {"trial_id": row["trial_id"], **row["record"]}
            elif event == "request_settled":
                requests[row["request_id"]].update(row["record"])
        return trials, requests

    def _append(self, event, *, check=None, **payload):
        with self.lock:
            rows = self._read()
            if check is not None:
                check(*self._state(rows))
            row = {"sequence": len(rows), "previous": rows[-1]["sha256"], "manifest_sha256": self.manifest_sha,
                   "event": event, "time": time.time(), **payload}
            row["sha256"] = _sha(_bytes(row))
            # Serialization must succeed before opening the append handle.
            data = _bytes(row) + b"\n"
            with self.path.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return row

    @staticmethod
    def _blocked(requests):
        return any(r["status"] != "settled" or r.get("policy_violation") for r in requests.values())

    def start_trial(self, trial_id, *, metadata):
        _identity(trial_id)
        self.assert_execution_pin()
        if (self.root / "plan.json").exists():
            plan, _ = verify_plan(self)
            matches = [slot for slot in plan["slots"] if slot["trial_id"] == trial_id]
            if len(matches) != 1 or any(metadata.get(key) != matches[0][key] for key in ("task_id", "split", "arm", "seed")):
                raise JournalError("trial must match its exact frozen planned slot")
        def check(trials, requests):
            if (trial_id in trials or self._blocked(requests)
                    or any(t["status"] != "completed" for t in trials.values())):
                raise JournalError("started trial or unsettled/unknown usage; no retry under any ID")
        return self._append("trial_started", check=check, trial_id=trial_id, metadata=metadata)

    def record_reservation(self, trial_id, record):
        self.assert_execution_pin()
        request_id = f"{trial_id}:{record['request']}"
        def check(trials, requests):
            if (trials.get(trial_id, {}).get("status") != "started"
                    or self._blocked(requests) or request_id in requests):
                raise JournalError("campaign cannot dispatch this reservation")
        return self._append("request_reserved", check=check, trial_id=trial_id, request_id=request_id, record=dict(record))

    def record_settlement(self, trial_id, record, raw_usage):
        request_id = f"{trial_id}:{record['request']}"
        def check(trials, requests):
            if requests.get(request_id, {}).get("status") != "pending":
                raise JournalError("reservation missing or already settled")
        return self._append("request_settled", check=check, trial_id=trial_id, request_id=request_id,
                            record=dict(record), raw_usage=raw_usage,
                            usage_source="Response.usage_provider_normalized_not_raw_SDK_invoice", currency=None)

    def finish_trial(self, trial_id, *, status, evidence):
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError("invalid terminal trial status")
        def check(trials, requests):
            if trials.get(trial_id, {}).get("status") != "started":
                raise JournalError("trial is not started; never overwrite a terminal trial")
            if status == "completed" and self._blocked(requests):
                raise JournalError("unknown usage cannot complete a trial")
        return self._append("trial_finished", check=check, trial_id=trial_id, status=status, evidence=evidence)

    def summary(self):
        rows = self.events()
        trials, requests = self._state(rows)
        return {"campaign_id": self.manifest["campaign_id"], "currency": None, "cost": None,
            "trials": trials, "requests": len(requests), "used_tokens": sum(r.get("used_tokens") or 0 for r in requests.values()),
            "held_tokens": sum(r["reserved_tokens"] for r in requests.values() if r["status"] != "settled"),
            "pending_requests": sum(r["status"] == "pending" for r in requests.values()),
            "unknown_requests": sum(r["status"] == "unknown" for r in requests.values()),
            "failed_trials": sum(t["status"] in ("failed", "cancelled") for t in trials.values()),
            "blocked": self._blocked(requests) or any(t["status"] != "completed" for t in trials.values()),
            "journal_sha256": rows[-1]["sha256"], "historical_campaign": self.manifest["historical_campaign"]}

    def resume(self):
        summary = self.summary()
        return {"action": "blocked_manual_review" if summary["blocked"] else "ready_for_unstarted_trial_only",
                "automatic_retry": False, "summary": summary}


class JournaledParentBudget(ParentBudget):
    """Public accounting seam; raw LLM type and RSI provenance stay unchanged."""
    def __init__(self, campaign, trial_id, *, verify_task):
        self.campaign, self.trial_id = campaign, _identity(trial_id)
        if not callable(verify_task):
            raise JournalError("mandatory prepared-task verifier required")
        self.verify_task = verify_task
        self.quota_stop = None
        if campaign.summary()["trials"].get(trial_id, {}).get("status") != "started":
            raise JournalError("start the trial durably before constructing its budget")
        super().__init__(**campaign.manifest["limits"], max_output_tokens=campaign.manifest["model"]["max_tokens"])

    def _verify_dispatch(self):
        try:
            self.campaign.assert_execution_pin()
            _assert_clean_model_environment()
            self.verify_task()
        except BaseException:
            self.blocked = True
            raise JournalError("pre-dispatch task/source/environment guard failed; manual review required") from None

    def reserve(self, messages, tools, max_output, purpose):
        self._verify_dispatch()
        try:
            record = super().reserve(messages, tools, max_output, purpose)
        except RsiBudgetExceeded as exc:
            self._record_quota_stop("provider_reservation", exc)
            raise
        try:
            self.campaign.record_reservation(self.trial_id, record)
        except BaseException:
            self.blocked = True
            raise JournalError("reservation persistence failed; provider dispatch forbidden") from None
        return record

    def begin_evaluation(self):
        self._verify_dispatch()
        try:
            return super().begin_evaluation()
        except RsiBudgetExceeded as exc:
            self._record_quota_stop("evaluation_allowance", exc)
            raise

    def _record_quota_stop(self, stage, error):
        known_quota = {"parent provider request allowance exhausted", "parent token reservation exhausted",
                       "parent deadline exhausted", "parent visible evaluation allowance exhausted"}
        if str(error) in known_quota and not self.blocked and self.snapshot()["usage_complete"]:
            self.quota_stop = {"source": "trusted_parent_budget_guard", "stage": stage}
            try:
                self.campaign._append("budget_stopped", trial_id=self.trial_id, evidence=self.quota_stop)
            except BaseException:
                self.blocked = True
                raise JournalError("budget stop persistence failed; manual review required") from None

    def settle(self, record, usage, *, error=None):
        failure = None
        try:
            super().settle(record, usage, error=error)
        except RsiBudgetExceeded as exc:
            failure = exc
        try:
            self.campaign.record_settlement(self.trial_id, record, usage)
        except BaseException:
            self.blocked = True
            raise JournalError("settlement persistence failed; reservation remains unresolved") from None
        if failure is not None:
            raise failure


def preflight(campaign):
    """Static artifact/recovery gate; never load credentials, execute or optimize."""
    result = {"campaign_id": campaign.manifest["campaign_id"], "model": dict(MODEL), "limits": dict(campaign.manifest["limits"]),
        "protocol_id": campaign.manifest["protocol_id"], "attempt_max_turns": campaign.manifest["attempt_max_turns"],
        "context_compaction": dict(campaign.manifest["context_compaction"]),
        "network_calls": 0, "ready_to_dispatch": False, "task_schema": "PreparedTask-v1",
        "phase": "needs_training", "not_ready_reason": None, "recovery": None,
        "credential_values": "not_read_or_reported", "credential_check": "not_performed",
        "readiness_scope": "frozen_task_artifacts_source_and_journal_only"}
    try:
        campaign.assert_execution_pin()
        result["recovery"] = campaign.resume()
        if not (campaign.root / "plan.json").is_file():
            result["not_ready_reason"] = "no_frozen_plan"
            return result
        plan, tasks = verify_plan(campaign)
        for task in tasks.values():
            task.verify()
        trials = result["recovery"]["summary"]["trials"]
        remaining = [slot for slot in plan["slots"] if trials.get(slot["trial_id"], {}).get("status") != "completed"]
        collection_pending = any(slot["phase"] == "collection" for slot in remaining)
        result.update(verified_tasks=len(tasks), planned_trials=len(plan["slots"]), remaining_trials=len(remaining),
                      next_trial=remaining[0]["trial_id"] if remaining else None)
        rows = campaign.events()
        started = [row for row in rows if row["event"] == "optimizer_started"]
        finished = [row for row in rows if row["event"] == "optimizer_finished"]
        result["phase"] = "needs_training" if collection_pending else "optimization"
        if finished:
            if len(started) != 1 or len(finished) != 1 or collection_pending:
                raise JournalError("optimizer phase evidence inconsistent")
            policy = json.loads((campaign.root / "research-policy.json").read_text(encoding="utf-8"))
            if finished[0]["policy_sha256"] != _sha(_bytes(policy)):
                raise JournalError("frozen research policy changed")
            result["phase"] = "testing" if remaining else "complete"
        if result["recovery"]["summary"]["blocked"]:
            result["not_ready_reason"] = "campaign_recovery_blocked"
        elif started and not finished:
            result["not_ready_reason"] = "optimizer_started_without_terminal_evidence"
        elif not finished and (campaign.root / "research-policy.json").exists():
            result["not_ready_reason"] = "optimizer_artifact_without_terminal_evidence"
        elif result["phase"] == "complete":
            result["not_ready_reason"] = "all_slots_complete"
        elif not remaining:
            result["not_ready_reason"] = "missing_optimizer_terminal_evidence"
        else:
            result["ready_to_dispatch"] = True
    except Exception as error:
        result.update(ready_to_dispatch=False, not_ready_reason="invalid_frozen_plan_or_task_material",
                      error_type=type(error).__name__)
    return result


def construct_live_llm():
    """Explicit host-only construction; never serialize Config or pass keys to tools."""
    from my_agent_llm import Config, LLM
    from my_agent_llm.auth.manager import AuthManager
    from my_agent_llm.auth.schema import ApiKeyCredential
    from my_coding_agent.paths import AgentPaths
    key = os.environ.get("LIVE_BENCH_API_KEY")
    if not key:
        credential = AuthManager(auth_path=AgentPaths().auth_path).get_credential("openai")
        if isinstance(credential, ApiKeyCredential) and credential.base_url == MODEL["base_url"]:
            key = credential.resolve_key()
    if not key:
        raise JournalError("explicit configured provider credential unavailable")
    return LLM(Config(**MODEL, api_key=key))


def _episode_integrity(report, budget):
    """Host completion is separate from solving the task or exhausting quota."""
    if budget.blocked or not budget.snapshot()["usage_complete"] or report["cleanup_errors"]:
        return False
    status = report["status"]
    if status in ("completed", "budget_exhausted"):
        return True
    if status == "invalid_attempt":
        attempts = report.get("attempts", [])
        return bool(attempts and (attempts[-1].get("agent_terminal") or {}).get("stop_reason") == "budget_exhausted")
    return status == "invalid" and budget.quota_stop is not None


def _branch_factory(campaign, *, source, output, mode):
    """Consume only the frozen protocol's child turns and context reserves."""
    from my_agent_core.context_compaction import CompactionPolicy
    from my_coding_agent.rsi_branch import LocalBranchFactory
    return LocalBranchFactory(source, output, attempt_max_turns=campaign.manifest["attempt_max_turns"],
        artifact_projection=mode == "layered",
        compaction_policy=CompactionPolicy(mode=mode, **campaign.manifest["context_compaction"]))


async def _postselection_evaluate(command, tree, budget):
    from my_coding_agent.rsi_branch import VisibleCommandEvaluator
    try:
        return await VisibleCommandEvaluator(command).evaluate(tree, budget, None)
    except RsiBudgetExceeded:
        if budget.blocked or budget.quota_stop is None or not budget.snapshot()["usage_complete"]:
            raise
        return {"valid": False, "score": None, "status": "not_run_parent_quota_exhausted"}


async def run_trial(*, campaign, trial_id, task_id, split, source, goal, visible_command,
                    final_command, final_evaluator_id, policy_parameters, mode, schedule, seed=23, llm=None,
                    metadata_extra=None, verify_task=None, task_evidence=None):
    """Single real trial API; callers freeze task inputs before explicit dispatch.

    Task-file/queue schemas are deliberately not guessed here. No model calls
    occur until a caller explicitly invokes this function. Independent final
    feedback is persisted separately and never supplied to the controller.
    """
    from my_agent_core.session import Session
    from my_agent_llm import LLM
    from my_coding_agent.agent import CodingAgent
    from my_coding_agent.rsi_branch import VisibleCommandEvaluator, copy_tree, file_sha, write_json
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime, execution_scope
    _identity(trial_id)
    _assert_clean_model_environment()
    if not callable(verify_task):
        raise JournalError("mandatory prepared-task verifier required")
    plan, planned_tasks = verify_plan(campaign)
    slot = next((s for s in plan["slots"] if s["trial_id"] == trial_id), None)
    if (slot is None or slot["task_id"] != task_id or slot["split"] != split
            or (metadata_extra or {}).get("arm") != slot["arm"] or seed != slot["seed"]):
        raise JournalError("unregistered or mismatched trial slot")
    pinned_task = planned_tasks[task_id]
    expected_mode = "baseline" if slot["arm"] == "A" else "layered"
    expected_schedule = "random" if slot["arm"] == "random" else "rsi" if slot["arm"] == "C" else "fixed"
    if mode != expected_mode or schedule != expected_schedule:
        raise JournalError("mode/schedule differs from frozen slot")
    if (Path(source).resolve() != pinned_task.source_dir.resolve() or goal != pinned_task.goal
            or tuple(visible_command) != pinned_task.visible_argv or tuple(final_command) != pinned_task.final_argv
            or final_evaluator_id != pinned_task.host_evaluator_sha):
        raise JournalError("trial arguments differ from frozen prepared task")
    def verify_inputs():
        pinned_task.verify()
        verify_task()
    verify_inputs()
    if not isinstance(goal, str) or not goal or mode not in ("baseline", "layered"):
        raise ValueError("explicit text goal and context mode required")
    if not isinstance(final_evaluator_id, str) or not final_evaluator_id or tuple(final_command) == tuple(visible_command):
        raise ValueError("independent final evaluator identity and distinct fixed argv required")
    raw = llm if llm is not None else construct_live_llm()
    if type(raw) is not LLM or any(getattr(raw.config, key, None) != value for key, value in MODEL.items()):
        raise JournalError("exact builtin LLM and frozen live provider profile required")
    policy = FrozenPolicy(policy_parameters)
    campaign.start_trial(trial_id, metadata={"task_id": task_id, "split": split, "mode": mode,
        "schedule": schedule, "seed": seed, "policy": policy.snapshot(), "final_evaluator_id": final_evaluator_id,
        **(metadata_extra or {})})
    budget = JournaledParentBudget(campaign, trial_id, verify_task=verify_inputs)
    agent = None
    try:
        factory = _branch_factory(campaign, source=source, output=campaign.root / "trials" / trial_id, mode=mode)
        runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator(visible_command), budget=budget,
            policy=policy, task_id=task_id, split=split, arm="host", schedule=schedule, seed=seed, max_attempts=4,
            task_evidence=task_evidence)
        agent = CodingAgent(workspace=source, llm=raw, session=Session(path=factory.output / "parent.jsonl"),
            rsi_runtime=runtime, auto_load_mcp=False, memory_dir=False, task_store=False,
            skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[])
        await agent.run(goal)
        result = agent.last_rsi_result
        integrity = _episode_integrity(result.report, budget)
        final = None
        if integrity and result.selected_path is not None:
            final_tree = factory.output / "independent-final-tree"
            copy_tree(result.selected_path, final_tree)
            with execution_scope(stage="independent_final_test"):
                final = await _postselection_evaluate(final_command, final_tree, budget)
            integrity = integrity and not budget.blocked and (bool(final["valid"]) or budget.quota_stop is not None)
        evidence = {"trial_id": trial_id, "task_id": task_id, "split": split,
            "protocol_id": campaign.manifest["protocol_id"], "attempt_max_turns": campaign.manifest["attempt_max_turns"],
            "provenance": result.report["provenance"], "execution_integrity": integrity,
            "episode_execution_status": "completed" if integrity else "failed",
            "agent_task_status": result.report["status"], "quota_stop": budget.quota_stop,
            "final_evaluator_id": final_evaluator_id, "selection_frozen": True, "final": final,
            "final_success": bool(integrity and final is not None and final["valid"] and final["score"] == 1),
            "runtime_result_sha256": file_sha(factory.output / "result.json"),
            "runtime_final_success_unchanged": result.final_success, "budget": budget.snapshot(), "currency": None}
        write_json(factory.output / "final-evaluation.json", evidence)
        await agent.aclose()
        agent = None
        campaign.finish_trial(trial_id, status="completed" if integrity else "failed", evidence={
            "final_evaluation_sha256": file_sha(factory.output / "final-evaluation.json"), "budget": budget.snapshot()})
        return {"result": result, "final_evaluation": evidence}
    except BaseException as exc:
        campaign.finish_trial(trial_id, status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            evidence={"error_type": type(exc).__name__, "budget": budget.snapshot()})
        raise
    finally:
        if agent is not None:
            await agent.aclose()


async def run_prepared_trial(*, campaign, trial_id, task, arm, seed=23, policy_parameters=None, llm=None):
    """Adapter boundary for the separately owned, verified HumanEval manifest."""
    _assert_clean_model_environment()
    if arm not in ("A", "B", "C", "random"):
        raise ValueError("unknown preregistered comparison arm")
    task.verify()
    from my_coding_agent.dream_rsi_contract import make_task_evidence
    public_input = (task.source_dir / "solution.py").read_bytes().decode("utf-8")
    function = next(node for node in ast.parse(public_input).body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == task.entry_point)
    examples = doctest.DocTestParser().get_examples(ast.get_docstring(function) or "")
    visible_spec = _bytes({"stage": "visible", "method": "all official entry-point doctest examples must pass",
        "entry_point": task.entry_point, "examples": [{"source": e.source, "want": e.want,
            "exc_msg": e.exc_msg, "options": e.options} for e in examples]}).decode("utf-8")
    task_evidence = make_task_evidence(public_input=public_input, visible_spec=visible_spec)
    if arm == "C" and policy_parameters is None:
        raise ValueError("C requires an explicit frozen train-selected policy")
    parameters = policy_parameters if arm == "C" else {"max_width": 2, "max_refine": 1}
    return await run_trial(campaign=campaign, trial_id=trial_id, task_id=task.task_id, split=task.split,
        source=task.source_dir, goal=task.goal, visible_command=task.visible_argv, final_command=task.final_argv,
        final_evaluator_id=task.host_evaluator_sha, policy_parameters=parameters,
        mode="baseline" if arm == "A" else "layered", schedule="random" if arm == "random" else "rsi" if arm == "C" else "fixed",
        seed=seed, llm=llm, verify_task=task.verify, task_evidence=task_evidence,
        metadata_extra={"arm": arm, "source_sha": task.source_sha,
            "host_evaluator_sha": task.host_evaluator_sha, "preflight_proof": task.preflight_proof})


def freeze_plan(campaign, task_manifest):
    """Freeze the 5 collection + 48 interleaved test slots before real dispatch."""
    from experiments.context_rsi.live_tasks import load_tasks
    campaign.assert_execution_pin()
    if any(row["event"] == "trial_started" for row in campaign.events()):
        raise JournalError("task plan must precede every trial")
    tasks = load_tasks(task_manifest)
    expected = {"train": {7, 11, 27}, "val": {32, 45}, "test": {1, 6, 9, 20, 33, 40}}
    for split, ids in expected.items():
        if {int(t.task_id.rsplit("/", 1)[-1]) for t in tasks if t.split == split} != ids:
            raise JournalError("task manifest differs from preregistered HumanEval split")
    if len(tasks) != 11:
        raise JournalError("exactly eleven frozen tasks required")
    slots = []
    for task in tasks:
        if task.split != "test":
            slots.append({"trial_id": f"collect-{task.task_id.replace('/', '-')}-s23", "phase": "collection",
                          "task_id": task.task_id, "split": task.split, "arm": "B", "seed": 23})
    # Explicit split ordering prevents any holdout information selecting a train candidate.
    slots.sort(key=lambda slot: (slot["split"] != "train", int(slot["task_id"].rsplit("/", 1)[-1])))
    for task in sorted((t for t in tasks if t.split == "test"), key=lambda t: int(t.task_id.rsplit("/", 1)[-1])):
        for seed in (23, 47):
            arms = ["A", "B", "C", "random"]
            random.Random(f"{seed}:{task.task_id}").shuffle(arms)
            for arm in arms:
                slots.append({"trial_id": f"test-{task.task_id.replace('/', '-')}-s{seed}-{arm}", "phase": "test",
                              "task_id": task.task_id, "split": "test", "arm": arm, "seed": seed})
    manifest = Path(task_manifest).resolve()
    value = {"schema_version": 1, "task_manifest": str(manifest), "task_manifest_sha256": _sha(manifest.read_bytes()),
        "tasks": {t.task_id: {"split": t.split, "source_sha": t.source_sha, "host_evaluator_sha": t.host_evaluator_sha} for t in tasks},
        "slots": slots, "seeds_are_scheduling_repeats_not_model_sampling_controls": True,
        "scope": "Official HumanEval short-code calibration; not long-horizon or strong repository generalization",
        "optimization": {"dry_run": True, "publish": False, "budgets": [1, 2, 4], "max_candidates": 4,
            "research_C": "freeze train-selected candidate even if holdout rejects publication; label rejection explicitly"}}
    _exclusive_json(campaign.root / "plan.json", value)
    _sync_directory(campaign.root)
    campaign._append("plan_frozen", plan_sha256=_sha(_bytes(value)))
    return value


def verify_plan(campaign):
    from experiments.context_rsi.live_tasks import load_tasks
    campaign.assert_execution_pin()
    try:
        plan = json.loads((campaign.root / "plan.json").read_text(encoding="utf-8"))
        markers = [r for r in campaign.events() if r["event"] == "plan_frozen"]
        if len(markers) != 1 or markers[0]["plan_sha256"] != _sha(_bytes(plan)):
            raise ValueError
        manifest = Path(plan["task_manifest"])
        if _sha(manifest.read_bytes()) != plan["task_manifest_sha256"]:
            raise ValueError
        tasks = {task.task_id: task for task in load_tasks(manifest)}
        identities = {name: {"split": task.split, "source_sha": task.source_sha,
                            "host_evaluator_sha": task.host_evaluator_sha} for name, task in tasks.items()}
        if identities != plan["tasks"]:
            raise ValueError
        return plan, tasks
    except (OSError, ValueError, KeyError, TypeError):
        raise JournalError("frozen task plan or task artifacts changed") from None


async def optimize_collection(campaign, plan, tasks):
    """Actual recorded-trace SDK optimizer; no publication or provider calls."""
    from types import SimpleNamespace
    from my_coding_agent.dream_rsi_optimizer import optimize
    from my_coding_agent.rsi_runtime import TaskSplits, export_dataset
    rows = campaign.events()
    completed = [r for r in rows if r["event"] == "optimizer_finished"]
    if completed:
        policy = json.loads((campaign.root / "research-policy.json").read_text(encoding="utf-8"))
        if len(completed) != 1 or completed[0]["policy_sha256"] != _sha(_bytes(policy)):
            raise JournalError("frozen research policy changed")
        return policy
    if any(r["event"] == "optimizer_started" for r in rows):
        raise JournalError("optimizer previously started; no implicit retry or overwrite")
    results = []
    for slot in plan["slots"]:
        if slot["phase"] == "collection":
            directory = campaign.root / "trials" / slot["trial_id"]
            terminal = [r for r in rows if r["event"] == "trial_finished" and r["trial_id"] == slot["trial_id"]]
            final_path = directory / "final-evaluation.json"
            if (len(terminal) != 1 or terminal[0]["status"] != "completed"
                    or _sha(final_path.read_bytes()) != terminal[0]["evidence"]["final_evaluation_sha256"]):
                raise JournalError("collection final evidence differs from durable terminal record")
            final = json.loads(final_path.read_text(encoding="utf-8"))
            if _sha((directory / "result.json").read_bytes()) != final["runtime_result_sha256"]:
                raise JournalError("runtime report differs from separately sealed final evidence")
            report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if report["task_id"] != slot["task_id"] or report["split"] != slot["split"]:
                raise JournalError("collection task identity differs")
            results.append(SimpleNamespace(report=report, trace_path=directory / "trace.json"))
    splits = TaskSplits(*(tuple(t.task_id for t in tasks.values() if t.split == split) for split in ("train", "val", "test")))
    campaign._append("optimizer_started", collection_trials=[s["trial_id"] for s in plan["slots"] if s["phase"] == "collection"])
    manifest = export_dataset(results, splits, campaign.root / "dataset")
    optimized = await asyncio.to_thread(optimize, manifest, campaign.root / "optimizer",
        registry_path=campaign.root / "private-registry", dry_run=True, budgets=(1, 2, 4), max_candidates=4,
        max_workers=1, timeout_seconds=30, max_seconds=300)
    accepted = optimized["validation_gain"] > optimized["min_gain"] and optimized["incumbent_gain"] > optimized["min_gain"]
    policy = {"parameters": optimized["selected"]["parameters"], "policy_hash": optimized["selected"]["policy_hash"],
        "selection": "single_train_selected_candidate", "kind": optimized["kind"], "promoted": False,
        "publication_status": "holdout_accepted_research_only_not_published" if accepted else "rejected_for_publication",
        "validation_gain": optimized["validation_gain"], "incumbent_gain": optimized["incumbent_gain"],
        "research_rule": plan["optimization"]["research_C"], "optimizer_summary_sha256": _sha(_bytes(optimized))}
    _exclusive_json(campaign.root / "research-policy.json", policy)
    campaign._append("optimizer_finished", policy_sha256=_sha(_bytes(policy)), policy=policy)
    return policy


async def run_batch(campaign, *, max_new_trials=1, llm=None):
    if type(max_new_trials) is not int or not 1 <= max_new_trials <= 53:
        raise ValueError("explicit max_new_trials must be in 1..53")
    plan, tasks = verify_plan(campaign)
    if campaign.summary()["blocked"]:
        raise JournalError("started or unknown trial requires manual review; no alternate-ID retry")
    count = 0
    for slot in plan["slots"]:
        status = campaign.summary()["trials"].get(slot["trial_id"])
        if status is not None:
            if status["status"] != "completed":
                raise JournalError("a planned trial failed; preserve its evidence and stop")
            continue
        if count >= max_new_trials:
            break
        plan, tasks = verify_plan(campaign)
        policy = await optimize_collection(campaign, plan, tasks) if slot["phase"] == "test" else None
        await run_prepared_trial(campaign=campaign, trial_id=slot["trial_id"], task=tasks[slot["task_id"]],
            arm=slot["arm"], seed=slot["seed"], policy_parameters=policy["parameters"] if slot["arm"] == "C" else None, llm=llm)
        count += 1
        if campaign.summary()["blocked"]:
            raise JournalError("new unknown usage stops the campaign")
    return {"new_trials": count, "summary": campaign.summary()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--root", type=Path, required=True)
    freeze.add_argument("--campaign-id", required=True)
    freeze.add_argument("--legacy-ledger", type=Path, required=True)
    freeze.add_argument("--legacy-sha256", help="Host-verified historical pointer; does not read/modify old ledger")
    freeze.add_argument("--task-manifest", type=Path, required=True)
    for name in ("preflight", "run", "resume", "summary"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, required=True)
        if name == "run":
            sub.add_argument("--max-new-trials", type=int, default=1)
    args = parser.parse_args(argv)
    campaign = (Campaign.freeze(args.root, campaign_id=args.campaign_id, legacy_ledger=args.legacy_ledger,
                                legacy_sha256=args.legacy_sha256)
                if args.command == "freeze" else Campaign(args.root))
    if args.command == "freeze":
        freeze_plan(campaign, args.task_manifest)
    if args.command in ("freeze", "preflight"):
        result = preflight(campaign)
    elif args.command == "run":
        raw = construct_live_llm()
        _scrub_cli_model_environment()
        result = asyncio.run(run_batch(campaign, max_new_trials=args.max_new_trials, llm=raw))
    elif args.command == "resume":
        result = campaign.resume()
    else:
        result = campaign.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
