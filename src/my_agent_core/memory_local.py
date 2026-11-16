"""Pinned Chinese CPU embedding profile; preparation and inference are separate.

The runtime only accepts a verified local model. Preparation downloads public
artifacts from the fixed official repository revision, without model/API auth.
"""
from __future__ import annotations

import hashlib
from importlib import import_module, metadata
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, urlopen

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REPO = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
MODEL_DIMENSIONS = 512
FASTEMBED_VERSION = "0.9.1"
MEM0_REVISION = "f1791daa86d469249c9b002f62644d58a1eb96a0"
# Independent official Git blob IDs from the pinned HF tree API. The large
# ONNX file is LFS-backed and uses its independently published SHA256 above.
MODEL_BLOBS = {
    "config.json": "60938626ad1097a0c1a14be4f8340e32c714a056",
    "ort_config.json": "31d3edac186bcfc7fb617662b7e0f750c7fef47a",
    "special_tokens_map.json": "a8b3208c2884c4efb86e49300fdd3dc877220cdf",
    "tokenizer.json": "cdb3043fc938fc918c06e66cf704c2ba58f88747",
    "tokenizer_config.json": "3a59388f0fd1bd22dec2ce7902c1be8e1fb84107",
    "vocab.txt": "ca4f9781030019ab9b253c6dcb8c7878b6dc87a5",
}
MODEL_FILES = (
    "config.json", "model_optimized.onnx", "ort_config.json", "special_tokens_map.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.txt",
)
logger = logging.getLogger(__name__)


def local_cpu_config(*, model_dir: str | Path, storage_dir: str | Path, threads: int = 2) -> dict:
    if type(threads) is not int or not 1 <= threads <= 32:
        raise ValueError("local CPU threads must be between 1 and 32")
    return {
        "profile": "local_cpu_zh", "model_dir": str(Path(model_dir).expanduser().resolve()),
        "storage_dir": str(Path(storage_dir).expanduser().resolve()), "threads": threads,
    }


