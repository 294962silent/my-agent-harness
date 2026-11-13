"""Qwen host contracts: fake subprocess is not GPU quality evidence."""

import sys
from pathlib import Path

import pytest

from my_agent_core.memory_qwen import QwenEmbeddingClient, local_qwen_config, query_text


def fake_worker(tmp_path):
    path = tmp_path / "fake_worker.py"
    path.write_text(
        "import json,sys\nprint(json.dumps({'ready':True,'dimensions':1024,'device':'cuda:0','pid':__import__('os').getpid(),'test_fake':True,'stdio_encoding':sys.stdin.encoding}),flush=True)\nfor line in sys.stdin:\n x=json.loads(line)\n if x.get('op')=='embed':\n  print(json.dumps({'id':x['id'],'vectors':[[1.0]+[0.0]*1023 for _ in x['texts']],'metadata':{'test_fake':True}}),flush=True)\n"
    )
    return path


def test_strict_profile_and_query_document_separation(tmp_path):
    c = local_qwen_config(model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable)
    assert c["device"] == "cuda:0" and c["batch_size"] == 1 and c["max_length"] == 1024
    assert query_text("find this", "search").startswith("Instruct: ")
    assert query_text("same facts", "add") == "same facts"
    with pytest.raises(ValueError):
        local_qwen_config(
            model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable, device="cpu"
        )
    with pytest.raises(ValueError):
        local_qwen_config(
            model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable, batch_size=5
        )


def test_fake_process_vectors_and_confirmed_owned_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_SECRET")
    client = QwenEmbeddingClient(
        local_qwen_config(model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable),
        _worker_path=fake_worker(tmp_path),
        _verify=False,
    )
    pid = client.pid
    assert "LIVE_BENCH_API_KEY" not in client.worker_environment_keys
    v = client.embed("facts", memory_action="add")
    assert len(v) == 1024 and v[0] == 1
    assert client.embed("question", memory_action="search") == v
    assert client.pid == pid
    assert client.status["stdio_encoding"].lower() in ("utf-8", "utf8")
    client.close()
    assert client.process_exited
    with pytest.raises(RuntimeError):
        client.embed("after close")


def test_timeout_kills_worker_and_opens_circuit(tmp_path):
    worker = tmp_path / "hang.py"
    worker.write_text(
        "import json,time\nprint(json.dumps({'ready':True,'dimensions':1024,'device':'cuda:0'}),flush=True)\ntime.sleep(100)\n"
    )
    config = local_qwen_config(
        model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable, request_timeout=0.1
    )
    client = QwenEmbeddingClient(config, _worker_path=worker, _verify=False)
    with pytest.raises(TimeoutError):
        client.embed("facts")
    assert client.process_exited
    with pytest.raises(RuntimeError):
        client.embed("no retry")
    client.close()


