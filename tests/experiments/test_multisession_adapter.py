"""Controlled native Mem0 extraction: offline accounting and fail-closed contracts."""

import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from experiments.benchmark_live.budget import BudgetStop
from experiments.benchmark_live.observation import ExperimentLLMOwner, SessionScopeSink
from experiments.memory_validation.runner import TokenLedger, token_state
from experiments.memory_validation.multisession_adapter import (
    ExtractionFormatError,
    GuardedMem0LLM,
    add_native_facts,
)
from my_agent_llm.observation import CallObservation
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.observation_runtime import ObservationRuntime
from tests.experiments.test_experiment_observation import Raw


class SyncRaw(Raw):
    def __init__(self, content, usage=None, finish_reason="stop"):
        super().__init__(
            usage if usage is not None else {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        )
        self.content, self.finish_reason, self.kwargs = content, finish_reason, None

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        event = CallObservation(
            lambda r: [o(r) for o in self.observers], provider="offline", model=self.model, method="chat"
        )
        event.update_usage(self.usage)
        event.finish()
        return SimpleNamespace(
            content=self.content, usage=self.usage, outcome="completed", finish_reason=self.finish_reason
        )


def setup_adapter(tmp_path, content, *, finish_reason="stop"):
    @contextmanager
    def propagate(**kw):
        yield

    scoped = SessionScopeSink(lambda record: None, campaign_id="campaign", propagate=propagate)
    raw = SyncRaw(content, finish_reason=finish_reason)
    owner = ExperimentLLMOwner(
        raw, runtime=ObservationRuntime(BoundedExporter(scoped)), scope_sink=scoped, required=True
    )
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="campaign", max_requests=4)
    adapter = GuardedMem0LLM(owner, ledger, run_id="extract-1", task_id="chunk-1", audit_path=tmp_path / "audit.jsonl")
    return adapter, owner, raw, ledger


def generate(adapter, **kwargs):
    return adapter.generate_response(
        messages=[{"role": "user", "content": "private source text"}], response_format={"type": "json_object"}, **kwargs
    )


def test_native_extraction_is_single_budgeted_observed_call(tmp_path):
    content = '{"memory":[{"id":"0","text":"private fact","linked_memory_ids":[]}]}'
    adapter, owner, raw, ledger = setup_adapter(tmp_path, content)
    try:
        assert json.loads(generate(adapter))["memory"][0]["text"] == "private fact"
        assert raw.calls == 1 and "response_format" not in raw.kwargs
        assert raw.kwargs["max_tokens"] == 2048
        assert token_state(ledger.snapshot())["used_tokens"] == 6
        audit = (tmp_path / "audit.jsonl").read_text()
        assert "private source text" not in audit and "private fact" not in audit
        assert json.loads(audit.splitlines()[-1])["status"] == "valid"
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"memory":"wrong"}',
        '{"memory":[{"text":"x","unknown":"field"}]}',
        '{"memory":[{"text":""}]}',
        '{"memory":[],"memory":[]}',
    ],
)
def test_invalid_format_remains_settled_and_cannot_retry(tmp_path, content):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, content)
    try:
        with pytest.raises(ExtractionFormatError):
            generate(adapter)
        assert raw.calls == 1 and ledger.snapshot()["requests"][0]["status"] == "ok"
        with pytest.raises(BudgetStop):
            generate(adapter)
        assert raw.calls == 1
        assert json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])["status"] == "invalid"
    finally:
        asyncio.run(owner.aclose())


def test_length_extraction_is_invalid_without_retry(tmp_path):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, '{"memory":[]}', finish_reason="length")
    try:
        with pytest.raises(ExtractionFormatError):
            generate(adapter)
        assert raw.calls == 1 and token_state(ledger.snapshot())["used_tokens"] == 6
    finally:
        asyncio.run(owner.aclose())


def test_unknown_usage_retains_global_stop(tmp_path):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, '{"memory":[]}')
    raw.usage = None
    try:
        with pytest.raises(BudgetStop):
            generate(adapter)
        assert raw.calls == 1 and token_state(ledger.snapshot())["unknown_or_inflight"]
        with pytest.raises(BudgetStop):
            generate(adapter)
        assert raw.calls == 1
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.parametrize("overrides", [{"response_format": None}, {"tools": []}, {"temperature": 1}])
def test_unsupported_sdk_options_fail_before_paid_call(tmp_path, overrides):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, '{"memory":[]}')
    try:
        options = {"messages": [{"role": "user", "content": "x"}], "response_format": {"type": "json_object"}}
        options.update(overrides)
        with pytest.raises(ValueError):
            adapter.generate_response(**options)
        assert raw.calls == 0 and not ledger.snapshot()["requests"]
    finally:
        asyncio.run(owner.aclose())


def test_native_add_restores_original_llm_and_checks_exact_call(tmp_path):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, '{"memory":[]}')
    old = object()

    class Memory:
        llm = old

        def add(self, messages, **kwargs):
            assert kwargs["infer"] is True
            self.llm.generate_response(messages=messages, response_format={"type": "json_object"})
            return {"results": []}

    memory = Memory()
    try:
        assert add_native_facts(memory, adapter, [{"role": "user", "content": "x"}], scope="conv") == {"results": []}
        assert memory.llm is old and raw.calls == 1
    finally:
        asyncio.run(owner.aclose())


def test_recreated_adapter_refuses_completed_run(tmp_path):
    adapter, owner, raw, ledger = setup_adapter(tmp_path, '{"memory":[]}')
    try:
        generate(adapter)
        with pytest.raises(BudgetStop):
            GuardedMem0LLM(owner, ledger, run_id="extract-1", task_id="chunk-1", audit_path=tmp_path / "audit.jsonl")
        assert raw.calls == 1
    finally:
        asyncio.run(owner.aclose())