def _fingerprint(path: Path) -> dict:
    body = path.read_bytes()
    return {"file": path.name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def verify_model_files(model_dir: str | Path) -> dict:
    root = Path(model_dir).expanduser().resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["repository"] != MODEL_REPO or manifest["revision"] != MODEL_REVISION:
            raise ValueError("unexpected model source")
        proofs = manifest["files"]
        if len(proofs) != len(MODEL_FILES) or {r["file"] for r in proofs} != set(MODEL_FILES):
            raise ValueError("unexpected model files")
        for proof in proofs:
            path = root / proof["file"]
            if path.is_symlink() or not path.resolve().is_relative_to(root) or _fingerprint(path) != proof:
                raise ValueError("model file differs from preparation manifest")
            if path.name == "model_optimized.onnx" and proof["sha256"] != MODEL_SHA256:
                raise ValueError("model ONNX differs from official SHA256")
            if path.name in MODEL_BLOBS:
                body = path.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
                if blob != MODEL_BLOBS[path.name]:
                    raise ValueError("model tokenizer/config differs from official Git blob")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Local Chinese model is missing or invalid; prepare the pinned model cache") from error


def prepare_model_cache(model_dir: str | Path) -> dict:
    """Explicit public-weight download. Runtime construction never calls this."""
    destination = Path(model_dir).expanduser().absolute()
    if destination.exists():
        return verify_model_files(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mem0-model-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "model"
        staging.mkdir()
        proofs, transports = [], {}
        for name in MODEL_FILES:
            url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
            request = Request(url, headers={"User-Agent": "my-agent-harness-local-memory"})
            path = staging / name
            transports[name] = _download_public(request, path)
            proofs.append(_fingerprint(path))
            logger.info("Downloaded pinned model artifact %s via %s", name, urlsplit(transports[name]).hostname)
        manifest = {
            "repository": MODEL_REPO, "revision": MODEL_REVISION,
            "model": MODEL_NAME, "dimensions": MODEL_DIMENSIONS, "license": "MIT",
            "source": f"https://huggingface.co/{MODEL_REPO}/tree/{MODEL_REVISION}", "files": proofs,
            "transports": transports,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verify_model_files(staging)
        staging.rename(destination)
    return manifest


def _download_public(request: Request, path: Path) -> str:
    proxies = getproxies()
    proxy = proxies.get("https")
    if proxy:
        parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if parts.username is not None or parts.password is not None:
            raise ValueError("authenticated proxy is not allowed for public model preparation")
    try:
        with urlopen(request, timeout=30) as response, path.open("xb") as output:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 110 * 1024 * 1024:
                    raise ValueError("model artifact exceeds expected bounded size")
                output.write(chunk)
        return request.full_url
    except OSError:
        # Windows curl does not automatically use WinINet's system proxy. Use
        # only an anonymous configured proxy, never curlrc/netrc/HF credentials.
        executable = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if executable is None:
            raise
        command = [executable, "--disable", "--fail", "--silent", "--show-error", "--location",
                   "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
                   "--max-time", "180", "--max-filesize", str(110 * 1024 * 1024)]
        if proxy:
            command.extend(["--proxy", proxy])
        # The mirror is transport only: every accepted artifact is checked
        # against the independent official Git blob/LFS hashes, never its claims.
        name = path.name
        urls = [request.full_url]
        if name != "model_optimized.onnx":
            urls.append(f"https://huggingface.co/{MODEL_REPO}/raw/{MODEL_REVISION}/{name}")
        urls.append(f"https://hf-mirror.com/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}")
        for url in urls:
            result = subprocess.run(command + [url, "--output", str(path)], capture_output=True,
                                    timeout=185, check=False)
            if result.returncode == 0:
                return url
        raise RuntimeError("Public model download failed for " + path.name
                           + "; curl status " + str(result.returncode)) from None


class NeverLLM:
    """No credentials, client or transport; infer=False must never call an LLM."""
    def generate_response(self, *args, **kwargs):
        raise RuntimeError("LLM inference is forbidden for the local CPU memory profile")


class CpuEmbedding:
    def __init__(self, model):
        self.dense_model = model
        self.client = self  # Owned Mem0Index cleanup releases the ORT session.

    def embed(self, text, memory_action=None):
        if self.dense_model is None:
            raise RuntimeError("local embedding is closed")
        vector = next(iter(self.dense_model.embed(text.replace("\n", " ")))).tolist()
        if len(vector) != MODEL_DIMENSIONS:
            raise ValueError("local Chinese embedding dimension differs from 512")
        return vector

    def close(self):
        self.dense_model = None


def _rollback_local_memory(memory, cleanup):
    """Attempt every owned cleanup without replacing the initialization error."""
    callbacks = [memory.embedding_model.close]
    if getattr(memory, "vector_store", None) is not None:
        callbacks.append(memory.vector_store.client.close)
    callbacks.append(cleanup)
    for callback in callbacks:
        try:
            callback()
        except BaseException:
            logger.warning("Local CPU resource retained after partial SDK initialization failure")


def create_local_cpu_index(config: dict, *, timeout: float = 2.0):
    """Local assembly for mem0 f179 Memory.__init__ (main.py:488-552).

    The pinned SDK's fastembed provider ignores providers/cache/threads. We
    reuse real Memory methods with explicit local components instead; no global
    SDK factory is patched, and no unused remote LLM client is constructed.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("local CPU memory timeout must be finite and positive")
    if set(config) - {"profile", "model_dir", "storage_dir", "threads"}:
        raise ValueError("unknown local CPU memory profile setting")
    if config.get("profile") != "local_cpu_zh":
        raise ValueError("unsupported local CPU memory profile")
    try:
        normalized = local_cpu_config(model_dir=config["model_dir"], storage_dir=config["storage_dir"],
                                      threads=config.get("threads", 2))
    except (KeyError, TypeError):
        raise ValueError("local CPU profile requires model_dir and storage_dir") from None
    verify_model_files(normalized["model_dir"])
    try:
        dist = metadata.distribution("mem0ai")
        source = json.loads(dist.read_text("direct_url.json") or "{}")
        if dist.version != "2.2.1" or source.get("vcs_info", {}).get("commit_id") != MEM0_REVISION:
            raise ValueError("local CPU profile requires the pinned Mem0 OSS revision")
        if metadata.version("fastembed") != FASTEMBED_VERSION:
            raise ValueError("local CPU profile requires fastembed==" + FASTEMBED_VERSION)
    except metadata.PackageNotFoundError:
        raise RuntimeError("local CPU memory dependencies are missing; install the mem0-local extra") from None
    # Telemetry has an import-time switch upstream. Never silently allow an
    # already-imported SDK to retain its enabled Posthog setting.
    for name in ("mem0.memory.main", "mem0.memory.telemetry"):
        loaded = sys.modules.get(name)
        if loaded is not None and getattr(loaded, "MEM0_TELEMETRY", False):
            raise RuntimeError("Restart with MEM0_TELEMETRY=false before loading the local memory profile")
    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from fastembed import TextEmbedding
    import onnxruntime
    sdk = import_module("mem0.memory.main")
    onnxruntime.disable_telemetry_events()
    dense = TextEmbedding(
        model_name=MODEL_NAME, providers=["CPUExecutionProvider"], cuda=False,
        threads=normalized["threads"], specific_model_path=normalized["model_dir"],
        cache_dir=normalized["model_dir"], local_files_only=True,
    )
    actual_providers = dense.model.model.get_providers()
    if actual_providers != ["CPUExecutionProvider"]:
        raise RuntimeError("local Chinese model did not initialize exclusively on CPU")
    storage = Path(normalized["storage_dir"])
    storage.mkdir(parents=True, exist_ok=True)
    # Weights are shared read-only; mutable vector/history caches are owned by
    # one adapter only, so unrelated CodingAgents/processes never share locks.
    instance_storage = Path(tempfile.mkdtemp(prefix="adapter-", dir=storage))

    def cleanup():
        target = instance_storage.resolve()
        if target.parent != storage.resolve() or not target.name.startswith("adapter-"):
            raise RuntimeError("local CPU cache cleanup escaped its owned directory")
        if target.exists():
            shutil.rmtree(target)

    sdk_config = sdk.MemoryConfig(**{
        "llm": {"provider": "openai", "config": {"enable_vision": False}},
        "embedder": {"provider": "fastembed", "config": {"model": MODEL_NAME, "embedding_dims": MODEL_DIMENSIONS}},
        "vector_store": {"provider": "qdrant", "config": {
            "path": str(instance_storage / "vectors"), "collection_name": "harness_local_cpu_zh",
            "embedding_model_dims": MODEL_DIMENSIONS,
        }},
        "history_db_path": str(instance_storage / "history.sqlite"),
    })
    memory = sdk.Memory.__new__(sdk.Memory)
    memory.config = sdk_config
    memory.embedding_model = CpuEmbedding(dense)
    memory.llm = NeverLLM()
    memory.db = None
    memory._entity_store = None
    memory.reranker = None
    memory.collection_name = sdk_config.vector_store.config.collection_name
    memory.api_version = sdk_config.version
    memory.custom_instructions = sdk_config.custom_instructions
    memory._harness_profile_metadata = {
        "profile": "local_cpu_zh", "model": MODEL_NAME, "model_revision": MODEL_REVISION,
        "dimensions": MODEL_DIMENSIONS, "providers": actual_providers,
        "threads": normalized["threads"], "fastembed_version": FASTEMBED_VERSION,
        "onnxruntime_version": metadata.version("onnxruntime"),
    }
    memory._harness_cleanup = cleanup
    memory._harness_cache_path = instance_storage
    try:
        memory.vector_store = sdk.VectorStoreFactory.create("qdrant", sdk_config.vector_store.config)
        memory.db = sdk.SQLiteManager(sdk_config.history_db_path)
    except BaseException:
        _rollback_local_memory(memory, cleanup)
        raise
    from my_agent_core.memory_mem0 import Mem0Index
    return Mem0Index(memory, timeout=timeout, own_memory=True)
