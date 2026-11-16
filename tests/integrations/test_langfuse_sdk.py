"""Real Langfuse 5.2 SDK objects, production sink, in-memory transport only."""

from dataclasses import asdict
import json

import pytest

from my_agent_llm.observation import CallStart, CallEnd
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.langfuse_export import LangfuseSink


def test_real_langfuse_sdk_in_memory_span_transport_no_network(monkeypatch):
    """Exercise SDK 5.2 objects and flushing, with an OTel in-memory exporter.

    This is a serialization/transport contract, not a remote ingestion or invoice
    test. Neither provider calls nor HTTP requests are allowed.
    """
    sdk = pytest.importorskip("langfuse")
    import socket
    from importlib.metadata import version

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    assert version("langfuse") == "5.2.0"

    def forbid_network(*args, **kwargs):
        raise AssertionError("network must not be used in the SDK contract test")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    transport = InMemorySpanExporter()
    provider = TracerProvider()
    client = sdk.Langfuse(
        public_key="pk-lf-offline-contract",
        secret_key="sk-lf-offline-contract",
        base_url="http://127.0.0.1:9",
        span_exporter=transport,
        tracer_provider=provider,
        flush_at=1,
        media_upload_thread_count=1,
    )
    exporter = BoundedExporter(LangfuseSink(client, own_client=True))
    start = CallStart("call", "root", None, "summary", "offline", "fake", "chat", 0.0)
    end = CallEnd(
        **asdict(start),
        status="ok",
        duration_seconds=0.1,
        usage={"prompt_tokens": 10, "cache_read_tokens": 100, "completion_tokens": 20, "total_tokens": 130},
    )
    try:
        exporter(start)
        exporter(end)
        assert exporter.close(timeout=5)
        assert exporter.export_errors == 0 and exporter.close_errors == 0
        spans = transport.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {"input": 110, "output": 20, "total": 130}
        assert "langfuse.observation.input" not in attrs and "langfuse.observation.output" not in attrs
        assert "private response" not in repr(attrs)
    finally:
        exporter.close(timeout=5)
        provider.shutdown()
