"""Real pinned Mem0/Qdrant with explicit local CPU adapter assembly.

The contract uses a fake local vector model and blocks sockets; the opt-in
weight smoke uses the real prepared model and remains a functional smoke only.
"""
import os
from pathlib import Path
import socket

import pytest


def _deny_network(*args, **kwargs):
    raise AssertionError("local CPU memory must not use a network API or telemetry")


@pytest.mark.anyio
async def test_pinned_local_assembly_real_sdk_no_keys_or_network(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    fastembed = pytest.importorskip("fastembed")
    sdk = pytest.importorskip("mem0.memory.main")
    telemetry = pytest.importorskip("mem0.memory.telemetry")
    monkeypatch.setattr(sdk, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(telemetry, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    from my_agent_core import memory_local
    from my_agent_core.memory import MemoryStore
    from my_agent_core.memory_mem0 import create_mem0_index
    monkeypatch.setattr(memory_local, "verify_model_files", lambda path: {})
    calls = []

    class Vector:
        def tolist(self):
            return [1.0] + [0.0] * 511

    class LocalModel(fastembed.TextEmbedding):
        def __init__(self, **kwargs):
            from types import SimpleNamespace
            calls.append(kwargs)
            self.model = SimpleNamespace(model=SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"]))

        def embed(self, text):
            yield Vector()

    monkeypatch.setattr(fastembed, "TextEmbedding", LocalModel)
    config = memory_local.local_cpu_config(model_dir=tmp_path / "model", storage_dir=tmp_path / "cache")
    first = create_mem0_index(config, timeout=5)
    second = create_mem0_index(config, timeout=5)
    first_cache, second_cache = first.memory._harness_cache_path, second.memory._harness_cache_path
    try:
        assert first_cache != second_cache
        assert first.status["local_profile"]["model_revision"] == memory_local.MODEL_REVISION
        assert first.status["local_profile"]["providers"] == ["CPUExecutionProvider"]
        for kwargs in calls:
            assert kwargs["providers"] == ["CPUExecutionProvider"]
            assert kwargs["cuda"] is False and kwargs["local_files_only"] is True
            assert kwargs["threads"] == 2
        assert isinstance(first.memory.llm, memory_local.NeverLLM)
        store = MemoryStore(tmp_path / "facts", memory_index=first)
        store.load_from_disk()
        store.remember("编辑器", "用户日常使用Vim")
        assert "Vim" in await store.aretrieve("常用编辑工具")
        store.delete("编辑器")
        assert await store.aretrieve("常用编辑工具") is None
    finally:
        assert await first.aclose(timeout=5)
        assert first.memory.embedding_model.dense_model is None
        assert not first_cache.exists()
        assert second_cache.exists()
        assert await second.aclose(timeout=5)
        assert not second_cache.exists()


@pytest.mark.anyio
async def test_real_local_chinese_model_functional_smoke(tmp_path, monkeypatch):
    import weakref
    location = os.environ.get("MY_AGENT_LOCAL_MODEL_DIR")
    if not location:
        pytest.skip("set MY_AGENT_LOCAL_MODEL_DIR to a prepared pinned model; never downloads in tests")
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    pytest.importorskip("fastembed")
    sdk = pytest.importorskip("mem0.memory.main")
    telemetry = pytest.importorskip("mem0.memory.telemetry")
    monkeypatch.setattr(sdk, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(telemetry, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    from my_agent_core.memory import MemoryStore
    from my_agent_core.memory_local import local_cpu_config
    from my_agent_core.memory_mem0 import create_mem0_index
    config = local_cpu_config(model_dir=Path(location), storage_dir=tmp_path / "cache")
    index = create_mem0_index(config, timeout=30)
    model_ref = weakref.ref(index.memory.embedding_model.dense_model)
    try:
        vector = index.memory.embedding_model.embed("中文长期记忆检索")
        assert len(vector) == 512
        assert index.memory.embedding_model.dense_model.model.model.get_providers() == ["CPUExecutionProvider"]
        store = MemoryStore(tmp_path / "facts", memory_index=index)
        store.load_from_disk()
        store.remember("编辑器偏好", "用户日常使用Vim编辑代码")
        lexical = store.retrieve
        monkeypatch.setattr(store, "retrieve", lambda *a, **kw: pytest.fail("real-model smoke unexpectedly fell back"))
        assert "Vim" in await store.aretrieve("用户写代码常用什么编辑器")
        assert not index.status["circuit_open"]
        monkeypatch.setattr(store, "retrieve", lexical)
        store.delete("编辑器偏好")
        assert await store.aretrieve("用户写代码常用什么编辑器") is None
    finally:
        assert await index.aclose(timeout=5)
        assert index.memory.embedding_model.dense_model is None
        assert model_ref() is None
