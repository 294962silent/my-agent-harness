"""Native Mem0 ADD extraction through the sole durable budget/observer owner.

No provider client is constructed here. A malformed, truncated, swallowed, or
unknown result stops the whole campaign without repeating the paid request.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from experiments.benchmark_live.budget import BudgetStop
from my_agent_llm.models import Message


_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class ExtractionFormatError(BudgetStop):
    """The paid extraction settled but is unusable; retain it and stop."""


def safe_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("an opaque ASCII identifier is required")
    return value


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_extraction(content):
    """Accept the pinned additive schema, with no Markdown repair or fallback."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty extraction content")
    value = json.loads(
        content,
        object_pairs_hook=_no_duplicates,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )
    if not isinstance(value, dict) or set(value) != {"memory"} or not isinstance(value["memory"], list):
        raise ValueError("invalid native extraction envelope")
    for row in value["memory"]:
        if (
            not isinstance(row, dict)
            or set(row) - {"id", "text", "linked_memory_ids", "attributed_to"}
            or not isinstance(row.get("text"), str)
            or not row["text"].strip()
        ):
            raise ValueError("invalid native fact")
        if "id" in row and not isinstance(row["id"], str):
            raise ValueError("invalid native fact ID")
        if "linked_memory_ids" in row and (
            not isinstance(row["linked_memory_ids"], list)
            or any(not isinstance(x, str) for x in row["linked_memory_ids"])
        ):
            raise ValueError("invalid native links")
        if "attributed_to" in row and row["attributed_to"] is not None and not isinstance(row["attributed_to"], str):
            raise ValueError("invalid fact attribution")
    return value


class GuardedMem0LLM:
    """One generate_response slot. All native LLM work uses this single slot.

    response_format=json_object is validated locally and represented in the
    pinned native prompt; it is never forwarded around GuardedLLM accounting.
    """

    def __init__(
        self,
        owner,
        ledger,
        *,
        run_id,
        task_id,
        audit_path,
        max_output_tokens=2048,
        request_timeout=180,
        after_settle=None,
    ):
        if not owner.required:
            raise ValueError("native extraction requires observation")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise ValueError("invalid frozen extraction output cap")
        self.owner, self.ledger = owner, ledger
        self.run_id, self.task_id = safe_id(run_id), safe_id(task_id)
        self.audit_path = Path(audit_path)
        self.max_output_tokens, self.after_settle = max_output_tokens, after_settle
        self.used, self.valid, self.empty = False, False, False
        if any(r["run_id"] == run_id for r in ledger.snapshot()["requests"]):
            raise BudgetStop("extraction slot already dispatched; never retry")
        ledger.register_run(run_id, max_requests=1, time_cap=request_timeout)

    def _audit(self, **row):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"run_id": self.run_id, "task_id": self.task_id, "stage": "extract", **row}
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _block(self):
        with self.ledger.transaction() as data:
            data["campaigns"][self.ledger.campaign_id]["blocked"] = True

    def generate_response(self, messages, response_format=None, **kwargs):
        if self.used:
            raise BudgetStop("extraction slot already dispatched; never retry")
        if response_format != {"type": "json_object"} or kwargs:
            raise ValueError("unsupported native LLM option")
        if (
            not isinstance(messages, list)
            or not messages
            or any(
                not isinstance(m, dict)
                or set(m) != {"role", "content"}
                or m["role"] not in ("system", "user", "assistant")
                or not isinstance(m["content"], str)
                for m in messages
            )
        ):
            raise ValueError("native extraction supports text-only messages")
        self.used = True
        self._audit(
            status="started",
            input_sha256=hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        )
        started = time.monotonic()
        try:
            response = self.owner.chat(
                self.ledger,
                self.run_id,
                [Message(**m) for m in messages],
                arm="shared_facts",
                stage="extract",
                task_id=self.task_id,
                max_tokens=self.max_output_tokens,
                temperature=0,
            )
            if response.finish_reason != "stop":
                raise ExtractionFormatError("native extraction did not finish normally")
            try:
                parsed = parse_extraction(response.content)
            except (ValueError, TypeError):
                raise ExtractionFormatError("native extraction JSON is invalid") from None
            self.valid, self.empty = True, not parsed["memory"]
            self._audit(
                status="valid",
                empty=self.empty,
                memory_count=len(parsed["memory"]),
                response_sha256=hashlib.sha256(response.content.encode()).hexdigest(),
                usage=response.usage,
                finish_reason=response.finish_reason,
                latency_seconds=time.monotonic() - started,
            )
            if self.after_settle is not None:
                self.after_settle(self.run_id, response)
            return response.content
        except BaseException as error:
            self.valid = False
            self._block()
            self._audit(
                status="invalid" if isinstance(error, ExtractionFormatError) else "stopped",
                error_type=type(error).__name__,
                latency_seconds=time.monotonic() - started,
            )
            raise


def add_native_facts(memory, adapter, messages, *, scope, metadata=None):
    """Exercise native infer=True once and detect SDK suppression of failures.

    The factory assembles Memory without a default paid provider. Replacing its
    NeverLLM placeholder for this bounded call is reversible and single-threaded.
    """
    safe_id(scope)
    if getattr(memory, "enable_graph", False):
        raise ValueError("native graph extraction is outside this frozen study")
    old = memory.llm
    memory.llm = adapter
    try:
        result = memory.add(messages, user_id=scope, metadata=metadata or {}, infer=True)
        if not adapter.used or not adapter.valid:
            adapter._block()
            raise ExtractionFormatError("native SDK did not expose one valid extraction")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("results"), list)
            or any(r.get("event") != "ADD" for r in result["results"])
        ):
            adapter._block()
            raise ExtractionFormatError("native ADD result contract differs")
        return result
    except BaseException:
        adapter._block()
        raise
    finally:
        memory.llm = old
