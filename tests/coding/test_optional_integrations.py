"""Offline assembly and ownership for opt-in integrations."""
from unittest.mock import Mock

import pytest

from my_agent_core.session import Session
from my_agent_llm import Response
from my_coding_agent.agent import CodingAgent
from my_coding_agent.settings import Settings


class LLM:
    async def achat_stream(self, **kwargs):
        self.messages = kwargs.get("messages")
        yield Response(content="ok", model="fake")


class Index:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def search(self, **kwargs):
        self.calls += 1
        return kwargs["records"]

    async def aclose(self, timeout=0.1):
        self.closed = True
        return True


@pytest.mark.anyio
async def test_injected_index_used_but_caller_owned(tmp_path):
    index = Index()
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=tmp_path / "memory", memory_index=index, auto_load_mcp=False)
    coding.memory_store.remember("editor", "Vim")
    await coding.run("which application do I use for editing?")
    assert index.calls == 1
    assert await coding.aclose()
    assert not index.closed


@pytest.mark.anyio
async def test_factory_ownership_and_observation_env(tmp_path, monkeypatch):
    import my_coding_agent.agent as module
    index = Index()
    memory_factory = Mock(return_value=index)
    monkeypatch.setattr(module, "create_mem0_index", memory_factory)
    runtime = Mock()
    runtime.close.return_value = False
    observation_factory = Mock(return_value=runtime)
    monkeypatch.setattr(module, "create_observation_runtime", observation_factory)
    monkeypatch.setenv("TEST_OBS_PUBLIC", "public")
    monkeypatch.setenv("TEST_OBS_SECRET", "secret")
    settings = Settings(memory_index={"enabled": True, "config": {"test": True}},
                        observation={"enabled": True, "public_key_env": "TEST_OBS_PUBLIC",
                                     "secret_key_env": "TEST_OBS_SECRET", "close_timeout": 0.01})
    llm = LLM()
    coding = CodingAgent(workspace=tmp_path, llm=llm, session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=tmp_path / "m", memory_index_config=settings.memory_index,
                         observation_config=settings.observation, auto_load_mcp=False)
    memory_factory.assert_called_once()
    runtime.attach.assert_called_once_with(llm)
    assert observation_factory.call_args.kwargs["secret_key"] == "secret"
    assert not await coding.aclose()
    assert index.closed
    assert coding.close_status["observation"] is False


def test_config_defaults_disabled_and_bad_config_diagnostic():
    settings = Settings()
    assert not settings.memory_index.enabled
    assert not settings.observation.enabled
    with pytest.raises(ValueError):
        Settings(memory_index={"enabled": True, "timeout": 0})


def test_enabled_bad_settings_do_not_silently_disable(tmp_path):
    import json
    from my_coding_agent.settings import load_settings
    from my_coding_agent.paths import AgentPaths
    paths = AgentPaths(home=tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text(json.dumps({"memory_index": {"enabled": True, "timeout": 0}}))
    with pytest.raises(ValueError, match="integration"):
        load_settings(paths)


@pytest.mark.anyio
async def test_disabled_integrations_do_not_import_sdks_or_start_workers(tmp_path, monkeypatch):
    import builtins
    import threading
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"mem0", "langfuse", "dream_rsi"}:
            raise AssertionError("disabled integration imported its SDK")
        return original(name, *args, **kwargs)

    def no_worker(*args, **kwargs):
        raise AssertionError("disabled integration started a thread")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(threading.Thread, "start", no_worker)
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=False, auto_load_mcp=False)
    assert await coding.aclose()


@pytest.mark.anyio
async def test_explicit_index_wins_over_factory_config(tmp_path, monkeypatch):
    import my_coding_agent.agent as module
    factory = Mock(side_effect=AssertionError("must not create another index"))
    monkeypatch.setattr(module, "create_mem0_index", factory)
    index = Index()
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=tmp_path / "m", memory_index=index,
                         memory_index_config={"enabled": True}, auto_load_mcp=False)
    assert coding.memory_store.memory_index is index
    assert await coding.aclose()
    factory.assert_not_called()


