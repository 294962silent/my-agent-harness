"""Owned subprocess CUDA embeddings for Mem0; host imports no Torch/Transformers."""

from importlib import import_module, metadata
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from types import FunctionType

from my_agent_core._memory_qwen_worker import DIMENSIONS, MODEL_REPO, MODEL_REVISION, verify_model_files
from my_agent_core._memory_qwen_worker import query_text as query_text

SDK_METHOD_HASHES = {
    "Memory._link_entities_for_memory": "541310fb8cb50c4928d6b4e54c4be54a22bb6d2d9785b06141dc7c7fdc70a5e8",
    "Memory._add_to_vector_store": "a8cb8d1dea1b052f46a53c5948ea660d6b8e780f9f6295da99f6879232308ebb",
    "Memory._search_vector_store": "df48dc18844ab3b958f38986c10eda84d25fe48df62f18242ec30645d4eaeffd",
    "Memory._create_memory": "2352aea276ac1aea624105934d1e7f43d663e3b383af7f611de1ff5e4072c677",
    "Memory._update_memory": "6f3821347b175ab6c5e42ef5ea0a115fc732ab689d173f24752fc2f7decb251d",
    "Qdrant._get_bm25_encoder": "ed17bd30db8a3bb4a088ade39ef314efb9287f1e3f21de0bf93e2f14b0f52b50",
    "Qdrant.create_col": "28756da953ea7fb30b0cf3b7f201852e26dfe1a09669594c5e320cfa6e7b12a2",
    "Qdrant.insert": "430d7d982a28e41c7ebc2e1b2a9a1d8ee89d0ce744752c8e33b0962ccac8007a",
    "Qdrant.keyword_search": "a10ee016cdbc064c8b6141aeffbd9a347b88b52fc2286a7b6e4134ab95734d56",
    "Qdrant.update": "9b815d586d151ccf33c16fff8c4a72344251f4bc6126b29cbfe491a1e0416603",
}


def _dense_only_classes(sdk):
    """Isolated globals on owned subclasses; never patch SDK globals/other stores."""
    from mem0.vector_stores.qdrant import Qdrant
    from qdrant_client.models import Distance, VectorParams

    replacements = {
        "lemmatize_for_bm25": lambda text: text,
        "extract_entities": lambda text: [],
        "extract_entities_batch": lambda texts, **kwargs: [[] for _ in texts],
    }
    discovered = {
        "Memory." + name
        for name, fn in sdk.Memory.__dict__.items()
        if inspect.isfunction(fn) and set(replacements).intersection(fn.__code__.co_names)
    }
    if discovered != {key for key in SDK_METHOD_HASHES if key.startswith("Memory.")}:
        raise ValueError("Mem0 optional-NLP call sites differ from pinned compatibility contract")
    for key, expected in SDK_METHOD_HASHES.items():
        owner, name = key.split(".")
        fn = getattr(sdk.Memory if owner == "Memory" else Qdrant, name)
        source = inspect.getsource(fn).replace("\r\n", "\n")
        if hashlib.sha256(source.encode()).hexdigest() != expected:
            raise ValueError("Mem0 source method differs from pinned compatibility contract")
    methods = {}
    for key in discovered:
        name = key.split(".")[1]
        original = getattr(sdk.Memory, name)
        clone = FunctionType(
            original.__code__,
            {**original.__globals__, **replacements},
            name,
            original.__defaults__,
            original.__closure__,
        )
        clone.__kwdefaults__ = original.__kwdefaults__
        methods[name] = clone
    dense_memory = type("QwenDenseOnlyMemory", (sdk.Memory,), methods)

    class DenseOnlyQdrant(Qdrant):
        def create_col(self, vector_size, on_disk=False):
            self._bm25_encoder, self._has_bm25_slot = False, False
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE, on_disk=on_disk),
            )

        def _get_bm25_encoder(self):
            raise RuntimeError("sparse encoder forbidden for the Qwen dense-only profile")

        def keyword_search(self, query, top_k=5, filters=None):
            return None

    return dense_memory, DenseOnlyQdrant


