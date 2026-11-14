"""Optional Langfuse v5 generation sink; always use BoundedExporter around it.

API checked against langfuse-python 5.2.0, commit
00e5098bd6511a2f7bc7b6813ac125a3af559697 (client.start_observation,
generation.update(usage_details=...), generation.end()). No real service tested.
No SDK import/client construction occurs unless from_credentials is invoked.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib

from .observation import CallEnd, CallRecord


def _usage_details(usage):
    """Map only known totals; prompt_tokens can exclude cache hits for a provider.

    Cache details stay in metadata so Langfuse cannot add them to input twice.
    Missing fields remain absent. These are provider snapshots, not invoices.
    """
    allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens"}
    numeric = {key: value for key, value in usage.items() if key in allowed and type(value) is int and value >= 0}
    details = {}
    output, total = numeric.get("completion_tokens"), numeric.get("total_tokens")
    if output is not None:
        details["output"] = output
    status = "incomplete"
    if total is not None:
        details["total"] = total
    if total is not None and output is not None:
        input_tokens = total - output
        parts = [numeric[key] for key in ("prompt_tokens", "cache_read_tokens", "cache_write_tokens") if key in numeric]
        if input_tokens < 0 or any(part > input_tokens for part in parts):
            status = "inconsistent"
            details.pop("total", None)
        else:
            details["input"] = input_tokens
            status = "complete"
    return details, numeric, status


class LangfuseSink:
    """Single-worker sink; injected clients are caller-owned unless specified."""
    def __init__(self, client, *, max_active: int = 1024, own_client: bool = False):
        if max_active < 1:
            raise ValueError("max_active must be positive")
        self.client = client
        self.max_active = max_active
        self.own_client = own_client
        self._active: OrderedDict = OrderedDict()
        self.orphan_ends = 0
        self.abandoned = 0

    @classmethod
    def from_credentials(cls, *, public_key: str, secret_key: str, base_url: str | None = None,
                         timeout: float = 5, max_active: int = 1024):
        """Explicit opt-in; credentials are never added to observation metadata."""
        if not public_key or not secret_key:
            raise ValueError("Explicit Langfuse public and secret keys are required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        from langfuse import Langfuse  # optional dependency
        options = {"public_key": public_key, "secret_key": secret_key, "timeout": timeout}
        if base_url is not None:
            options["base_url"] = base_url
        return cls(Langfuse(**options), max_active=max_active, own_client=True)

    def __call__(self, record: CallRecord) -> None:
        if isinstance(record, CallEnd):
            span = self._active.pop(record.call_id, None)
            if span is None:
                self.orphan_ends += 1
                return
            try:
                metadata = {"status": record.status, "duration_seconds": record.duration_seconds,
                            "error_type": record.error_type}
                options = {"metadata": metadata}
                if record.usage is not None:
                    details, numeric, status = _usage_details(record.usage)
                    metadata.update(provider_usage=numeric, usage_mapping_status=status)
                    if details:
                        options["usage_details"] = details
                if record.status == "error":
                    options["level"] = "ERROR"
                span.update(**options)
            finally:
                span.end()
            return
        while len(self._active) >= self.max_active:
            _, old = self._active.popitem(last=False)
            self.abandoned += 1
            old.end()
        trace = {"trace_id": hashlib.sha256(record.root_id.encode()).hexdigest()[:32]}
        parent = self._active.get(record.parent_id)
        if parent is not None:
            trace["parent_span_id"] = parent.id
        span = self.client.start_observation(
            trace_context=trace, name=f"llm.{record.purpose}", as_type="generation", model=record.model,
            metadata={"call_id": record.call_id, "root_id": record.root_id, "parent_id": record.parent_id,
                      "purpose": record.purpose, "provider": record.provider, "method": record.method,
                      "started_at": record.started_at},
        )
        self._active[record.call_id] = span

    def flush(self) -> None:
        self.client.flush()

    def close(self) -> None:
        try:
            while self._active:
                _, span = self._active.popitem(last=False)
                self.abandoned += 1
                span.end()
        finally:
            try:
                self.client.flush()
            finally:
                if self.own_client:
                    self.client.shutdown()