@pytest.mark.anyio
async def test_explicit_memory_disable_overrides_enabled_index_config(tmp_path, monkeypatch):
    import my_coding_agent.agent as module
    factory = Mock(side_effect=AssertionError("disabled memory initialized an SDK"))
    monkeypatch.setattr(module, "create_mem0_index", factory)
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=False, memory_index_config={"enabled": True}, auto_load_mcp=False)
    assert coding.memory_store is None
    assert await coding.aclose()
    factory.assert_not_called()


@pytest.mark.anyio
async def test_rpc_replacement_closes_owned_runtime_and_forwards_settings(tmp_path, monkeypatch):
    import io
    import my_coding_agent.agent as module
    import my_coding_agent.rpc_server as rpc
    from my_coding_agent.paths import AgentPaths

    events = []

    class Runtime:
        def attach(self, llm):
            events.append("attach")

        def close(self, timeout=5):
            events.append("close")
            return True

    monkeypatch.setattr(module, "create_observation_runtime", lambda **kw: Runtime())
    monkeypatch.setattr(rpc, "load_settings", lambda *a, **kw: Settings(observation={"enabled": True}))
    server = rpc.RpcServer(stdout=io.StringIO(), stderr=io.StringIO(), llm=LLM(),
                           paths=AgentPaths(home=tmp_path / "home"))
    result = await server.handle_request({"id": 1, "method": "initialize", "params": {"workspace": str(tmp_path)}})
    assert "error" not in result
    assert events == ["attach"]
    result = await server.handle_request({"id": 2, "method": "session_new", "params": {}})
    assert "error" not in result
    assert events == ["attach", "close", "attach"]
    result = await server.handle_request({"id": 3, "method": "shutdown", "params": {}})
    assert "error" not in result
    assert events == ["attach", "close", "attach", "close"]
    assert result["result"]["close_status"]["observation"] is True


@pytest.mark.anyio
async def test_failed_core_close_still_releases_runtime_and_mcp(tmp_path, monkeypatch):
    import my_coding_agent.agent as module
    from unittest.mock import AsyncMock
    runtime = Mock()
    runtime.close.return_value = True
    monkeypatch.setattr(module, "create_observation_runtime", lambda **kw: runtime)
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=False, observation_config={"enabled": True}, auto_load_mcp=False)
    coding.agent.aclose = AsyncMock(side_effect=RuntimeError("original close failure"))
    coding.close_mcp = AsyncMock()
    with pytest.raises(RuntimeError, match="original close failure"):
        await coding.aclose()
    runtime.close.assert_called_once()
    coding.close_mcp.assert_awaited_once()


@pytest.mark.anyio
async def test_failed_attach_rolls_back_factory_owned_memory(tmp_path, monkeypatch):
    import asyncio
    import my_coding_agent.agent as module
    index = Index()
    monkeypatch.setattr(module, "create_mem0_index", lambda *a, **kw: index)
    runtime = Mock()
    runtime.attach.side_effect = RuntimeError("attach failed")
    monkeypatch.setattr(module, "create_observation_runtime", lambda **kw: runtime)
    with pytest.raises(RuntimeError, match="attach failed"):
        CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                    memory_dir=tmp_path / "m", memory_index_config={"enabled": True},
                    observation_config={"enabled": True}, auto_load_mcp=False)
    await asyncio.sleep(0)
    assert index.closed
    runtime.close.assert_called_once()


@pytest.mark.anyio
async def test_cancelled_close_still_cleans_runtime_and_mcp(tmp_path, monkeypatch):
    import asyncio
    import my_coding_agent.agent as module
    from unittest.mock import AsyncMock
    entered = asyncio.Event()
    runtime = Mock()
    runtime.close.return_value = True
    monkeypatch.setattr(module, "create_observation_runtime", lambda **kw: runtime)
    coding = CodingAgent(workspace=tmp_path, llm=LLM(), session=Session(path=tmp_path / "s.jsonl"),
                         memory_dir=False, observation_config={"enabled": True}, auto_load_mcp=False)

    async def closing():
        entered.set()
        await asyncio.Event().wait()

    coding.agent.aclose = closing
    coding.close_mcp = AsyncMock()
    close = asyncio.create_task(coding.aclose())
    await entered.wait()
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    runtime.close.assert_called_once()
    coding.close_mcp.assert_awaited_once()
