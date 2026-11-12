"""Optional real OSS SDK contract, fake embeddings/LLM, local vectors only.

This verifies SDK structure and no-extraction behavior, not semantic quality.
"""
import socket

import pytest


@pytest.mark.anyio
async def test_real_oss_sdk_infer_false_scope_metadata_search_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    module = pytest.importorskip("mem0.memory.main")
    pytest.importorskip("qdrant_client")
    monkeypatch.setattr(module, "MEM0_TELEMETRY", False)

    def no_network(*args, **kwargs):
        raise AssertionError("real SDK contract must not use a network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    class Embeddings:
        def embed(self, text, memory_action=None):
            return [1.0, 0.0, 0.0, 0.0]

    class NoLLM:
        def generate_response(self, *args, **kwargs):
            raise AssertionError("infer=False must never extract facts with an LLM")

    monkeypatch.setattr(module.EmbedderFactory, "create", lambda *args: Embeddings())
    monkeypatch.setattr(module.LlmFactory, "create", lambda *args: NoLLM())
    # Suppress informational notices; no Mem0 hosted client is constructed.
    for name in ("display_first_run_notice", "display_scale_threshold_notice", "display_temporal_usage_notice"):
        monkeypatch.setattr(module, name, lambda *a, **kw: None)
    config = {
        "vector_store": {"provider": "qdrant", "config": {
            "path": str(tmp_path / "vectors"), "collection_name": "contract", "embedding_model_dims": 4,
        }},
        "embedder": {"provider": "openai", "config": {"embedding_dims": 4}},
        "llm": {"provider": "openai", "config": {"enable_vision": False}},
        "history_db_path": str(tmp_path / "history.sqlite"),
    }
    memory = module.Memory.from_config(config)
    try:
        metadata = {"harness_identity": "id", "harness_revision": "v1", "harness_scope": "scope"}
        added = memory.add("Editor preference is Vim", user_id="scope", metadata=metadata, infer=False)
        remote_id = added["results"][0]["id"]
        assert added["results"][0]["event"] == "ADD"
        rows = memory.search("editor", filters={"user_id": "scope"}, top_k=3, threshold=0.0)["results"]
        assert rows[0]["id"] == remote_id
        assert rows[0]["metadata"] == metadata
        assert rows[0]["memory"] == "Editor preference is Vim"
        assert isinstance(rows[0]["score"], (float, int))
        assert memory.search("editor", filters={"user_id": "other"}, top_k=3)["results"] == []
        memory.delete(remote_id)
        assert memory.search("editor", filters={"user_id": "scope"}, top_k=3)["results"] == []
        from my_agent_core.memory import MemoryStore
        from my_agent_core.memory_mem0 import Mem0Index
        adapter = Mem0Index(memory, timeout=5, threshold=0.0, own_memory=True)
        store = MemoryStore(tmp_path / "facts", memory_index=adapter)
        store.load_from_disk()
        store.remember("editor", "Emacs")
        assert "Emacs" in await store.aretrieve("editing application")
        store.delete("editor")
        assert await store.aretrieve("editing application") is None
        assert await adapter.aclose()
        # Pinned Memory.close() alone leaves Qdrant's local path locked. The
        # production adapter must release its owned client before replacement.
        replacement = module.Memory.from_config(config)
        replacement.close()
        replacement.vector_store.client.close()
    finally:
        memory.close()
        memory.vector_store.client.close()
