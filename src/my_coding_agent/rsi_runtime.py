"""Bounded serial RSI execution. Offline contracts never imply model quality."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import inspect
import json
import math
from pathlib import Path
import random
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

from my_agent_core.run_lifecycle import RunBudget, RunLimits
from my_agent_llm.events import StreamDoneEvent, StreamStartEvent
from my_agent_llm.models import Message, StreamChunk
from my_agent_llm.observation import _CONTEXT

_SCOPE = ContextVar("rsi_scope", default={})


class RsiBudgetExceeded(RuntimeError):
    """No further provider or evaluator work is authorized by this parent."""


class ParentBudget:
    """One request/token reservation pool and RunBudget deadline for all children.

    Input reservations use serialized UTF-8 bytes plus framing, a conservative
    protocol assumption rather than a tokenizer billing guarantee. Unknown or
    cancelled provider calls retain their full reservation and stop the parent.
    """
    def __init__(self, *, max_requests, max_tokens, max_seconds, max_evaluations,
                 max_output_tokens=4096, input_usd_per_million=None, output_usd_per_million=None):
        for value in (max_requests, max_tokens, max_evaluations, max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("positive integer RSI limits required")
        for value in (input_usd_per_million, output_usd_per_million):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("invalid explicit price")
        self.run = RunBudget(RunLimits(max_turns=max_requests, max_seconds=max_seconds))
        self.max_tokens, self.max_evaluations = max_tokens, max_evaluations
        self.max_output_tokens = max_output_tokens
        self.prices = (input_usd_per_million, output_usd_per_million)
        self.root_id, self.records, self.evaluations = uuid4().hex, [], []
        self.blocked = False

    def remaining(self):
        if self.blocked:
            raise RsiBudgetExceeded("parent blocked by unknown provider usage")
        try:
            return self.run.remaining()
        except Exception:
            raise RsiBudgetExceeded("parent deadline exhausted") from None

    def reserve(self, messages, tools, max_output, purpose):
        self.remaining()
        if type(max_output) is not int or not 1 <= max_output <= self.max_output_tokens:
            raise RsiBudgetExceeded("output allowance exceeds parent policy")
        def encode(value):
            if hasattr(value, "model_dump"):
                return value.model_dump()
            raise TypeError("unsupported provider request value")
        raw = json.dumps({"messages": messages, "tools": tools}, default=encode, ensure_ascii=False).encode()
        input_bound = len(raw) + 1024 + 128 * len(messages)
        held = input_bound + max_output
        current = self.snapshot()
        if current["used_tokens"] + current["held_tokens"] + held > self.max_tokens:
            raise RsiBudgetExceeded("parent token reservation exhausted")
        if any(r["status"] == "pending" for r in self.records):
            raise RsiBudgetExceeded("serial parent already has an in-flight provider call")
        try:
            self.run.begin_turn()
        except Exception:
            raise RsiBudgetExceeded("parent provider request allowance exhausted") from None
        record = {"request": len(self.records) + 1, "purpose": purpose, **_SCOPE.get(),
                  "input_bound": input_bound, "max_output": max_output, "reserved_tokens": held,
                  "status": "pending", "usage": None, "used_tokens": None, "cost_usd": None}
        self.records.append(record)
        return record

    def settle(self, record, usage, *, error=None):
        from my_agent_llm.langfuse_export import _usage_details
        fields = {"prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens"}
        valid = isinstance(usage, dict) and all(type(value) is int and value >= 0
                                                for key, value in usage.items() if key in fields)
        normalized = dict(usage) if valid else {}
        # Without a total, all normalized cache components must be explicit.
        # Absence does not mean zero and prompt_tokens may exclude cached input.
        components = {"prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens"}
        if valid and "total_tokens" not in normalized and components <= normalized.keys():
            normalized["total_tokens"] = sum(normalized[key] for key in components)
        details, _, status = _usage_details(normalized)
        valid = valid and status == "complete"
        if error is not None or not valid:
            self.blocked = True
            record.update(status="unknown", error_type=error or "MissingUsage")
            raise RsiBudgetExceeded("provider usage unavailable; reservation retained")
        used = details["total"]
        record.update(status="settled", usage={k: v for k, v in usage.items() if type(v) is int},
                      input_tokens=details["input"], output_tokens=details["output"], used_tokens=used,
                      usage_basis="reported_total_minus_completion" if "total_tokens" in usage else "explicit_all_input_components")
        if all(p is not None for p in self.prices):
            record["cost_usd"] = (details["input"] * self.prices[0] + details["output"] * self.prices[1]) / 1e6
            record["cost_basis"] = "all_reported_input_at_explicit_rate_not_provider_invoice"
        violation = ("completion_exceeds_request_maximum" if details["output"] > record["max_output"]
                     else "reservation_assumption_exceeded" if used > record["reserved_tokens"] else None)
        if violation:
            record["policy_violation"] = violation
            self.blocked = True
            raise RsiBudgetExceeded("known provider usage violated parent policy")

    async def awaited(self, coroutine):
        try:
            remaining = self.remaining()
        except BaseException:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise
        return await asyncio.wait_for(coroutine, timeout=remaining)

    def begin_evaluation(self):
        self.remaining()
        if len(self.evaluations) >= self.max_evaluations:
            raise RsiBudgetExceeded("parent visible evaluation allowance exhausted")
        row = {"evaluation": len(self.evaluations) + 1, **_SCOPE.get(), "status": "started"}
        self.evaluations.append(row)
        return row

    def snapshot(self):
        known = all(r["status"] == "settled" for r in self.records)
        prices = all(r["cost_usd"] is not None for r in self.records)
        return {"root_id": self.root_id, "requests": len(self.records),
                "used_tokens": sum(r["used_tokens"] or 0 for r in self.records),
                "held_tokens": sum(r["reserved_tokens"] for r in self.records if r["status"] != "settled"),
                "reserved_tokens_total": sum(r["reserved_tokens"] for r in self.records),
                "usage_complete": known, "blocked": self.blocked, "evaluations": len(self.evaluations),
                "cost_complete": bool(self.records) and known and prices,
                "cost_usd": sum(r["cost_usd"] for r in self.records) if self.records and known and prices else None,
                "wall_seconds": self.run.clock() - self.run.started,
                "limits": {"max_requests": self.run.limits.max_turns, "max_tokens": self.max_tokens,
                           "max_seconds": self.run.limits.max_seconds, "max_evaluations": self.max_evaluations}}


class _ProjectionMarker:
    """Only a diagnostic rewrite marker; never a provider or cache-edit API."""
    __slots__ = ("_mark",)

    def __init__(self, mark):
        self._mark = mark

    def mark_message_rewrite(self, reason):
        if not isinstance(reason, str) or len(reason) > 256:
            raise ValueError("invalid projection diagnostic reason")
        self._mark(reason)


class SharedLLM:
    """Every supported async provider entry consumes the same parent pool.

    No __getattr__ provider delegation exists. Sync APIs explicitly fail rather
    than escaping accounting. RSI batch execution buffers one response; it does
    not claim incremental provider streaming or parallel execution.
    """
    def __init__(self, raw, budget):
        self._raw, self.budget = raw, budget
        source = getattr(raw, "config", None)
        self.model = getattr(raw, "model", None)
        self.config = SimpleNamespace(model=self.model, max_tokens=getattr(source, "max_tokens", 1024),
                                      provider=getattr(source, "provider", None), max_retries=0)
        diagnostics = (getattr(getattr(raw, "_provider", None), "request_projection", None)
                       or getattr(raw, "request_projection", None))
        marker = getattr(diagnostics, "mark_message_rewrite", None)
        self.request_projection = _ProjectionMarker(marker) if callable(marker) else None

    async def achat(self, messages, *, tools=None, **kwargs):
        purpose = kwargs.pop("purpose", None) or _CONTEXT.get().purpose or "agent"
        max_output = kwargs.get("max_tokens") or self.config.max_tokens or 1024
        kwargs["max_tokens"] = max_output
        record = self.budget.reserve(messages, tools, max_output, purpose)
        try:
            response = await self.budget.awaited(self._raw.achat(messages, tools=tools, **kwargs))
        except BaseException as exc:
            try:
                self.budget.settle(record, None, error=type(exc).__name__)
            except RsiBudgetExceeded:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise RsiBudgetExceeded("provider request failed; reservation retained") from None
        self.budget.settle(record, response.usage)
        return response

    async def astream_events(self, messages, *, tools=None, signal=None, **kwargs):
        if signal is not None and signal.is_cancelled():
            raise asyncio.CancelledError
        yield StreamStartEvent(partial=Message(role="assistant", content=""))
        response = await self.achat(messages, tools=tools, **kwargs)
        yield StreamDoneEvent(message=response.to_message(), usage=response.usage)

    async def achat_stream(self, messages, **kwargs):
        response = await self.achat(messages, **kwargs)
        yield StreamChunk(content=response.content, tool_calls=response.tool_calls,
                          usage=response.usage, finish_reason=response.finish_reason)

    async def astream(self, messages, **kwargs):
        async for chunk in self.achat_stream(messages, **kwargs):
            yield chunk

    def chat(self, *args, **kwargs):
        raise NotImplementedError("RSI synchronous provider calls are not supported")

    def stream(self, *args, **kwargs):
        raise NotImplementedError("RSI synchronous provider calls are not supported")

    def provider_evidence(self):
        provider = getattr(self._raw, "_provider", None)
        evidence = {"client_class": type(self._raw).__module__ + "." + type(self._raw).__name__,
                    "provider_class": type(provider).__module__ + "." + type(provider).__name__, "model": self.model}
        builtin = (type(self._raw).__module__ == "my_agent_llm.client"
                   and type(provider).__module__.startswith("my_agent_llm.providers."))
        for name, item in (("client", type(self._raw)), ("provider", type(provider))):
            try:
                path = Path(inspect.getfile(item))
                evidence[name + "_source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, TypeError):
                builtin = False
        evidence.update(kind="observed" if builtin else "synthetic",
                        request_projection_supported=self.request_projection is not None,
                        native_cache_edits="not_sent",
                        boundary="Host/runtime provenance evidence, not cryptographic proof of a real service.")
        return evidence


@contextmanager
def execution_scope(**scope):
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


class FrozenPolicy:
    """Read an active registry version once, never hot-swap within an episode."""
    def __init__(self, parameters, *, version=None):
        from .dream_rsi_contract import policy_identity, validate_parameters
        value = validate_parameters(parameters)
        self._parameters = (value["max_width"], value["max_refine"])
        self.policy_hash, self.version = policy_identity(value), version

    @property
    def parameters(self):
        return {"max_width": self._parameters[0], "max_refine": self._parameters[1]}

    @classmethod
    def from_registry(cls, path=None):
        from .dream_rsi_registry import PolicyRegistry
        snapshot = PolicyRegistry(path).snapshot()
        if snapshot["parameters"] is None:
            raise ValueError("RSI requires an explicitly published active policy")
        return cls(snapshot["parameters"], version=snapshot["version"])

    def snapshot(self):
        return {"parameters": self.parameters, "policy_hash": self.policy_hash, "registry_version": self.version}


class RsiResult:
    def __init__(self, *, text, selected_visible_score, selected_path, patch_path, changes_path,
                 trace_path, report, factory):
        self.text, self.selected_visible_score = text, selected_visible_score
        self.selected_path, self.patch_path, self.changes_path = selected_path, patch_path, changes_path
        self.trace_path, self.report, self.final_success = trace_path, report, None
        self._factory = factory
        from .rsi_branch import file_sha, inventory
        self._changes_sha = file_sha(changes_path) if changes_path else None
        self._selected_files = inventory(selected_path) if selected_path else None

    def apply_to(self, target):
        """Explicit reviewed application; never invoked by run or selection."""
        from .rsi_branch import file_sha, inventory, plain_path, safe_relative
        target = plain_path(target).resolve()
        if self.changes_path is not None:
            plain_path(self.changes_path)
        if (self.selected_path is None or target != self._factory.source
                or inventory(target) != self._factory.manifest
                or inventory(self.selected_path) != self._selected_files
                or file_sha(self.changes_path) != self._changes_sha):
            raise ValueError("candidate or original workspace changed; application refused")
        changes = json.loads(self.changes_path.read_text(encoding="utf-8"))["changes"]
        for relative in changes:
            path = plain_path(target / safe_relative(relative))
            if not path.resolve().is_relative_to(target):
                raise ValueError("candidate application escaped original workspace")
        for relative, change in changes.items():
            path = target / relative
            if change["after_sha256"] is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((self.selected_path / relative).read_bytes())
        return {"status": "applied", "files": len(changes), "target": str(target)}


class RsiRuntime:
    """Opt-in batch portfolio of real agents, initially serial and non-promoting."""
    def __init__(self, *, factory, evaluator, budget, policy, max_attempts=4, schedule="rsi", seed=23,
                 task_id, split, arm="C"):
        from .dream_rsi_contract import MAX_NODES
        from .rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
        if not isinstance(factory, LocalBranchFactory) or not isinstance(evaluator, VisibleCommandEvaluator):
            raise TypeError("initial RSI runtime requires real local branches and fixed visible command evaluation")
        if not isinstance(policy, FrozenPolicy) or not isinstance(budget, ParentBudget):
            raise TypeError("frozen policy and shared enforced parent budget required")
        if schedule not in ("fixed", "rsi", "random") or type(seed) is not int:
            raise ValueError("invalid scheduling mode/seed")
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_NODES:
            raise ValueError("invalid attempt ceiling")
        if not isinstance(task_id, str) or not task_id or split not in ("train", "val", "test"):
            raise ValueError("explicit task identity and train/val/test split required")
        self.factory, self.evaluator, self.budget, self.policy = factory, evaluator, budget, policy
        self.max_attempts, self.schedule, self.seed = max_attempts, schedule, seed
        self.task_id, self.split, self.arm, self._used = task_id, split, arm, False
        self._task = None
        self._cancel_requested = False

    def abort(self):
        """Cancel this episode, including the currently awaited child/provider."""
        task = self._task
        if task is not None and not task.done():
            self._cancel_requested = True
            task.get_loop().call_soon_threadsafe(task.cancel)

    def planned_nodes(self):
        parameters = self.policy.parameters
        width, depth = parameters["max_width"], parameters["max_refine"]
        if self.schedule != "random":
            return [(b, a) for a in range(depth + 1) for b in range(width)][:self.max_attempts]
        rng, progress, plan = random.Random(self.seed), {}, []
        for _ in range(self.max_attempts):
            choices = [(b, a + 1) for b, a in sorted(progress.items()) if a < depth]
            if len(progress) < width:
                choices.append((len(progress), 0))
            if not choices:
                break
            b, a = rng.choice(choices)
            progress[b] = a
            plan.append((b, a))
        return plan

    async def run(self, prompt, *, llm):
        from dataclasses import asdict, is_dataclass
        from .dream_rsi_contract import validate_trace
        from .rsi_branch import file_sha, write_json
        if self._used:
            raise RuntimeError("RSI runtime is single-use; no implicit retry/resume")
        if not isinstance(prompt, str):
            raise NotImplementedError("initial RSI batch execution accepts text goals only")
        self._used = True
        shared = SharedLLM(llm, self.budget)
        provenance = shared.provider_evidence()
        frozen = self.policy.snapshot()
        plan = self.planned_nodes()
        write_json(self.factory.output / "plan.json", {"task_id": self.task_id, "split": self.split,
            "arm": self.arm, "schedule": self.schedule, "seed": self.seed, "policy": frozen,
            "nodes": plan, "provenance": provenance, "budget": self.budget.snapshot()["limits"],
            "context_policy": asdict(self.factory.compaction_policy) if is_dataclass(self.factory.compaction_policy) else None,
            "artifact_projection": self.factory.artifact_projection})
        baseline, attempts, nodes, feedbacks = None, [], [], {}
        selected, status, failure, cleanup = None, "completed", None, []
        interruption = None
        active = None
        self._task = asyncio.current_task()
        try:
            with execution_scope(stage="baseline"):
                baseline = await self.evaluator.evaluate(self.factory.baseline, self.budget, shared)
            self.factory.verify_baseline()
            for branch, attempt in plan:
                if self.budget.run.turns >= self.budget.run.limits.max_turns:
                    status = "budget_exhausted"
                    break
                self.budget.remaining()
                if self.policy.snapshot() != frozen:
                    raise ValueError("policy changed within episode")
                with execution_scope(stage="attempt", branch=branch, attempt=attempt):
                    active = {"id": f"b{branch}_a{attempt}", "branch": branch, "attempt": attempt,
                              "valid": False, "stage": "started", "agent_terminal": None, "visible": None}
                    attempts.append(active)
                    handle = self.factory.handles.get(branch)
                    if handle is None:
                        handle = await self.budget.awaited(self.factory.create(branch, shared))
                    before = self.budget.snapshot()
                    result = await handle.attempt(prompt, attempt, feedbacks.get(branch), self.budget)
                    if self._cancel_requested or self._task.cancelling():
                        raise asyncio.CancelledError
                    terminal = result["agent_terminal"].get("stop_reason")
                    if self.budget.blocked:
                        raise RsiBudgetExceeded("provider usage unavailable")
                    measured = await self.evaluator.evaluate(handle.workspace, self.budget, shared)
                    valid = terminal == "end_turn" and measured["valid"]
                    evidence = {"id": f"b{branch}_a{attempt}", "branch": branch, "attempt": attempt, "stage": "completed",
                        "agent_terminal": result["agent_terminal"], "visible": measured, "valid": valid,
                        "actions": result["actions"], "budget_before": before, "budget_after": self.budget.snapshot(),
                        "context_snapshots": result["context_snapshots"], "context_activity": result["context_activity"],
                        "context": dict(handle.agent.context_manager.compaction_report),
                        "tool_schema_sha256": hashlib.sha256(json.dumps(handle.agent.registry.get_schemas(), sort_keys=True).encode()).hexdigest()}
                    checkpoint = handle.checkpoint(attempt, evidence)
                    evidence["checkpoint"] = str(checkpoint)
                    active.update(evidence)
                    if valid:
                        cell = {"evaluated": True, "combined_score": measured["score"],
                                "n_valid": int(measured["score"] == 1), "n_total": 1}
                        nodes.append({"id": evidence["id"], "branch": branch, "attempt": attempt,
                            "parent_id": None if attempt == 0 else f"b{branch}_a{attempt - 1}",
                            "path": str(checkpoint), "intent": "real branch attempt", "tags": ["visible_proxy"],
                            "status": "done", "raw": cell})
                        if selected is None or measured["score"] > selected["score"]:
                            selected = {"score": measured["score"], "checkpoint": checkpoint,
                                        "text": result["text"], "id": evidence["id"], "agent_terminal": terminal}
                        feedbacks[branch] = measured
                        handle.mark_completed(attempt, prompt, measured)
                    else:
                        status = "invalid_attempt"
                        break
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                interruption = exc
            failure = type(exc).__name__
            status = ("cancelled" if isinstance(exc, asyncio.CancelledError) else
                      "budget_exhausted" if isinstance(exc, RsiBudgetExceeded) else "invalid")
            if active is not None and active["stage"] != "completed":
                active.update(stage="invalid", error_type=failure, budget_after=self.budget.snapshot())
        finally:
            cleanup = await self.factory.close()
            self._task = None
        if asyncio.current_task().cancelling() and interruption is None:
            interruption = asyncio.CancelledError()
            status, failure = "cancelled", "CancelledError"
        if cleanup and interruption is None:
            status, failure = "cleanup_failed", failure or "BranchCleanupError"
        trace_path = None
        if baseline is not None and nodes and all(a["valid"] for a in attempts) and not self.budget.blocked and not cleanup:
            trace = {"version": 1, "n_problems": 1, "baseline": {
                "evaluated": True, "combined_score": baseline["score"], "n_valid": int(baseline["score"] == 1), "n_total": 1},
                "nodes": nodes, "provenance": provenance, "feedback_type": "visible_command_exit_status"}
            validate_trace(trace)
            trace_path = self.factory.output / "trace.json"
            write_json(trace_path, trace)
        selected_path = patch_path = changes_path = None
        if selected is not None:
            selected_path, patch_path, changes_path = self.factory.export(selected["checkpoint"])
        report = {"task_id": self.task_id, "split": self.split, "arm": self.arm, "status": status,
            "error_type": failure, "selected_candidate": selected["id"] if selected else None,
            "selected_visible_score": selected["score"] if selected else None,
            "selected_agent_terminal": selected["agent_terminal"] if selected else None,
            "final_success": None, "quality": "unmeasured", "auto_promoted": False,
            "provenance": provenance, "policy": frozen, "budget": self.budget.snapshot(),
            "provider_requests": self.budget.records, "evaluations": self.budget.evaluations,
            "attempts": attempts, "planned_attempts": len(plan), "dispatched_attempts": len(attempts),
            "completed_attempts": sum(a["stage"] == "completed" for a in attempts),
            "valid_attempts": sum(a["valid"] for a in attempts), "invalid_attempts": sum(not a["valid"] for a in attempts),
            "attempt_completion_rate": sum(a["stage"] == "completed" for a in attempts)/len(plan),
            "not_dispatched_attempts": len(plan)-len(attempts), "cleanup_errors": cleanup,
            "trace_sha256": file_sha(trace_path) if trace_path else None,
            "selected_patch_sha256": file_sha(patch_path) if patch_path else None}
        write_json(self.factory.output / "result.json", report)
        if interruption is not None:
            raise interruption
        return RsiResult(text=selected["text"] if selected else "", selected_visible_score=selected["score"] if selected else None,
            selected_path=selected_path, patch_path=patch_path, changes_path=changes_path,
            trace_path=trace_path, report=report, factory=self.factory)


@dataclass(frozen=True)
class TaskSplits:
    """Whole task identities are disjoint before any optimization is run."""
    train: tuple[str, ...]
    val: tuple[str, ...]
    test: tuple[str, ...]

    def __post_init__(self):
        seen = set()
        for name in ("train", "val", "test"):
            values = tuple(getattr(self, name))
            if not values or any(not isinstance(v, str) or not v for v in values):
                raise ValueError("nonempty explicit task splits required")
            if len(set(values)) != len(values) or seen.intersection(values):
                raise ValueError("task splits overlap or contain duplicate identities")
            seen.update(values)
            object.__setattr__(self, name, values)


def export_dataset(results, splits, output):
    """Export immutable scored traces, excluding the independent test split."""
    from .rsi_branch import file_sha, write_json
    from .dream_rsi_optimizer import load_dataset
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    entries, kinds = [], []
    for index, result in enumerate(results):
        report = result.report
        task = report["task_id"]
        if task in splits.test:
            continue
        split = "train" if task in splits.train else "holdout" if task in splits.val else None
        expected = "val" if split == "holdout" else split
        if (split is None or report["split"] != expected or report["status"] != "completed"
                or result.trace_path is None or file_sha(result.trace_path) != report["trace_sha256"]):
            raise ValueError("unverified trace or task split")
        name = f"trace-{index}.json"
        (output / name).write_bytes(result.trace_path.read_bytes())
        kinds.append(report["provenance"]["kind"])
        entries.append({"id": f"trace-{index}", "task_id": task, "split": split, "file": name,
                        "sha256": file_sha(output / name), "provenance": "production CodingAgent measured visible-command trace"})
    manifest = output / "manifest.json"
    write_json(manifest, {"schema_version": 1, "kind": "observed" if kinds and all(k == "observed" for k in kinds) else "synthetic",
                          "source": "Verified local RSI results; scripted/custom clients remain synthetic.", "entries": entries})
    load_dataset(manifest)
    return manifest


async def run_contract_demo(output):
    """Real tools and SDK replay with a scripted model; zero provider API calls.

    The low micro threshold exercises the projection contract, not performance.
    Independent final commands execute only after selection and consume the same
    episode evaluation/deadline pool. They never enter optimizer/controller data.
    """
    import sys
    from my_agent_core.context_compaction import CompactionPolicy
    from my_agent_core.session import Session
    from my_agent_llm import Config, Response
    from .agent import CodingAgent
    from .dream_rsi_optimizer import optimize
    from .rsi_branch import LocalBranchFactory, VisibleCommandEvaluator, copy_tree, write_json

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    splits = TaskSplits(("contract-train",), ("contract-val",), ("contract-test",))
    fixed = FrozenPolicy({"max_width": 2, "max_refine": 1})
    limits = dict(max_requests=12, max_tokens=300000, max_seconds=90, max_evaluations=6)
    visible = VisibleCommandEvaluator((sys.executable, "-c",
        "from pathlib import Path; assert Path('answer.txt').read_text() == '42'"))

    class ScriptedWriter:
        config = Config(provider="openai", model="scripted-rsi-contract", max_tokens=64, max_retries=0)
        model = "scripted-rsi-contract"

        def __init__(self, task):
            self.task, self.counts = task, {}

        async def achat(self, messages, **kwargs):
            scope = _SCOPE.get()
            key = (scope.get("branch"), scope.get("attempt"))
            step = self.counts.get(key, 0)
            self.counts[key] = step + 1
            usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            if step % 3 == 2:
                return Response(model=self.model, content="Edited using the real write tool.", usage=usage)
            branch, attempt = key
            success = attempt > 0 and (self.task != "contract-val" or branch == 0)
            path, content = ("answer.txt", "42" if success else "7") if step % 3 == 0 else ("notes.txt", "contract fixture")
            return Response(model=self.model, content="", usage=usage, tool_calls=[{
                "id": f"write-{branch}-{attempt}-{step}", "type": "function", "function": {
                    "name": "write", "arguments": json.dumps({"path": path, "content": content})}}])

    async def episode(task, split, arm, policy, mode, source, *, final=False):
        budget = ParentBudget(**limits)
        factory = LocalBranchFactory(source, output / (arm if split == "test" else split), attempt_max_turns=3,
            artifact_projection=mode == "layered", compaction_policy=CompactionPolicy(
                mode=mode, recent_tool_groups=0, micro_min_chars=1, model_context_window=32000))
        runtime = RsiRuntime(factory=factory, evaluator=visible, budget=budget, policy=policy,
            task_id=task, split=split, arm=arm, schedule="random" if arm == "random" else "fixed" if arm in ("A", "B") else "rsi")
        agent = CodingAgent(workspace=source, llm=ScriptedWriter(task), session=Session(path=factory.output / "parent.jsonl"),
            rsi_runtime=runtime, auto_load_mcp=False, memory_dir=False, task_store=False,
            skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[])
        try:
            await agent.run("Edit answer.txt so the visible test passes.")
        finally:
            await agent.aclose()
        result = agent.last_rsi_result
        if final:
            # Separate command and copied tree: neither its feedback nor mutations
            # can affect online selection or the reviewable selected artifact.
            final_tree = factory.output / "final-test-tree"
            if result.selected_path is None:
                raise ValueError("contract fixture did not select a candidate")
            copy_tree(result.selected_path, final_tree)
            evaluator = VisibleCommandEvaluator((sys.executable, "-c",
                "from pathlib import Path; assert int(Path('answer.txt').read_text()) * 2 == 84"))
            with execution_scope(stage="independent_final_test"):
                measured = await evaluator.evaluate(final_tree, budget, None)
            result.report["final_test"] = {"raw_passed": measured["score"] == 1, "independent_contract_only": True,
                "selection_frozen": True, "evaluation": measured}
            result.report["budget"] = budget.snapshot()
            write_json(factory.output / "final-test.json", result.report["final_test"])
        return result

    sources = {}
    for name in ("train", "val", "test"):
        source = output / f"{name}-source"
        source.mkdir()
        (source / "answer.txt").write_text("0", encoding="utf-8")
        sources[name] = source
    train = await episode("contract-train", "train", "B", fixed, "layered", sources["train"])
    val = await episode("contract-val", "val", "B", fixed, "layered", sources["val"])
    manifest = export_dataset([train, val], splits, output / "dataset")
    optimized = await asyncio.to_thread(optimize, manifest, output / "optimizer", registry_path=output / "private-registry",
        budgets=(1, 2, 4), max_candidates=4, dry_run=True, max_workers=1, timeout_seconds=30, max_seconds=300)
    frozen = FrozenPolicy(optimized["selected"]["parameters"])
    arms = {}
    for arm, mode, policy in (("A", "baseline", fixed), ("B", "layered", fixed),
                              ("C", "layered", frozen), ("random", "layered", fixed)):
        result = await episode("contract-test", "test", arm, policy, mode, sources["test"], final=True)
        arms[arm] = result.report
    summary = {"actual_provider_calls": 0, "quality": "unmeasured", "optimizer": optimized, "arms": arms,
               "fixture": "Synthetic scripted model; low micro threshold; independent local contract tests only."}
    write_json(output / "demo-summary.json", summary)
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Zero-provider RSI production-loop contract demo")
    parser.add_argument("command", choices=("demo",))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = asyncio.run(run_contract_demo(args.output))
    print(json.dumps({"output": str(args.output.resolve()), "quality": result["quality"], "actual_provider_calls": 0}))


if __name__ == "__main__":
    main()
