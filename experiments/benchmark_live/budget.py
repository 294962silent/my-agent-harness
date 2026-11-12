"""Durable reservation ledger. Unknown usage remains reserved; never estimated as zero.

Prices are conservative peak rates: input .30 USD/M, output 1.20 USD/M.
The text-only token ceiling is serialized UTF-8 bytes + generous framing allowance;
it is an assumption for this tokenizer/protocol, not a provider billing guarantee.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from filelock import FileLock

from my_agent_llm.events import StreamDoneEvent, StreamStartEvent
from my_agent_llm.models import Response, StreamChunk
from my_agent_llm.observation import _CONTEXT


class BudgetStop(RuntimeError):
    """Stop this run without exposing provider exceptions or credentials."""


def _cost(input_tokens: int, output_tokens: int) -> int:
    return int(
        (Decimal(input_tokens) * Decimal("0.30") + Decimal(output_tokens) * Decimal("1.20")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


class BudgetLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        limit_usd: str = "30",
        max_requests: int = 16,
        time_cap: float = 600,
        spend_ceiling_usd: float | None = None,
        request_ceiling: int | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock", timeout=10)
        limit = Decimal(limit_usd)
        if not limit.is_finite() or not 0 < limit <= 30:
            raise ValueError("limit must be positive and at most the authorized 30 USD")
        if not 0 < max_requests <= 16 or not 0 < time_cap <= 1800:
            raise ValueError("invalid run request/time limits")
        self.limit = int(limit * 1_000_000)
        self.spend_ceiling = None
        if spend_ceiling_usd is not None:
            ceiling = Decimal(str(spend_ceiling_usd))
            if not ceiling.is_finite() or not 0 < ceiling <= 30:
                raise ValueError("spend ceiling must be positive, finite and at most 30 USD")
            self.spend_ceiling = int(ceiling * 1_000_000)
        self.max_requests, self.time_cap = max_requests, time_cap
        if request_ceiling is not None and (type(request_ceiling) is not int or request_ceiling <= 0):
            raise ValueError("request ceiling must be a positive integer")
        self.request_ceiling = request_ceiling
        with self.lock:
            if not self.path.exists():
                self._write(
                    {
                        "limit_micro_usd": self.limit,
                        "pricing": {"input_per_million": "0.30", "output_per_million": "1.20"},
                        "max_requests": max_requests,
                        "time_cap": time_cap,
                        "runs": {},
                        "requests": [],
                    }
                )
            data = self._read()
            if (data["limit_micro_usd"], data["max_requests"], data["time_cap"]) != (
                self.limit,
                max_requests,
                time_cap,
            ):
                raise ValueError("existing ledger policy differs; never silently raise limits")

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def effective_request_limit(self):
        """One read-only source for the local/root min used by reservations and hints."""
        return min(self.max_requests, self.request_ceiling) if self.request_ceiling is not None else self.max_requests

    def _write(self, data):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".budget-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def transaction(self):
        with self.lock:
            data = self._read()
            yield data
            self._write(data)

    def snapshot(self):
        with self.lock:
            return self._read()

    def remaining_time(self, run_id):
        run = self.snapshot()["runs"].get(run_id)
        return self.time_cap if run is None else max(0, self.time_cap - (time.time() - run["started_at"]))

    def reserve(
        self,
        run_id,
        *,
        input_bound: int,
        max_output: int,
        purpose: str,
        model: str = "deepseek-v4.1-flash",
        provider: str = "openai",
    ) -> str:
        if not isinstance(input_bound, int) or input_bound < 0 or not 0 < max_output <= 4096:
            raise ValueError("invalid reservation size")
        held = _cost(input_bound, max_output)
        with self.transaction() as data:
            run = data["runs"].setdefault(run_id, {"started_at": time.time(), "blocked": False})
            requests = [r for r in data["requests"] if r["run_id"] == run_id]
            if run["blocked"] or any(r["status"] == "pending" for r in requests):
                raise BudgetStop("run has unknown/inflight usage; start no further requests")
            if len(requests) >= self.effective_request_limit or time.time() - run["started_at"] >= self.time_cap:
                raise BudgetStop("run request or time cap reached")
            charged = sum(
                r["cost_micro_usd"] if r["cost_micro_usd"] is not None else r["held_micro_usd"]
                for r in data["requests"]
            )
            effective_limit = (
                min(data["limit_micro_usd"], self.spend_ceiling)
                if self.spend_ceiling is not None
                else data["limit_micro_usd"]
            )
            if charged + held > effective_limit:
                raise BudgetStop("shared authorized budget exhausted")
            request_id = uuid4().hex
            data["requests"].append(
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "model": model,
                    "provider": provider,
                    "purpose": purpose,
                    "status": "pending",
                    "input_token_bound": input_bound,
                    "max_output_tokens": max_output,
                    "held_micro_usd": held,
                    "usage": None,
                    "cost_micro_usd": None,
                    "created_at": time.time(),
                }
            )
        return request_id

    def settle(self, request_id, *, usage, status: str) -> bool:
        with self.transaction() as data:
            row = next(r for r in data["requests"] if r["request_id"] == request_id)
            if row["status"] != "pending":
                raise ValueError("reservation already settled")
            known = isinstance(usage, dict) and all(
                isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens")
            )
            if known:
                known = all(
                    isinstance(usage.get(k, 0), int) and not isinstance(usage.get(k, 0), bool) and usage.get(k, 0) >= 0
                    for k in ("cache_read_tokens", "cache_write_tokens")
                )
            row["usage"] = dict(usage) if isinstance(usage, dict) else None
            row["status"] = status
            if known:
                inputs = usage["prompt_tokens"] + usage.get("cache_read_tokens", 0) + usage.get("cache_write_tokens", 0)
                outputs = usage["completion_tokens"]
                row["cost_micro_usd"] = _cost(inputs, outputs)
                if inputs > row["input_token_bound"] or outputs > row["max_output_tokens"]:
                    row["status"] = "bound_exceeded"
                    known = False
            if not known or status != "ok":
                data["runs"][row["run_id"]]["blocked"] = True
            row["finished_at"] = time.time()
        return known and status == "ok"


class GuardedLLM:
    """Explicit budget boundary, including auxiliary LLM calls.

    Uses non-streaming SDK calls and emits one completed chunk/event. This forbids
    provider stream-options fallback requests and obtains complete usage for each
    reservation. The underlying client must disable SDK retries.
    """

    def __init__(self, llm, ledger: BudgetLedger, run_id: str):
        if llm.config.max_retries != 0:
            raise ValueError("live runs require max_retries=0")
        self.llm, self.ledger, self.run_id = llm, ledger, run_id
        self.config = llm.config
        self.model = llm.model

    def _reserve(self, messages, tools, kwargs):
        if kwargs.get("model") not in (None, self.model):
            raise BudgetStop("model override differs from budgeted model")
        if set(kwargs) - {"model", "max_tokens", "temperature", "top_p", "stop"}:
            raise BudgetStop("unsupported request overrides outside budget accounting")
        if any(not isinstance(m.content, str) for m in messages):
            raise BudgetStop("live budget guard supports text-only official tasks")
        bound = (
            len(
                json.dumps(
                    {"messages": [m.model_dump() for m in messages], "tools": tools, "model": self.model},
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            + 4096
            + 256 * (len(messages) + len(tools or []))
        )
        output = min(int(kwargs.pop("max_tokens", 4096)), 4096)
        if output <= 0:
            raise ValueError("max_tokens must be positive")
        kwargs["max_tokens"] = output
        purpose = _CONTEXT.get().purpose or "agent"
        return self.ledger.reserve(
            self.run_id,
            input_bound=bound,
            max_output=output,
            purpose=purpose,
            model=self.model,
            provider=self.config.provider,
        )

    def _finish(self, reservation, response):
        status = (
            "error" if response.outcome == "provider_error" else "cancelled" if response.outcome == "aborted" else "ok"
        )
        if not self.ledger.settle(reservation, usage=response.usage, status=status):
            raise BudgetStop("unknown usage or failed request retained in ledger; run stopped")
        return response

    def chat(self, messages, *, tools=None, **kwargs):
        reservation = self._reserve(messages, tools, kwargs)
        try:
            response = self.llm.chat(messages, tools=tools, **kwargs)
        except BaseException:
            self.ledger.settle(reservation, usage=None, status="error")
            raise BudgetStop("model request failed; reservation retained") from None
        return self._finish(reservation, response)

    async def achat(self, messages, *, tools=None, **kwargs):
        reservation = self._reserve(messages, tools, kwargs)
        try:
            response = await asyncio.wait_for(
                self.llm.achat(messages, tools=tools, **kwargs), timeout=self.ledger.remaining_time(self.run_id)
            )
        except BaseException as exc:
            self.ledger.settle(
                reservation, usage=None, status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BudgetStop("model request failed; reservation retained") from None
        return self._finish(reservation, response)

    def stream(self, messages, **kwargs):
        response = self.chat(messages, **kwargs)
        yield StreamChunk(
            content=response.content,
            response=response,
            usage=response.usage,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
        )

    async def achat_stream(self, messages, **kwargs):
        response = await self.achat(messages, **kwargs)
        yield StreamChunk(
            content=response.content,
            response=response,
            usage=response.usage,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
        )

    async def astream_events(self, messages, *, signal=None, **kwargs):
        if signal is not None and signal.is_cancelled():
            raise asyncio.CancelledError()
        response: Response = await self.achat(messages, **kwargs)
        message = response.to_message()
        yield StreamStartEvent(partial=message)
        yield StreamDoneEvent(message=message, usage=response.usage)
