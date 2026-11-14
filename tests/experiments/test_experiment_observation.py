"""Offline experiment-owner lifecycle, budget and session propagation contracts."""
import asyncio
import json
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from experiments.benchmark_live.budget import BudgetLedger, BudgetStop
from experiments.benchmark_live.observation import ExperimentLLMOwner, SessionScopeSink
from my_agent_llm.observation import CallEnd, CallObservation, CallStart
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.observation_runtime import ObservationRuntime


class Raw:
    def __init__(self, usage=None):
        self.config = SimpleNamespace(max_retries=0, provider="offline")
        self.model = "offline-model"
        self.usage = usage
        self.calls = 0
        self.observers = []
        self.closes = 0
        self._provider = SimpleNamespace(client=self, async_client=self)

    def add_observer(self, observer):
        self.observers.append(observer)
        return SimpleNamespace(closed=False, close=lambda: self.observers.remove(observer))

    async def achat(self, messages, **kwargs):
        self.calls += 1
        observation = CallObservation(lambda r: [o(r) for o in self.observers],
                                      provider="offline", model=self.model, method="achat")
        response = SimpleNamespace(usage=self.usage, outcome="completed")
        observation.update_usage(self.usage)
        observation.finish()
        return response

    async def aclose(self):
        self.closes += 1


def ledger_at(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", campaign_id="offline-owner",
                          limit_usd="1", spend_ceiling_usd=1)
    ledger.register_run("trial", max_requests=1, time_cap=30)
    return ledger


def test_disabled_owner_does_not_construct_sdk(monkeypatch):
    import my_agent_llm.langfuse_export as export
    monkeypatch.setattr(export.LangfuseSink, "from_credentials", lambda **kw: pytest.fail("SDK constructed"))
    raw = Raw()
    owner = ExperimentLLMOwner.from_environment(raw, campaign_id="campaign", enabled=False)
    assert owner.runtime is None and not raw.observers
    assert asyncio.run(owner.aclose())["llm_clients_closed"]
    assert raw.closes == 1


def test_enabled_missing_credentials_fails_before_call(monkeypatch):
    monkeypatch.delenv("OWNER_TEST_PUBLIC", raising=False)
    monkeypatch.delenv("OWNER_TEST_SECRET", raising=False)
    raw = Raw()
    with pytest.raises(ValueError, match="credentials"):
        ExperimentLLMOwner.from_environment(raw, campaign_id="campaign", enabled=True,
            public_key_env="OWNER_TEST_PUBLIC", secret_key_env="OWNER_TEST_SECRET")
    assert raw.calls == 0


