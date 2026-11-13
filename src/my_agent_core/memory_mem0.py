"""OSS Mem0 candidate cache, pinned contract f1791daa / mem0ai 2.2.1.

One daemon worker at a time. A timeout opens the circuit permanently; it does
not cancel a blocking SDK request. No model/embedding call occurs on import.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from copy import deepcopy
import math
import threading

from my_agent_core.memory_semantic import MemoryCandidate, MemoryRecord


class Mem0Index:
    def __init__(self, memory, *, timeout: float = 2.0, top_k: int = 8, threshold: float = 0.1,
                 own_memory: bool = False):
        if not math.isfinite(timeout) or timeout <= 0 or top_k < 1 or not 0 <= threshold <= 1:
            raise ValueError("invalid Mem0 candidate index bounds")
        self.memory = memory
        self.timeout = timeout
        self.top_k = top_k
        self.threshold = threshold
        self._future: Future | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._broken = False
        self._own_memory = own_memory
        self._sdk_closed = False
        self._known: dict[tuple[str, str], tuple[str, str]] = {}

    def _close_sdk(self):
        if self._own_memory and not self._sdk_closed:
            # Memory.close() at the pinned revision closes SQLite only. Release
            # factory-owned vector/HTTP clients too (not caller-injected ones).
            resources = [self.memory]
            for component in ("vector_store", "embedding_model", "llm", "_entity_store"):
                resource = getattr(getattr(self.memory, component, None), "client", None)
                if resource is not None and all(resource is not old for old in resources):
                    resources.append(resource)
            errors = []
            for resource in resources:
                close = getattr(resource, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as error:
                        errors.append(error)
            if errors:
                raise RuntimeError("Mem0 owned resource close failed") from None
            cleanup = getattr(self.memory, "_harness_cleanup", None)
            if cleanup is not None:
                cleanup()
            self._sdk_closed = True

    @property
    def inflight(self) -> bool:
        return self._future is not None and not self._future.done()

    @property
    def status(self) -> dict:
        """Metadata only; a timeout never means the SDK worker was terminated."""
        result = {
            "provider": "mem0", "inflight": self.inflight, "circuit_open": self._broken,
            "closed": self._closed, "sdk_owned": self._own_memory, "sdk_closed": self._sdk_closed,
            "indexed_identities": len(self._known),
        }
        profile = getattr(self.memory, "_harness_profile_metadata", None)
        if profile is not None:
            result["local_profile"] = dict(profile)
        return result

    def _work(self, scope, query, records):
        wanted = {r["identity"]: r for r in records}
        for (old_scope, identity), (revision, remote_id) in list(self._known.items()):
            record = wanted.get(identity) if old_scope == scope else None
            if old_scope == scope and (record is None or record["revision"] != revision):
                self.memory.delete(remote_id)
                del self._known[(old_scope, identity)]
        for record in records:
            identity, revision = record["identity"], record["revision"]
            if (scope, identity) in self._known:
                continue
            metadata = {"harness_identity": identity, "harness_revision": revision, "harness_scope": scope}
            # Discover an existing matching cache entry after a process restart.
            existing = self.memory.search(
                query=record["text"], top_k=1, threshold=0.0,
                filters={"user_id": scope, **metadata}, rerank=False,
            )
            remote_id = next((r.get("id") for r in existing.get("results", [])
                              if r.get("metadata") == metadata and isinstance(r.get("id"), str)), None)
            if remote_id is None:
                added = self.memory.add(record["text"], user_id=scope, metadata=metadata, infer=False)
                rows = added.get("results", [])
                if len(rows) != 1 or not isinstance(rows[0].get("id"), str):
                    raise ValueError("Mem0 add returned no unique memory ID")
                remote_id = rows[0]["id"]
            self._known[(scope, identity)] = (revision, remote_id)
        result = self.memory.search(
            query=query, filters={"user_id": scope}, top_k=self.top_k,
            threshold=self.threshold, rerank=False,
        )
        candidates = []
        for row in result["results"]:
            metadata = row.get("metadata") or {}
            if all(isinstance(metadata.get(k), str) for k in ("harness_scope", "harness_identity", "harness_revision")):
                candidates.append(MemoryCandidate(
                    scope=metadata["harness_scope"], identity=metadata["harness_identity"],
                    revision=metadata["harness_revision"],
                ))
        return candidates

    async def search(self, *, scope: str, query: str, records: list[MemoryRecord]) -> list[MemoryCandidate]:
        with self._lock:
            if self._closed or self._broken or self.inflight:
                raise RuntimeError("Mem0 candidate index closed, unavailable or already in flight")
            future: Future = Future()
            self._future = future

        def run():
            result, failure = None, None
            try:
                result = self._work(scope, query, records)
            except BaseException as error:
                failure = error
            with self._lock:
                # Commit completion under the same lock as request_close: either
                # this worker cleans up, or request_close starts the finalizer.
                if not self._closed:
                    if failure is None:
                        future.set_result(result)
                    else:
                        future.set_exception(failure)
                    return
            try:
                self._close_sdk()
            except BaseException as error:
                if failure is None:
                    failure = error
            if failure is None:
                future.set_result(result)
            else:
                future.set_exception(failure)

        threading.Thread(target=run, name="memory-mem0", daemon=True).start()
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), self.timeout)
        except (Exception, asyncio.CancelledError):
            self._broken = True
            # Retrieve late exceptions without cancelling the blocking worker.
            wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            raise

    def request_close(self) -> None:
        """Begin owned cleanup even if the constructing host cannot await yet."""
        with self._lock:
            self._closed = True
            start_finalizer = not self.inflight and self._own_memory and not self._sdk_closed
            if start_finalizer:
                future: Future = Future()
                self._future = future
        if start_finalizer:

            def close():
                try:
                    self._close_sdk()
                    future.set_result(None)
                except BaseException as error:
                    future.set_exception(error)

            threading.Thread(target=close, name="memory-mem0-close", daemon=True).start()

    async def aclose(self, timeout: float = 0.1) -> bool:
        self.request_close()
        if self._future is None:
            return True
        wrapped = asyncio.wrap_future(self._future)
        try:
            await asyncio.wait_for(asyncio.shield(wrapped), max(0, timeout))
        except TimeoutError:
            wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            return False
        except Exception:
            # Work may have failed while its finally still released every owned
            # resource. Closing status describes cleanup, not retrieval success.
            return not self.inflight and (not self._own_memory or self._sdk_closed)
        return not self.inflight and (not self._own_memory or self._sdk_closed)


def create_mem0_index(config: dict, *, timeout: float = 2.0) -> Mem0Index:
    """Explicit OSS-only factory. SDK import/config validation never run disabled."""
    if not isinstance(config, dict):
        raise ValueError("Mem0 configuration must be a dictionary")
    if "profile" in config:
        if config["profile"] == "local_qwen_gpu":
            from my_agent_core.memory_qwen import create_local_qwen_index
            return create_local_qwen_index(config, timeout=timeout)
        from my_agent_core.memory_local import create_local_cpu_index
        return create_local_cpu_index(config, timeout=timeout)
    config = deepcopy(config)
    if any(not isinstance(config.get(key), dict) or not config[key] for key in ("llm", "embedder", "vector_store")):
        raise ValueError("Mem0 requires explicit llm, embedder and vector_store configuration")
    llm_config = config["llm"].setdefault("config", {})
    if not isinstance(llm_config, dict):
        raise ValueError("Mem0 llm.config must be a dictionary")
    if llm_config.get("enable_vision"):
        raise ValueError("Mem0 candidate index does not allow vision")
    llm_config["enable_vision"] = False
    try:
        from mem0 import Memory
    except ImportError as error:
        raise RuntimeError("Mem0 is enabled but the optional mem0ai SDK is unavailable") from error
    try:
        memory = Memory.from_config(config)
    except Exception as error:
        raise ValueError("Mem0 configuration initialization failed (" + type(error).__name__ + ")") from None
    return Mem0Index(memory, timeout=timeout, own_memory=True)
