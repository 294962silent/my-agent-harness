"""Explicit owner for future paid experiments; metadata-only, one export worker.

SDK propagation happens on the export worker, where observations are created.
The ledger remains authoritative; export failures never retry a provider call.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
import math
import os
import re
from threading import Lock

from experiments.benchmark_live.budget import GuardedLLM
from my_agent_llm.observation import CallEnd, observation_context
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.observation_runtime import ObservationRuntime


_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STAGES = {"answer", "judge", "extract", "update", "probe", "train", "holdout", "test", "final", "optimize"}
_CORRELATION_KEYS = {"round_id", "branch_id", "attempt_id", "runtime_stage"}


class ObservationRequiredError(RuntimeError):
    """Stop the study after a settled call when required tracing is unhealthy."""


def _identifier(value):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError("metadata must use an opaque ASCII identifier")
    return value


def _correlation(metadata):
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or set(metadata) - _CORRELATION_KEYS:
        raise ValueError("unsupported observation correlation metadata")
    return {key: _identifier(value) for key, value in metadata.items()}


@dataclass(frozen=True)
class _ScopedRecord:
    record: object
    metadata: tuple[tuple[str, str], ...]


class SessionScopeSink:
    """Freeze metadata at CallStart on the caller thread, paired by call_id."""
    def __init__(self, sink, *, campaign_id, propagate, flush=None, close=None):
        self.sink = sink
        self.campaign_id = _identifier(campaign_id)
        self.propagate = propagate
        self._flush, self._close = flush, close
        self._scopes, self._calls = {}, {}
        self._current = ContextVar("experiment_call_scope", default=None)
        self._lock = Lock()

    def _metadata(self, arm, stage, task_id, metadata=None):
        if stage not in _STAGES:
            raise ValueError("unsupported experiment stage")
        return {"campaign": self.campaign_id, "arm": _identifier(arm),
                "stage": stage, "task_id": _identifier(task_id), **_correlation(metadata)}

    def register(self, root_id, *, arm, stage, task_id, metadata=None):
        """Persistent fallback for a root; explicit scope overrides it per call."""
        _identifier(root_id)
        metadata = self._metadata(arm, stage, task_id, metadata)
        with self._lock:
            if root_id in self._scopes and self._scopes[root_id] != metadata:
                raise ValueError("active observation scope cannot change")
            self._scopes[root_id] = metadata

    def unregister(self, root_id):
        with self._lock:
            if any(root == root_id for root, _ in self._calls.values()):
                raise ValueError("cannot unregister an active call scope")
            self._scopes.pop(root_id, None)

    @contextmanager
    def scope(self, root_id, *, arm, stage, task_id, metadata=None):
        metadata = self._metadata(arm, stage, task_id, metadata)
        token = self._current.set((_identifier(root_id), tuple(metadata.items())))
        try:
            yield
        finally:
            self._current.reset(token)

    def capture(self, record):
        """No I/O. End consumes only its matching immutable start snapshot."""
        with self._lock:
            if isinstance(record, CallEnd):
                entry = self._calls.get(record.call_id)
                if entry is None or entry[0] != record.root_id:
                    raise ValueError("observation end must match an active call scope")
                _, metadata = self._calls.pop(record.call_id)
            else:
                if record.call_id in self._calls:
                    raise ValueError("duplicate observation call_id")
                current = self._current.get()
                if current is not None and current[0] == record.root_id:
                    metadata = current[1]
                elif record.root_id in self._scopes:
                    metadata = tuple(self._scopes[record.root_id].items())
                else:
                    raise ValueError("observation requires an explicit experiment scope")
                self._calls[record.call_id] = (record.root_id, metadata)
        return _ScopedRecord(record, metadata)

    def __call__(self, item):
        # Direct legacy BoundedExporter users retain registered-root support.
        captured = item if isinstance(item, _ScopedRecord) else self.capture(item)
        with self.propagate(session_id=self.campaign_id, metadata=dict(captured.metadata)):
            self.sink(captured.record)

    def flush(self):
        if self._flush is not None:
            self._flush()

    def close(self):
        try:
            if self._close is not None:
                self._close()
        finally:
            with self._lock:
                self._scopes.clear()
                self._calls.clear()


class ScopeAwareExporter:
    """Capture context before enqueue; retain one existing bounded worker."""
    def __init__(self, sink, *, capacity=1024, exporter=None):
        self.sink = sink
        self.exporter = exporter or BoundedExporter(sink, capacity=capacity)
        if self.exporter.sink is not sink:
            raise ValueError("scope exporter must own the same sink")

    def __call__(self, record):
        try:
            captured = self.sink.capture(record)
        except BaseException:
            with self.exporter._lock:
                self.exporter.export_errors += 1
            return
        self.exporter(captured)

    def close(self, timeout=5):
        return self.exporter.close(timeout=timeout)

    def flush(self, timeout=5):
        return self.exporter.flush(timeout=timeout)

    @property
    def is_alive(self):
        return self.exporter.is_alive

    @property
    def dropped(self):
        return self.exporter.dropped

    @property
    def export_errors(self):
        return self.exporter.export_errors

    @property
    def close_errors(self):
        return self.exporter.close_errors


async def close_owned_llm(raw):
    """Close the distinct provider clients owned by this experiment facade."""
    provider = getattr(raw, "_provider", None)
    clients, errors = [], []
    for name in ("async_client", "client"):
        client = getattr(provider, name, None)
        if client is None or any(client is old for old in clients):
            continue
        clients.append(client)
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(type(exc).__name__)
    return not errors


class ExperimentLLMOwner:
    """Attach to the raw facade before GuardedLLM; close after all calls settle."""
    def __init__(self, raw, *, runtime=None, scope_sink=None, close_timeout=15, required=False):
        if not math.isfinite(close_timeout) or close_timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        if required and (runtime is None or scope_sink is None):
            raise ObservationRequiredError("required observation runtime is unavailable")
        self.required = bool(required)
        self.raw, self.runtime, self.scope_sink = raw, runtime, scope_sink
        self.close_timeout = close_timeout
        self._close_result = None
        if runtime is not None:
            if scope_sink is not None and not isinstance(runtime.exporter, ScopeAwareExporter):
                runtime.exporter = ScopeAwareExporter(scope_sink, exporter=runtime.exporter)
            try:
                runtime.attach(raw)
            except BaseException:
                runtime.close(timeout=close_timeout)
                raise

    @classmethod
    def from_environment(cls, raw, *, campaign_id, enabled=False,
                         public_key_env="LANGFUSE_PUBLIC_KEY", secret_key_env="LANGFUSE_SECRET_KEY",
                         base_url="https://cloud.langfuse.com", capacity=1024,
                         network_timeout=5, close_timeout=15, required=False):
        _identifier(campaign_id)
        if not enabled:
            return cls(raw, close_timeout=close_timeout, required=required)
        if not math.isfinite(close_timeout) or close_timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        public, secret = os.environ.get(public_key_env), os.environ.get(secret_key_env)
        if not public or not secret:
            raise ValueError("explicit Langfuse credentials are required")
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not math.isfinite(network_timeout) or network_timeout <= 0:
            raise ValueError("network timeout must be finite and positive")
        from langfuse import propagate_attributes
        from my_agent_llm.langfuse_export import LangfuseSink
        sink = LangfuseSink.from_credentials(public_key=public, secret_key=secret,
                                             base_url=base_url, timeout=network_timeout)
        scoped = SessionScopeSink(sink, campaign_id=campaign_id, propagate=propagate_attributes,
                                  flush=sink.flush, close=sink.close)
        runtime = ObservationRuntime(ScopeAwareExporter(scoped, capacity=capacity))
        return cls(raw, runtime=runtime, scope_sink=scoped, close_timeout=close_timeout, required=required)

    def check_health(self):
        """Fast local gate; remote presence still needs an explicit readback."""
        if self.required and (self.runtime is None or not self.runtime.is_alive
                              or self.runtime.export_errors or self.runtime.dropped
                              or self.runtime.close_errors):
            raise ObservationRequiredError("required observation exporter is unhealthy")

    def flush_and_check(self, timeout=None):
        """Drain the worker and SDK, then block if required export was lost."""
        wait = self.close_timeout if timeout is None else timeout
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("flush timeout must be finite and nonnegative")
        complete = self.runtime is None or self.runtime.exporter.flush(timeout=wait)
        self.check_health()
        if self.required and not complete:
            raise ObservationRequiredError("required observation flush did not complete")
        return complete

    async def aflush_and_check(self, timeout=None):
        return await asyncio.to_thread(self.flush_and_check, timeout)

    @contextmanager
    def observe(self, root_id, *, arm, stage, task_id, purpose=None, metadata=None):
        """Observe raw calls only; the caller retains sole budget ownership.

        ResearchBudget callers use this around their existing guarded branch.
        Multiple calls and concurrent task-local scopes may share a root.
        """
        if self._close_result is not None:
            raise RuntimeError("experiment owner is closed")
        self.check_health()
        _identifier(root_id), _identifier(arm), _identifier(task_id)
        _correlation(metadata)
        if stage not in _STAGES:
            raise ValueError("unsupported experiment stage")
        with observation_context(root_id=root_id, purpose=purpose or "experiment_" + stage):
            if self.scope_sink is None:
                yield self.raw
            else:
                with self.scope_sink.scope(root_id, arm=arm, stage=stage, task_id=task_id, metadata=metadata):
                    yield self.raw

    def chat(self, ledger, run_id, messages, *, arm, stage, task_id, **kwargs):
        """Budgeted sync entry for native Mem0 adapters; never nest guards."""
        with self.observe(run_id, arm=arm, stage=stage, task_id=task_id):
            response = GuardedLLM(self.raw, ledger, run_id).chat(messages, **kwargs)
        if self.required:
            self.flush_and_check()
        return response

    async def achat(self, ledger, run_id, messages, *, arm, stage, task_id, **kwargs):
        with self.observe(run_id, arm=arm, stage=stage, task_id=task_id):
            response = await GuardedLLM(self.raw, ledger, run_id).achat(messages, **kwargs)
        if self.required:
            await self.aflush_and_check()
        return response

    async def aclose(self):
        if self._close_result is not None:
            return dict(self._close_result)
        result = {"runtime_close_complete": True, "export_errors": 0, "close_errors": 0,
                  "dropped": 0, "runtime_alive_after_close": False}
        try:
            if self.runtime is not None:
                result["runtime_close_complete"] = await asyncio.to_thread(
                    self.runtime.close, timeout=self.close_timeout)
                result.update(export_errors=self.runtime.export_errors, close_errors=self.runtime.close_errors,
                              dropped=self.runtime.dropped, runtime_alive_after_close=self.runtime.is_alive)
        except BaseException as exc:
            result.update(runtime_close_complete=False, close_error_type=type(exc).__name__)
        finally:
            result["llm_clients_closed"] = await close_owned_llm(self.raw)
        self._close_result = result
        return dict(result)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()