def test_guarded_call_settles_and_closes_owned_clients(tmp_path):
    rows, contexts, closes = [], [], []

    @contextmanager
    def propagate(**kw):
        contexts.append(kw)
        yield

    sink = SimpleNamespace(__call__=rows.append, flush=lambda: closes.append("flush"),
                           close=lambda: closes.append("shutdown"))
    scoped = SessionScopeSink(rows.append, campaign_id="campaign", propagate=propagate,
                              flush=sink.flush, close=sink.close)
    runtime = ObservationRuntime(BoundedExporter(scoped))
    raw = Raw({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    owner = ExperimentLLMOwner(raw, runtime=runtime, scope_sink=scoped)
    ledger = ledger_at(tmp_path)
    from my_agent_llm.models import Message

    async def run():
        await owner.achat(ledger, "trial", [Message(role="user", content="private input")],
                          arm="mem0", stage="answer", task_id="lme_abc123", max_tokens=8)
        return await owner.aclose()

    result = asyncio.run(run())
    assert result["runtime_close_complete"] and result["llm_clients_closed"]
    assert result["export_errors"] == 0 and result["dropped"] == 0
    assert [r.status for r in rows if isinstance(r, CallEnd)] == ["ok"]
    assert all(c["session_id"] == "campaign" for c in contexts)
    assert contexts[0]["metadata"] == {"campaign": "campaign", "arm": "mem0",
                                       "stage": "answer", "task_id": "lme_abc123"}
    assert "private input" not in repr(rows) + repr(contexts)
    assert ledger.snapshot()["requests"][0]["status"] == "ok"
    assert raw.closes == 1 and not raw.observers
    assert closes[-1] == "shutdown"
    asyncio.run(owner.aclose())
    assert raw.closes == 1


def test_unknown_usage_stops_and_preserves_reservation(tmp_path):
    from my_agent_llm.models import Message
    raw = Raw(None)
    owner = ExperimentLLMOwner(raw)
    ledger = ledger_at(tmp_path)

    async def run():
        try:
            with pytest.raises(BudgetStop, match="unknown usage"):
                await owner.achat(ledger, "trial", [Message(role="user", content="input")],
                                  arm="none", stage="probe", task_id="probe", max_tokens=8)
        finally:
            await owner.aclose()

    asyncio.run(run())
    request = ledger.snapshot()["requests"][0]
    assert request["usage"] is None and request["status"] == "ok"
    assert ledger.snapshot()["campaigns"]["offline-owner"]["blocked"]
    assert raw.calls == 1 and raw.closes == 1


def test_metadata_rejects_raw_text_and_unknown_stage_before_paid_call(tmp_path):
    raw = Raw()
    owner = ExperimentLLMOwner(raw)
    ledger = ledger_at(tmp_path)
    from my_agent_llm.models import Message
    for stage, task_id in (("answer", "raw question with spaces"), ("raw body", "safe")):
        with pytest.raises(ValueError):
            asyncio.run(owner.achat(ledger, "trial", [Message(role="user", content="x")],
                                   arm="mem0", stage=stage, task_id=task_id, max_tokens=8))
    assert raw.calls == 0 and ledger.snapshot()["requests"] == []
    asyncio.run(owner.aclose())


def test_export_failure_is_visible_without_repeating_settled_call(tmp_path):
    @contextmanager
    def propagate(**kwargs):
        yield

    def fail(record):
        raise OSError("transport failure")

    scoped = SessionScopeSink(fail, campaign_id="campaign", propagate=propagate)
    runtime = ObservationRuntime(BoundedExporter(scoped))
    raw = Raw({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    owner = ExperimentLLMOwner(raw, runtime=runtime, scope_sink=scoped)
    ledger = ledger_at(tmp_path)
    from my_agent_llm.models import Message

    async def run():
        await owner.achat(ledger, "trial", [Message(role="user", content="x")],
                          arm="probe", stage="probe", task_id="probe", max_tokens=8)
        return await owner.aclose()

    result = asyncio.run(run())
    assert result["export_errors"] == 2 and raw.calls == 1
    assert ledger.snapshot()["requests"][0]["status"] == "ok"


def test_real_sdk_session_scope_runs_on_export_worker_no_network(monkeypatch):
    sdk = pytest.importorskip("langfuse")
    import socket
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from my_agent_llm.langfuse_export import LangfuseSink
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("network used"))
    transport = InMemorySpanExporter()
    provider = TracerProvider()
    client = sdk.Langfuse(public_key="pk-lf-owner-offline", secret_key="sk-lf-owner-offline",
        base_url="http://127.0.0.1:9", span_exporter=transport, tracer_provider=provider,
        flush_at=1, media_upload_thread_count=1)
    base = LangfuseSink(client, own_client=True)
    scoped = SessionScopeSink(base, campaign_id="campaign", propagate=sdk.propagate_attributes,
                              flush=base.flush, close=base.close)
    scoped.register("trial", arm="lexical", stage="judge", task_id="lme_abc123")
    exporter = BoundedExporter(scoped)
    start = CallStart("call", "trial", None, "experiment_judge", "offline", "model", "achat", 0)
    try:
        exporter(start)
        exporter(CallEnd(**asdict(start), status="ok", duration_seconds=.1,
                         usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}))
        assert exporter.close(timeout=5)
        assert exporter.export_errors == 0
        attrs = transport.get_finished_spans()[0].attributes
        assert attrs["session.id"] == "campaign"
        assert json.loads(attrs["langfuse.trace.metadata.arm"]) == "lexical"
        assert json.loads(attrs["langfuse.trace.metadata.stage"]) == "judge"
        assert "langfuse.observation.input" not in attrs and "langfuse.observation.output" not in attrs
    finally:
        exporter.close(timeout=5)
        provider.shutdown()


def test_cli_requires_explicit_single_paid_flag_before_dispatch(monkeypatch):
    from experiments.benchmark_live import observation_probe
    monkeypatch.setattr(observation_probe, "run_probe", lambda args: pytest.fail("probe dispatched"))
    with pytest.raises(SystemExit) as error:
        observation_probe.main(["--output", "unused", "--campaign-id", "campaign", "--run-id", "trial",
                                "--project-id", "project"])
    assert error.value.code == 2


def test_cli_refuses_existing_output_without_request(tmp_path, monkeypatch):
    from experiments.benchmark_live import observation_probe
    (tmp_path / "ledger.json").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(observation_probe, "create_live_llm", lambda **kw: pytest.fail("provider created"))
    args = SimpleNamespace(output=str(tmp_path), campaign_id="campaign", run_id="trial",
                           project_id="project", windows_user_env=False)
    with pytest.raises(ValueError, match="never retried"):
        asyncio.run(observation_probe.run_probe(args))
    assert (tmp_path / "ledger.json").read_text(encoding="utf-8") == "preserve"


def test_cli_disabled_observation_has_no_paid_request(tmp_path, monkeypatch):
    from experiments.benchmark_live import observation_probe
    monkeypatch.setattr(observation_probe, "create_live_llm", lambda **kw: pytest.fail("provider created"))
    monkeypatch.setattr(observation_probe, "load_settings",
                        lambda: SimpleNamespace(observation=SimpleNamespace(enabled=False)))
    args = SimpleNamespace(output=str(tmp_path), campaign_id="campaign", run_id="trial",
                           project_id="project", windows_user_env=False)
    result = asyncio.run(observation_probe.run_probe(args))
    assert result["status"] == "stopped" and result["error_type"] == "ValueError"
    assert not (tmp_path / "ledger.json").exists()
