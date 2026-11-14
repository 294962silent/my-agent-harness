"""Owned dense-only Mem0 profiles: same backend/search for raw and fact arms."""

from __future__ import annotations

from importlib import import_module, metadata
import json
import os
from pathlib import Path
import shutil
import tempfile

from my_agent_core.memory_local import MEM0_REVISION, NeverLLM
from my_agent_core.memory_qwen import QwenEmbeddingClient, _dense_only_classes


EXTRACTION_INSTRUCTIONS = (
    "Return only the native JSON object with a memory array. Extract concise atomic facts "
    "from the named speakers, preserving names, preferences, changes, dates and negations. "
    "The source includes its original session date; resolve relative times against that "
    "date, never the current wall clock. Include the relevant absolute date in temporal facts. "
    "Do not answer benchmark questions or invent facts. Use at most 800 characters per fact."
)


def create_study_memory(config):
    """Assemble without any SDK default paid client, retaining native ADD logic.

    The controlled adapter is attached only for infer=True. Search has no LLM.
    The shared dense-only subclass disables optional BM25/entity paths equally
    in raw and fact arms; this is explicitly not the default native hybrid.
    """
    dist = metadata.distribution("mem0ai")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if dist.version != "2.2.1" or direct.get("vcs_info", {}).get("commit_id") != MEM0_REVISION:
        raise ValueError("pinned native Mem0 build differs")
    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    sdk = import_module("mem0.memory.main")
    memory_class, vector_class = _dense_only_classes(sdk)
    storage = Path(config["storage_dir"])
    storage.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix="locomo-", dir=storage))
    if config["max_length"] != 4096 or config["batch_size"] != 1:
        raise ValueError("study embedding limits differ")
    if os.name == "nt":
        from experiments.memory_validation.multisession_embedding import WindowsQwenClient

        embedding = WindowsQwenClient(config)
    else:
        embedding = QwenEmbeddingClient(config, _worker_path=Path(__file__).with_name("qwen_seccomp_worker.py"))
    memory = memory_class.__new__(memory_class)
    cfg = sdk.MemoryConfig(
        **{
            "llm": {"provider": "openai", "config": {"enable_vision": False}},
            "embedder": {
                "provider": "huggingface",
                "config": {"model": "Qwen/Qwen3-Embedding-0.6B", "embedding_dims": 1024},
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": str(owned / "vectors"),
                    "collection_name": "locomo_dense_1024",
                    "embedding_model_dims": 1024,
                },
            },
            "history_db_path": str(owned / "history.sqlite"),
            "custom_instructions": EXTRACTION_INSTRUCTIONS,
        }
    )
    memory.config, memory.embedding_model, memory.llm = cfg, embedding, NeverLLM()
    memory.collection_name, memory.api_version = cfg.vector_store.config.collection_name, cfg.version
    memory.custom_instructions = cfg.custom_instructions
    memory._entity_store, memory.reranker, memory.enable_graph = None, None, False
    memory.db, memory.vector_store = None, None
    try:
        memory.vector_store = vector_class(
            collection_name=memory.collection_name, embedding_model_dims=1024, path=str(owned / "vectors")
        )
        memory.db = sdk.SQLiteManager(cfg.history_db_path)
    except BaseException:
        embedding.close()
        shutil.rmtree(owned)
        raise
    return StudyMemory(memory, owned)


class StudyMemory:
    def __init__(self, memory, owned):
        self.memory, self.owned, self.closed = memory, owned, False

    def search(self, query, scope):
        return self.memory.search(query, filters={"user_id": scope}, top_k=8, threshold=0, rerank=False)["results"]

    def close(self):
        if self.closed:
            return
        errors = []
        for close in (self.memory.close, self.memory.vector_store.client.close, self.memory.embedding_model.close):
            try:
                close()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("owned study memory close failed")
        self.closed = True
        shutil.rmtree(self.owned)