def local_qwen_config(
    *,
    model_dir,
    storage_dir,
    python_executable,
    device="cuda:0",
    batch_size=1,
    max_length=1024,
    startup_timeout=120,
    request_timeout=30,
):
    if device != "cuda:0" or type(batch_size) is not int or not 1 <= batch_size <= 4:
        raise ValueError("Qwen GPU profile requires cuda:0 and batch_size 1..4")
    if type(max_length) is not int or not 64 <= max_length <= 1024:
        raise ValueError("Qwen initial token limit must be 64..1024")
    if any(
        type(t) not in (int, float) or not math.isfinite(t) or not 0 < t <= 600
        for t in (startup_timeout, request_timeout)
    ):
        raise ValueError("invalid GPU worker timeout")
    executable = Path(python_executable).absolute()
    if not executable.is_file():
        raise ValueError("explicit independent Python executable missing")
    return {
        "profile": "local_qwen_gpu",
        "model_dir": str(Path(model_dir).absolute()),
        "storage_dir": str(Path(storage_dir).absolute()),
        "python_executable": str(executable),
        "device": device,
        "batch_size": batch_size,
        "max_length": max_length,
        "startup_timeout": startup_timeout,
        "request_timeout": request_timeout,
    }


class QwenEmbeddingClient:
    def __init__(self, config, *, _worker_path=None, _verify=True):
        self.config = config
        self.client = self
        self._lock = threading.Lock()
        self._closed = False
        self._broken = False
        self._sequence = 0
        self._sender = None
        self._worker_handle = None
        self._worker_exit_confirmed = True
        if _verify:
            verify_model_files(config["model_dir"])
        worker = _worker_path or Path(__file__).with_name("_memory_qwen_worker.py")
        env = {
            k: v
            for k, v in os.environ.items()
            if not re.search(
                r"API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|OPENAI|ANTHROPIC|DEEPSEEK|LIVE_BENCH|PROXY", k, re.I
            )
            and k not in ("PYTHONPATH", "PYTHONHOME")
        }
        env.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
            DO_NOT_TRACK="1",
            CUDA_VISIBLE_DEVICES="0",
            TOKENIZERS_PARALLELISM="false",
        )
        self.worker_environment_keys = tuple(sorted(env))
        command = [
            config["python_executable"],
            "-I",
            "-X",
            "utf8",
            "-u",
            str(worker),
            "--model-dir",
            config["model_dir"],
            "--device",
            config["device"],
            "--batch-size",
            str(config["batch_size"]),
            "--max-length",
            str(config["max_length"]),
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            start_new_session=os.name == "posix",
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
        )
        self._responses = queue.Queue(maxsize=8)
        self._reader = threading.Thread(target=self._read, name="qwen-stdio-reader", daemon=True)
        self._reader.start()
        try:
            ready = self._receive(config["startup_timeout"])
            if (
                ready.get("ready") is not True
                or ready.get("dimensions") != DIMENSIONS
                or ready.get("device") != "cuda:0"
            ):
                code = ready.get("error")
                public_code = (
                    code if code in ("cuda_unavailable", "cuda_architecture_unsupported") else "startup_failed"
                )
                raise RuntimeError("CUDA embedding worker readiness: " + public_code)
            self.status = {
                **ready,
                "profile": "local_qwen_gpu",
                "model": MODEL_REPO,
                "model_revision": MODEL_REVISION,
                "worker_pid": ready.get("pid", self.pid),
                "launcher_pid": self.pid,
            }
            if os.name == "nt" and ready.get("pid", self.pid) != self.pid:
                import ctypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                self._kernel = kernel
                self._worker_handle = kernel.OpenProcess(0x00100000 | 0x1000, False, ready["pid"])
                if not self._worker_handle:
                    raise RuntimeError("cannot verify owned Windows GPU process handle")
                self._worker_exit_confirmed = False
        except BaseException:
            self.close()
            raise

    @property
    def pid(self):
        return self._process.pid

    @property
    def process_exited(self):
        if self._process.poll() is None:
            return False
        if self._worker_handle:
            return self._kernel.WaitForSingleObject(self._worker_handle, 0) == 0
        return self._worker_exit_confirmed

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(2 * 1024 * 1024 + 1)
                if not line:
                    self._responses.put_nowait(None)
                    break
                if len(line) > 2 * 1024 * 1024 or not line.endswith("\n"):
                    self._responses.put_nowait(None)
                    break
                self._responses.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            pass

    def _receive(self, timeout):
        try:
            line = self._responses.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("CUDA embedding worker deadline exceeded") from None
        if line is None:
            raise RuntimeError("CUDA embedding worker exited or violated protocol")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("invalid CUDA worker response")
        return value

    def embed(self, text, memory_action=None):
        return self.embed_batch([text], memory_action=memory_action)[0]

    def embed_batch(self, texts, memory_action=None):
        action = getattr(memory_action, "value", memory_action)
        if action not in (None, "add", "update", "search", "document"):
            raise ValueError("unsupported embedding action")
        if (
            not isinstance(texts, list)
            or not 1 <= len(texts) <= self.config["batch_size"]
            or any(not isinstance(t, str) or not t or len(t) > 65536 for t in texts)
        ):
            raise ValueError("invalid bounded embedding batch")
        with self._lock:
            if self._closed or self._broken or self.process_exited:
                raise RuntimeError("CUDA embedding worker closed or circuit open")
            self._sequence += 1
            request = {
                "id": self._sequence,
                "op": "embed",
                "texts": texts,
                "action": "search" if action == "search" else "document",
            }
            try:
                deadline = time.monotonic() + self.config["request_timeout"]
                sent = queue.Queue(maxsize=1)
                payload = json.dumps(request, ensure_ascii=False) + "\n"
                if len(payload.encode("utf-8")) > 1024 * 1024:
                    raise ValueError("CUDA embedding frame exceeds 1MiB")

                def send():
                    try:
                        self._process.stdin.write(payload)
                        self._process.stdin.flush()
                        sent.put_nowait(True)
                    except Exception:
                        sent.put_nowait(False)

                self._sender = threading.Thread(target=send, name="qwen-stdio-sender", daemon=True)
                self._sender.start()
                try:
                    written = sent.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise TimeoutError("CUDA embedding send deadline exceeded") from None
                if not written:
                    raise RuntimeError("CUDA embedding pipe write failed")
                result = self._receive(max(0, deadline - time.monotonic()))
                if result.get("id") != self._sequence:
                    raise RuntimeError("CUDA response identity differs")
                if "error" in result:
                    raise RuntimeError("CUDA embedding failed: " + str(result["error"])[:64])
                vectors = result.get("vectors")
                if not isinstance(vectors, list) or len(vectors) != len(texts):
                    raise RuntimeError("CUDA embedding batch differs")
                for vector in vectors:
                    if (
                        not isinstance(vector, list)
                        or len(vector) != DIMENSIONS
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                        or not 0.99 <= sum(v * v for v in vector) <= 1.01
                    ):
                        raise RuntimeError("CUDA vector dimensions/norm differ")
                self.status.update(result.get("metadata") or {})
                return vectors
            except BaseException:
                self._broken = True
                self.close()
                raise

    def close(self):
        self._closed = True
        if self._process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self._process.wait(timeout=10)
        if self._worker_handle:
            if self._kernel.WaitForSingleObject(self._worker_handle, 2000) != 0:
                subprocess.run(
                    ["taskkill", "/PID", str(self.status["worker_pid"]), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            if self._kernel.WaitForSingleObject(self._worker_handle, 2000) != 0:
                raise RuntimeError("owned GPU process remains alive")
            self._kernel.CloseHandle(self._worker_handle)
            self._worker_handle = None
            self._worker_exit_confirmed = True
        if self._sender is not None and threading.current_thread() is not self._sender:
            self._sender.join(timeout=2)
            if self._sender.is_alive():
                raise RuntimeError("owned CUDA sender did not terminate")
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                stream.close()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
            if self._reader.is_alive():
                raise RuntimeError("owned CUDA reader did not terminate")
        if not self.process_exited:
            raise RuntimeError("owned CUDA worker did not exit")


def create_local_qwen_index(config, *, timeout=2.0):
    allowed = {
        "profile",
        "model_dir",
        "storage_dir",
        "python_executable",
        "device",
        "batch_size",
        "max_length",
        "startup_timeout",
        "request_timeout",
    }
    if set(config) - allowed or config.get("profile") != "local_qwen_gpu":
        raise ValueError("unsupported Qwen GPU profile setting")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid Mem0 timeout")
    normalized = local_qwen_config(**{k: v for k, v in config.items() if k != "profile"})
    from my_agent_core.memory_local import MEM0_REVISION, NeverLLM

    dist = metadata.distribution("mem0ai")
    source = json.loads(dist.read_text("direct_url.json") or "{}")
    if dist.version != "2.2.1" or source.get("vcs_info", {}).get("commit_id") != MEM0_REVISION:
        raise ValueError("Qwen profile requires the pinned Mem0 OSS revision")
    for name in ("mem0.memory.main", "mem0.memory.telemetry"):
        loaded = __import__("sys").modules.get(name)
        if loaded is not None and getattr(loaded, "MEM0_TELEMETRY", False):
            raise RuntimeError("restart with MEM0_TELEMETRY=false")
    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    sdk = import_module("mem0.memory.main")
    storage = Path(normalized["storage_dir"])
    storage.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix="qwen1024-", dir=storage))

    def cleanup():
        if owned.resolve().parent != storage.resolve() or not owned.name.startswith("qwen1024-"):
            raise RuntimeError("owned Qwen index path differs")
        if owned.exists():
            shutil.rmtree(owned)

    try:
        return _assemble_index(sdk, normalized, owned, cleanup, NeverLLM, timeout)
    except BaseException:
        cleanup()
        raise


