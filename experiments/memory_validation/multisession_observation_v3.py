"""Bounded, metadata-only v3 readback for the first two extraction calls.

Only remote observation reads are retried. Provider model calls remain outside
this gate and are never replayed. Missing observations may become visible during
a bounded polling window; duplicate matches for one trace stop immediately.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from experiments.benchmark_live.observation import ExperimentLLMOwner
from experiments.memory_validation.budget_v3 import GuardedLLM as GuardedLLMV3
from experiments.memory_validation.multisession_observation import (
    _object,
    query_cloud,
)


PROTOCOL = "locomo-multisession-four-arm-v3"
DEFAULT_WINDOW_SECONDS = 180.0
DEFAULT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 30.0


class ReadbackError(RuntimeError):
    """Sanitized remote readback failure."""


class ReadbackTimeout(ReadbackError):
    """The bounded read-only polling window expired."""


class ReadbackAmbiguous(ReadbackError):
    """More than one observation matched a single trace."""


class ReadbackUsageUnknown(ReadbackError):
    """A settled model response lacks complete usage."""


class ExperimentLLMOwnerV3(ExperimentLLMOwner):
    """Preserve owner semantics while routing through the isolated 32K guard."""

    def chat(self, ledger, run_id, messages, *, arm, stage, task_id, **kwargs):
        with self.observe(run_id, arm=arm, stage=stage, task_id=task_id):
            response = GuardedLLMV3(self.raw, ledger, run_id).chat(messages, **kwargs)
        if self.required:
            self.flush_and_check()
        return response

    async def achat(self, ledger, run_id, messages, *, arm, stage, task_id, **kwargs):
        with self.observe(run_id, arm=arm, stage=stage, task_id=task_id):
            response = await GuardedLLMV3(self.raw, ledger, run_id).achat(messages, **kwargs)
        if self.required:
            await self.aflush_and_check()
        return response


def checked_usage(usage):
    """Return the three accounting fields or stop before any remote query."""
    if not isinstance(usage, dict):
        raise ReadbackUsageUnknown("settled extraction usage is unavailable")
    result = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise ReadbackUsageUnknown("settled extraction usage is unknown")
        result[name] = value
    if result["total_tokens"] < max(result["prompt_tokens"], result["completion_tokens"]):
        raise ReadbackUsageUnknown("settled extraction usage is inconsistent")
    return result


def matching_cloud_rows(rows, root, *, campaign, project, model, task_id, usage):
    """Return every GEN generation matching one trace and its frozen metadata."""
    usage = checked_usage(usage)
    trace = hashlib.sha256(root.encode()).hexdigest()[:32]
    matches = []
    for row in rows:
        try:
            meta = _object(row.get("metadata"))
            if meta.get("parent_id") is not None:
                _object(meta["parent_id"])
            wanted = {
                "root_id": root,
                "campaign": campaign,
                "arm": "shared_facts",
                "stage": "extract",
                "task_id": task_id,
                "status": "ok",
            }
            usage_details = row.get("usageDetails") or {}
            if (
                any(meta.get(key) != value for key, value in wanted.items())
                or row.get("projectId") != project
                or row.get("traceId") != trace
                or row.get("sessionId") != campaign
                or str(row.get("type", "")).upper() != "GENERATION"
                or not row.get("endTime")
                or row.get("model") != model
                or usage_details.get("total") != usage["total_tokens"]
                or usage_details.get("output") != usage["completion_tokens"]
                or usage_details.get("input") != usage["total_tokens"] - usage["completion_tokens"]
            ):
                continue
            # Langfuse flattens the provider_usage mapping in exported metadata.
            # Accept the observed dotted keys while retaining support for a dict.
            provider = _object(meta.get("provider_usage")) if "provider_usage" in meta else {
                "total_tokens": meta.get("provider_usage.total_tokens"),
                "completion_tokens": meta.get("provider_usage.completion_tokens"),
                "prompt_tokens": meta.get("provider_usage.prompt_tokens"),
            }
            if any(provider.get(name) != usage[name] for name in usage):
                continue
            matches.append(
                {
                    "trace_id": trace,
                    "observation_id": row.get("id"),
                    "session_id": campaign,
                    "project_id": project,
                    "root_id": root,
                    "task_id": task_id,
                    "model": model,
                    "usage_details": {
                        key: usage_details[key]
                        for key in ("input", "output", "total")
                        if key in usage_details
                    },
                    "metadata_only": True,
                }
            )
        except (ValueError, TypeError):
            continue
    return matches


def match_cloud_exact(rows, root, *, campaign, project, model, task_id, usage):
    """Require exactly one matching remote observation for one trace."""
    matches = matching_cloud_rows(
        rows,
        root,
        campaign=campaign,
        project=project,
        model=model,
        task_id=task_id,
        usage=usage,
    )
    if len(matches) > 1:
        raise ReadbackAmbiguous("multiple remote observations match one extraction trace")
    return matches[0] if matches else None


def _write_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class FirstExtractReadbackV3:
    """Verify the first two settled extraction traces with bounded polling."""

    def __init__(
        self,
        owner,
        *,
        campaign,
        project,
        model,
        receipt,
        query=query_cloud,
        wait=time.sleep,
        monotonic=time.monotonic,
        window_seconds=DEFAULT_WINDOW_SECONDS,
        initial_delay_seconds=DEFAULT_INITIAL_DELAY_SECONDS,
        max_delay_seconds=DEFAULT_MAX_DELAY_SECONDS,
        protocol=PROTOCOL,
    ):
        if not 0 < window_seconds <= 180:
            raise ValueError("readback window must be within the frozen 180-second bound")
        if not 0 < initial_delay_seconds <= max_delay_seconds:
            raise ValueError("readback backoff bounds differ")
        self.owner, self.campaign, self.project, self.model = owner, campaign, project, model
        self.receipt = Path(receipt)
        self.query, self.wait, self.monotonic = query, wait, monotonic
        self.window_seconds = float(window_seconds)
        self.initial_delay_seconds = float(initial_delay_seconds)
        self.max_delay_seconds = float(max_delay_seconds)
        self.protocol = protocol
        self.calls = []
        self.verified = False
        self.attempts = {}
        self.query_errors = {}
        self.wait_seconds = []
        self.poll_started_at = None
        self.poll_finished_at = None
        if self.receipt.exists():
            prior = json.loads(self.receipt.read_text(encoding="utf-8"))
            rows = prior.get("rows")
            if (
                prior.get("schema_version") != 3
                or prior.get("protocol") != protocol
                or prior.get("status") != "verified"
                or prior.get("campaign") != campaign
                or prior.get("project") != project
                or prior.get("model") != model
                or prior.get("sample_count") != 2
                or prior.get("extra_model_requests") != 0
                or not isinstance(rows, list)
                or len(rows) != 2
                or len({row.get("trace_id") for row in rows}) != 2
                or any(
                    row.get("session_id") != campaign
                    or row.get("project_id") != project
                    or row.get("model") != model
                    for row in rows
                )
            ):
                raise ReadbackError("existing v3 first-extraction readback receipt differs")
            self.verified = True

    def _attempt(self, root, task_id, usage):
        self.attempts[root] = self.attempts.get(root, 0) + 1
        try:
            rows = self.query(root)
        except Exception:
            self.query_errors[root] = self.query_errors.get(root, 0) + 1
            return None
        return match_cloud_exact(
            rows,
            root,
            campaign=self.campaign,
            project=self.project,
            model=self.model,
            task_id=task_id,
            usage=usage,
        )

    def _failure(self, reason, rows):
        path = self.receipt.with_name(self.receipt.stem + ".failed.json")
        value = {
            "schema_version": 3,
            "protocol": self.protocol,
            "status": "failed",
            "reason": reason,
            "campaign": self.campaign,
            "project": self.project,
            "model": self.model,
            "sample_count": 2,
            "extra_model_requests": 0,
            "window_seconds": self.window_seconds,
            "attempts_per_trace": dict(self.attempts),
            "query_errors_per_trace": dict(self.query_errors),
            "rows": [row for row in rows if row is not None],
        }
        if not path.exists():
            _write_exclusive(path, value)
        return value

    def __call__(self, root, response, *, task_id):
        if self.verified:
            return
        if len(self.calls) >= 2:
            raise ReadbackError("first extraction readback not verified; no later dispatch")
        usage = checked_usage(dict(response.usage or {}))
        self.calls.append((root, task_id, usage))
        if len(self.calls) < 2:
            return
        if self.calls[0][0] == self.calls[1][0]:
            raise ReadbackError("first extraction traces must be distinct")

        self.owner.flush_and_check()
        self.poll_started_at = self.monotonic()
        deadline = self.poll_started_at + self.window_seconds
        found = {}
        delay = self.initial_delay_seconds
        while True:
            for trace_root, task, usage in self.calls:
                if trace_root in found:
                    continue
                row = self._attempt(trace_root, task, usage)
                if row is not None:
                    found[trace_root] = row
            if len(found) == 2:
                break
            now = self.monotonic()
            if now >= deadline:
                self._failure("readback_timeout", list(found.values()))
                raise ReadbackTimeout("bounded extraction trace polling window expired")
            pause = min(delay, deadline - now)
            self.wait(pause)
            self.wait_seconds.append(pause)
            delay = min(self.max_delay_seconds, delay * 2)

        self.poll_finished_at = self.monotonic()
        rows = [found[root] for root, _, _ in self.calls]
        value = {
            "schema_version": 3,
            "protocol": self.protocol,
            "status": "verified",
            "campaign": self.campaign,
            "project": self.project,
            "model": self.model,
            "sample_count": 2,
            "extra_model_requests": 0,
            "readback_mode": "bounded_polling",
            "window_seconds": self.window_seconds,
            "elapsed_seconds": self.poll_finished_at - self.poll_started_at,
            "backoff": {
                "kind": "exponential",
                "initial_seconds": self.initial_delay_seconds,
                "max_seconds": self.max_delay_seconds,
                "waits_seconds": list(self.wait_seconds),
            },
            "attempts_per_trace": dict(self.attempts),
            "query_errors_per_trace": dict(self.query_errors),
            "one_observation_per_trace": True,
            "same_campaign_session": True,
            "distinct_traces": True,
            "rows": rows,
        }
        _write_exclusive(self.receipt, value)
        self.verified = True
