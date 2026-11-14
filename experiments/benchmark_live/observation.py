"""Explicit owner for future paid experiments; metadata-only, one export worker.

SDK propagation happens on the export worker, where observations are created.
The ledger remains authoritative; export failures never retry a provider call.
"""
from __future__ import annotations

import asyncio
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
_STAGES = {"answer", "judge", "extract", "probe"}


def _identifier(value):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError("metadata must use an opaque ASCII identifier")
    return value


class SessionScopeSink:
    """Propagate a frozen campaign session and allowlisted per-trial attributes."""
    def __init__(self, sink, *, campaign_id, propagate, flush=None, close=None):
        self.sink = sink
        self.campaign_id = _identifier(campaign_id)
        self.propagate = propagate
        self._flush, self._close = flush, close
        self._scopes = {}
        self._lock = Lock()

    def register(self, root_id, *, arm, stage, task_id):
        _identifier(root_id)
        if stage not in _STAGES:
            raise ValueError("unsupported experiment stage")
        metadata = {"campaign": self.campaign_id, "arm": _identifier(arm),
                    "stage": stage, "task_id": _identifier(task_id)}
        with self._lock:
            if root_id in self._scopes and self._scopes[root_id] != metadata:
                raise ValueError("active observation scope cannot change")
            self._scopes[root_id] = metadata

    def __call__(self, record):
        with self._lock:
            metadata = dict(self._scopes[record.root_id])
        try:
            with self.propagate(session_id=self.campaign_id, metadata=metadata):
                self.sink(record)
        finally:
            if isinstance(record, CallEnd):
                with self._lock:
                    self._scopes.pop(record.root_id, None)

    def flush(self):
        if self._flush is not None:
            self._flush()

    def close(self):
        if self._close is not None:
            self._close()


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
    def __init__(self, raw, *, runtime=None, scope_sink=None, close_timeout=15):
        if not math.isfinite(close_timeout) or close_timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        self.raw, self.runtime, self.scope_sink = raw, runtime, scope_sink
        self.close_timeout = close_timeout
        self._close_result = None
        if runtime is not None:
            try:
                runtime.attach(raw)
            except BaseException:
                runtime.close(timeout=close_timeout)
                raise

    @classmethod
    def from_environment(cls, raw, *, campaign_id, enabled=False,
                         public_key_env="LANGFUSE_PUBLIC_KEY", secret_key_env="LANGFUSE_SECRET_KEY",
                         base_url="https://cloud.langfuse.com", capacity=1024,
                         network_timeout=5, close_timeout=15):
        _identifier(campaign_id)
        if not enabled:
            return cls(raw, close_timeout=close_timeout)
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
        runtime = ObservationRuntime(BoundedExporter(scoped, capacity=capacity))
        return cls(raw, runtime=runtime, scope_sink=scoped, close_timeout=close_timeout)

    async def achat(self, ledger, run_id, messages, *, arm, stage, task_id, **kwargs):
        if self._close_result is not None:
            raise RuntimeError("experiment owner is closed")
        _identifier(run_id), _identifier(arm), _identifier(task_id)
        if stage not in _STAGES:
            raise ValueError("unsupported experiment stage")
        if self.scope_sink is not None:
            self.scope_sink.register(run_id, arm=arm, stage=stage, task_id=task_id)
        with observation_context(root_id=run_id, purpose="experiment_" + stage):
            return await GuardedLLM(self.raw, ledger, run_id).achat(messages, **kwargs)

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