def test_owned_dense_shim_does_not_patch_sdk_or_other_qdrant(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    sdk = pytest.importorskip("mem0.memory.main", reason="optional pinned Mem0 SDK contract")
    from mem0.vector_stores.qdrant import Qdrant
    from my_agent_core.memory_qwen import _dense_only_classes

    original_lemma = sdk.lemmatize_for_bm25
    original_entities = sdk.extract_entities
    original_method = sdk.Memory._search_vector_store
    memory_class, vector_class = _dense_only_classes(sdk)
    assert sdk.lemmatize_for_bm25 is original_lemma and sdk.extract_entities is original_entities
    assert sdk.Memory._search_vector_store is original_method
    assert memory_class._search_vector_store.__globals__["lemmatize_for_bm25"]("UNCHANGED") == "UNCHANGED"
    assert memory_class._search_vector_store.__globals__["extract_entities"]("text") == []
    owned = vector_class(collection_name="owned", embedding_model_dims=1024, path=str(tmp_path / "owned"))
    ordinary = Qdrant(collection_name="unrelated", embedding_model_dims=512, path=str(tmp_path / "ordinary"))
    try:
        assert not owned._has_bm25_slot and owned._bm25_encoder is False
        assert ordinary._has_bm25_slot and ordinary._bm25_encoder is None
        assert owned.keyword_search("anything") is None
        with pytest.raises(RuntimeError, match="forbidden"):
            owned._get_bm25_encoder()
    finally:
        owned.client.close()
        ordinary.client.close()


def test_changed_sdk_source_fails_closed_before_worker(monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    sdk = pytest.importorskip("mem0.memory.main", reason="optional pinned Mem0 SDK contract")
    from my_agent_core import memory_qwen

    pins = dict(memory_qwen.SDK_METHOD_HASHES)
    pins["Memory._search_vector_store"] = "0" * 64
    monkeypatch.setattr(memory_qwen, "SDK_METHOD_HASHES", pins)
    with pytest.raises(ValueError, match="source method differs"):
        memory_qwen._dense_only_classes(sdk)


def test_official_model_hash_gate_rejects_incomplete_source(tmp_path):
    from my_agent_core._memory_qwen_worker import verify_model_files

    with pytest.raises(ValueError, match="missing or size"):
        verify_model_files(tmp_path)


def test_real_mem0_methods_dense_shim_avoid_optional_network_models(tmp_path, monkeypatch):
    import socket

    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    sdk = pytest.importorskip("mem0.memory.main", reason="optional pinned Mem0 SDK contract")
    from my_agent_core import memory_qwen
    from my_agent_core.memory_local import NeverLLM

    class Embedder:
        def __init__(self, config):
            self.status = {}
            self.client = self
            self.closed = False

        def embed(self, *a, **kw):
            return [1.0] + [0.0] * 1023

        def embed_batch(self, texts, *a, **kw):
            return [self.embed(t) for t in texts]

        def close(self):
            self.closed = True

    monkeypatch.setattr(memory_qwen, "QwenEmbeddingClient", Embedder)

    def forbidden(*a, **kw):
        raise AssertionError("optional network/model construction forbidden")

    import fastembed
    import mem0.utils.spacy_models as spacy_models

    monkeypatch.setattr(fastembed, "SparseTextEmbedding", forbidden)
    monkeypatch.setattr(spacy_models, "_ensure_model_available", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    owned = tmp_path / "owned"
    owned.mkdir()
    index = memory_qwen._assemble_index(sdk, {}, owned, lambda: None, NeverLLM, 10)
    try:
        result = index.memory.add("local text only", user_id="scope", metadata={"id": "fixture"}, infer=False)
        assert result["results"]
        assert index.memory.search("local text", filters={"user_id": "scope"}, rerank=False)["results"]
    finally:
        index._close_sdk()
    assert index.memory.embedding_model.closed


def test_worker_python_guard_denies_default_and_explicit_internet_sockets():
    import subprocess
    from my_agent_core import _memory_qwen_worker

    script = (
        "import runpy,socket;ns=runpy.run_path("
        + repr(str(Path(_memory_qwen_worker.__file__)))
        + ");ns['deny_internet_sockets']()\nfor family in (None,socket.AF_INET,socket.AF_INET6):\n try: socket.socket() if family is None else socket.socket(family)\n except PermissionError: pass\n else: raise AssertionError('Internet socket allowed')\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


def test_send_deadline_covers_ready_worker_not_reading_large_payload(tmp_path):
    import threading

    worker = tmp_path / "not_reading.py"
    worker.write_text(
        "import json,time\nprint(json.dumps({'ready':True,'dimensions':1024,'device':'cuda:0'}),flush=True)\ntime.sleep(100)\n"
    )
    config = local_qwen_config(
        model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable, request_timeout=0.1
    )
    client = QwenEmbeddingClient(config, _worker_path=worker, _verify=False)
    errors = []

    def request():
        try:
            client.embed("x" * 65536)
        except Exception as exc:
            errors.append(type(exc))

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    thread.join(3)
    try:
        assert not thread.is_alive(), "sending must share the embedding request deadline"
        assert errors == [TimeoutError]
    finally:
        client.close()
        thread.join(2)
    assert client.process_exited and not thread.is_alive()


def test_dead_stdin_flush_during_shutdown_preserves_request_timeout(tmp_path):
    worker = tmp_path / "no_response.py"
    worker.write_text(
        "import json,time\nprint(json.dumps({'ready':True,'dimensions':1024,'device':'cuda:0'}),flush=True)\ntime.sleep(100)\n"
    )
    config = local_qwen_config(
        model_dir=tmp_path, storage_dir=tmp_path / "store", python_executable=sys.executable, request_timeout=0.1
    )
    client = QwenEmbeddingClient(config, _worker_path=worker, _verify=False)
    original = client._process.stdin

    class DeadPipeClose:
        def write(self, value):
            return original.write(value)

        def flush(self):
            return original.flush()

        def close(self):
            original.close()
            raise BrokenPipeError("buffer flush after owned process termination")

    client._process.stdin = DeadPipeClose()
    try:
        with pytest.raises(TimeoutError):
            client.embed("facts")
        assert client.process_exited and not client._reader.is_alive() and not client._sender.is_alive()
    finally:
        try:
            client.close()
        except BrokenPipeError:
            pass
