"""Export is bounded, metadata-only and requires no service or credentials."""
import json
from threading import Event

from my_agent_llm import Config, LLM
from my_agent_llm.models import Response
from my_agent_llm.observation import InMemoryObserver, observation_context
from my_agent_llm.observation_export import BoundedExporter, JsonlSink
from my_agent_llm.langfuse_export import LangfuseSink


def records():
    recorder = InMemoryObserver()
    llm = LLM(Config(provider="openai", api_key="offline", model="fake"), observer=recorder)
    class Provider:
        def chat(self, *args, **kwargs):
            return Response(content="private response", model="fake", usage={"prompt_tokens": 7})
    llm._provider = Provider()
    with observation_context(root_id="test-run", purpose="summary"):
        llm.chat([])
    return recorder.records


def test_queue_overflow_error_isolation_flush_and_close(tmp_path):
    entered, release = Event(), Event()
    class Slow:
        def __call__(self, record):
            entered.set()
            assert release.wait(3)
            raise ValueError("private error")
    exporter = BoundedExporter(Slow(), capacity=1)
    start, end = records()
    exporter(start)
    assert entered.wait(1)
    exporter(end)
    exporter(end)
    assert exporter.dropped == 1
    assert not exporter.flush(timeout=0.01)
    assert not exporter.close(timeout=0.01)
    release.set()
    assert exporter.close(timeout=3) and not exporter.is_alive
    assert exporter.export_errors == 2
    exporter(start)
    assert exporter.dropped == 2
    sink = JsonlSink(tmp_path / "calls.jsonl")
    exporter = BoundedExporter(sink)
    for record in (start, end):
        exporter(record)
    assert exporter.flush(timeout=2)
    assert exporter.close(timeout=2)
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert rows[-1]["usage"] == {"prompt_tokens": 7}
    assert "private" not in json.dumps(rows)


def test_langfuse_v5_fake_client_no_content_or_credentials():
    calls = []
    class Span:
        id = "a" * 16
        def update(self, **kwargs):
            calls.append(("update", kwargs))
        def end(self, **kwargs):
            calls.append(("end", kwargs))
    class Client:
        def start_observation(self, **kwargs):
            calls.append(("start", kwargs))
            return Span()
        def flush(self):
            calls.append(("flush", {}))
    sink = LangfuseSink(Client())
    for record in records():
        sink(record)
    sink.flush()
    sink.close()
    start = calls[0][1]
    assert start["as_type"] == "generation" and len(start["trace_context"]["trace_id"]) == 32
    assert calls[1][1]["usage_details"] == {"prompt_tokens": 7}
    assert not any("input" in kwargs or "output" in kwargs or "model_parameters" in kwargs for _, kwargs in calls)


def test_owned_langfuse_shutdown_runs_despite_flush_failure():
    class Client:
        shutdown_calls = 0
        def flush(self):
            raise OSError("private transport error")
        def shutdown(self):
            self.shutdown_calls += 1
    client = Client()
    exporter = BoundedExporter(LangfuseSink(client, own_client=True))
    assert not exporter.close(timeout=2)
    assert not exporter.is_alive  # thread stopped, but sink cleanup failed
    assert client.shutdown_calls == 1
    assert exporter.close_errors == 2 and exporter.export_errors == 2
