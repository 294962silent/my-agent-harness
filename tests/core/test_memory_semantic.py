"""Offline contracts: semantic candidates never become authoritative facts."""
import asyncio
import threading

import pytest

from my_agent_core.memory import MemoryStore


class Candidates:
    def __init__(self):
        self.rows = []
        self.closed = False

    async def search(self, *, scope, query, records):
        self.records = records
        self.scope = scope
        return self.rows or list(records)

    async def aclose(self, timeout=0.1):
        self.closed = True
        return True


@pytest.mark.anyio
async def test_local_revision_delete_and_scope_win_over_remote(tmp_path):
    index = Candidates()
    store = MemoryStore(tmp_path, memory_index=index)
    store.load_from_disk()
    store.remember("editor", "Vim")
    first = await store.aretrieve("preferred editing application")
    assert "Vim" in first
    stale = list(index.records)
    store.remember("editor", "Emacs")
    index.rows = stale
    assert await store.aretrieve("preferred editing application") is None
    store.delete("editor")
    assert await store.aretrieve("preferred editing application") is None
    assert not store._facts
    assert store._deleted()
    store.remember("editor", "Emacs")
    index.rows = [dict(stale[0], scope="another-workspace")]
    assert await store.aretrieve("preferred editing application") is None


@pytest.mark.anyio
async def test_recheck_after_async_search_and_failure_fallback(tmp_path):
    class Deleting(Candidates):
        async def search(self, **kwargs):
            store.delete("editor")
            return kwargs["records"]

    store = MemoryStore(tmp_path, memory_index=Deleting())
    store.load_from_disk()
    store.remember("editor", "Vim")
    assert await store.aretrieve("editor") is None

    class Broken(Candidates):
        async def search(self, **kwargs):
            raise RuntimeError("offline")

    store = MemoryStore(tmp_path, memory_index=Broken())
    store.load_from_disk()
    store.remember("editor", "Emacs")
    assert "Emacs" in await store.aretrieve("editor")


@pytest.mark.anyio
async def test_mem0_contract_and_single_inflight_timeout():
    from my_agent_core.memory_mem0 import Mem0Index

    class SDK:
        def __init__(self):
            self.calls = []
            self.rows = []

        def search(self, **kwargs):
            self.calls.append(("search", kwargs))
            return {"results": self.rows}

        def add(self, messages, **kwargs):
            self.calls.append(("add", kwargs))
            assert kwargs["infer"] is False
            assert "memory_type" not in kwargs
            self.rows = [{"id": "remote", "metadata": kwargs["metadata"]}]
            return {"results": [{"id": "remote"}]}

        def delete(self, memory_id):
            self.calls.append(("delete", memory_id))

    sdk = SDK()
    index = Mem0Index(sdk, timeout=0.1)
    record = {"scope": "scope", "identity": "id", "revision": "rev", "text": "safe fact"}
    result = await index.search(scope="scope", query="query", records=[record])
    assert result == [{k: record[k] for k in ("scope", "identity", "revision")}]
    await index.search(scope="scope", query="query", records=[record])
    assert sum(name == "add" for name, _ in sdk.calls) == 1
    await index.search(scope="scope", query="query", records=[])
    assert ("delete", "remote") in sdk.calls
    assert await index.aclose()


@pytest.mark.anyio
async def test_owned_sdk_close_is_bounded_and_repeated_close_is_truthful():
    from my_agent_core.memory_mem0 import Mem0Index
    entered, release = threading.Event(), threading.Event()

    class SDK:
        def close(self):
            entered.set()
            release.wait(2)

    index = Mem0Index(SDK(), own_memory=True)
    close = asyncio.create_task(index.aclose(timeout=0.01))
    assert await asyncio.to_thread(entered.wait, 2)
    assert await close is False
    assert index.inflight
    release.set()
    assert await index.aclose(timeout=2) is True


def test_factory_rejects_vision_before_importing_sdk():
    from my_agent_core.memory_mem0 import create_mem0_index
    with pytest.raises(ValueError, match="vision"):
        create_mem0_index({"llm": {"config": {"enable_vision": True}},
                           "embedder": {"provider": "x"}, "vector_store": {"provider": "x"}})


@pytest.mark.anyio
async def test_hanging_sdk_does_not_accumulate_workers():
    from my_agent_core.memory_mem0 import Mem0Index
    entered, release = threading.Event(), threading.Event()

    class Hanging:
        def search(self, **kwargs):
            entered.set()
            release.wait(2)
            return {"results": []}

    index = Mem0Index(Hanging(), timeout=0.01)
    # Synchronize worker entry rather than assuming OS scheduling within 10 ms.
    operation = asyncio.create_task(index.search(scope="scope", query="q", records=[]))
    assert await asyncio.to_thread(entered.wait, 2)
    with pytest.raises(TimeoutError):
        await operation
    assert entered.is_set() and index.inflight
    assert index.status["circuit_open"] and index.status["inflight"]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await index.search(scope="scope", query="q", records=[])
    assert not await index.aclose(timeout=0.001)
    release.set()
    for _ in range(100):
        if not index.inflight:
            break
        await asyncio.sleep(0.001)
    assert await index.aclose()


@pytest.mark.anyio
async def test_inflight_failure_still_closes_owned_resources_after_request_close():
    from my_agent_core.memory_mem0 import Mem0Index
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Failing:
        def search(self, **kwargs):
            entered.set()
            release.wait(2)
            raise RuntimeError("failed search")

        def close(self):
            closed.set()

    index = Mem0Index(Failing(), timeout=2, own_memory=True)
    operation = asyncio.create_task(index.search(scope="s", query="q", records=[]))
    assert await asyncio.to_thread(entered.wait, 2)
    index.request_close()
    release.set()
    with pytest.raises(RuntimeError, match="failed search"):
        await operation
    assert closed.is_set()
    assert index.status["sdk_closed"]
    assert await index.aclose()


@pytest.mark.anyio
async def test_direct_agent_cleanup_failure_still_closes_owned_index(tmp_path):
    from unittest.mock import AsyncMock
    from my_agent_core.agent import Agent
    from my_agent_core.session import Session
    index = Candidates()
    agent = Agent(llm=object(), tools=[], session=Session(path=tmp_path / "s.jsonl"),
                  memory_dir=False, memory_index=index, memory_index_owned=True)
    agent._memory_distiller.close = AsyncMock(side_effect=RuntimeError("distiller close failed"))
    with pytest.raises(RuntimeError, match="distiller close failed"):
        await agent.aclose()
    assert index.closed
    assert agent.memory_index_close_status is True