def _assemble_index(sdk, normalized, owned, cleanup, never_llm, timeout):
    memory_class, vector_class = _dense_only_classes(sdk)
    cfg = sdk.MemoryConfig(
        **{
            "llm": {"provider": "openai", "config": {"enable_vision": False}},
            "embedder": {"provider": "huggingface", "config": {"model": MODEL_REPO, "embedding_dims": DIMENSIONS}},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": str(owned / "vectors"),
                    "collection_name": "harness_local_qwen_gpu_1024",
                    "embedding_model_dims": DIMENSIONS,
                },
            },
            "history_db_path": str(owned / "history.sqlite"),
        }
    )
    embedder = QwenEmbeddingClient(normalized)
    memory = memory_class.__new__(memory_class)
    memory.config, memory.embedding_model, memory.llm = cfg, embedder, never_llm()
    memory.db, memory._entity_store, memory.reranker = None, None, None
    memory.collection_name, memory.api_version = cfg.vector_store.config.collection_name, cfg.version
    memory.custom_instructions = cfg.custom_instructions
    memory._harness_profile_metadata = embedder.status
    memory._harness_profile_metadata.update(
        bm25="disabled_owned_dense_store",
        nlp="disabled_isolated_sdk_globals",
        sdk_method_hashes=dict(SDK_METHOD_HASHES),
    )
    memory._harness_cleanup, memory._harness_cache_path = cleanup, owned
    try:
        memory.vector_store = vector_class(
            collection_name=cfg.vector_store.config.collection_name,
            embedding_model_dims=DIMENSIONS,
            path=str(owned / "vectors"),
        )
        memory.db = sdk.SQLiteManager(cfg.history_db_path)
    except BaseException:
        embedder.close()
        if getattr(memory, "vector_store", None) is not None:
            memory.vector_store.client.close()
        cleanup()
        raise
    from my_agent_core.memory_mem0 import Mem0Index

    return Mem0Index(memory, timeout=timeout, own_memory=True)
