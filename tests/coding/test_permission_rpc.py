"""工具审批 JSON-RPC 往返测试（permission_request → 客户端响应 → Future resolve）。"""

from __future__ import annotations

import asyncio
import io
import json
import queue
from pathlib import Path
from typing import Any

import pytest
from my_agent_core.hooks import ToolCallHook

from my_coding_agent.permissions import PermissionRequest
from my_coding_agent.rpc_server import RpcServer


class FakeLLM:
    """初始化 / 权限往返不需要真实模型，只提供最小属性。"""

    model = "fake-model"


class QueueStdin:
    """内存 stdin：readline 阻塞等待测试线程/协程 push 数据，模拟真实管道。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()

    def readline(self) -> str:
        return self._queue.get()

    def push(self, payload: dict[str, Any]) -> None:
        self._queue.put(json.dumps(payload, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self._queue.put("")


def _messages(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _permission_requests(out: io.StringIO) -> list[dict[str, Any]]:
    return [msg for msg in _messages(out) if msg.get("method") == "permission_request"]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("等待条件超时")


async def _initialize(server: RpcServer, workspace: Path, mode: str = "review", **extra: Any) -> dict[str, Any]:
    response = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"workspace": str(workspace), "mode": mode, **extra},
        }
    )
    assert response["result"]["status"] == "ok"
    return response


@pytest.mark.anyio
async def test_permission_request_round_trip_and_session_scope(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    gate = server.agent.permission_gate  # type: ignore[union-attr]
    assert gate is not None
    assert gate.confirm_callback is not None
    assert gate.workspace == tmp_path.resolve()

    request = PermissionRequest(
        action="write",
        target="a.txt",
        details={"path": "a.txt", "content": "hi"},
        preview="hi",
        tool_name="write",
        tool_call_id="call_1",
        risk="medium",
    )
    pending = asyncio.create_task(server._request_permission(request))
    await _wait_until(lambda: bool(_permission_requests(out)))

    assert pending.done() is False
    message = _permission_requests(out)[0]
    params = message["params"]
    assert message["jsonrpc"] == "2.0"
    assert message["id"] == "perm-1"
    assert params["request_id"] == "perm-1"
    assert params["tool_call_id"] == "call_1"
    assert params["tool_name"] == "write"
    assert params["action"] == "write"
    assert params["target"] == "a.txt"
    assert params["preview"] == "hi"
    assert params["risk"] == "medium"
    assert params["details"] == {"path": "a.txt", "content": "hi"}
    assert params["timeout_ms"] == 300_000

    ack = await server.handle_request(
        {"jsonrpc": "2.0", "id": "perm-1", "result": {"approved": True, "scope": "session"}}
    )
    assert ack["result"] == {"status": "ok"}
    assert await pending is True
    assert gate.is_session_approved("write")
    assert server._permission_requests == {}

    # scope=session 后同名工具直接放行，不再产生新的 permission_request
    before = len(_permission_requests(out))
    result = await gate(ToolCallHook(tool_call_id="call_2", tool_name="write", args={"path": "b.txt", "content": "y"}))
    assert result.block is False
    assert len(_permission_requests(out)) == before


@pytest.mark.anyio
async def test_gate_prompts_over_rpc_and_blocks_on_denial(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    gate = server.agent.permission_gate  # type: ignore[union-attr]
    assert gate is not None
    hook = ToolCallHook(tool_call_id="call_bash", tool_name="bash", args={"command": "rm -rf build"})
    pending = asyncio.create_task(gate(hook))
    await _wait_until(lambda: bool(_permission_requests(out)))

    params = _permission_requests(out)[0]["params"]
    assert params["risk"] == "high"
    assert "高危命令" in (params["reason"] or "")
    assert params["target"] == "rm -rf build"

    await server.handle_request(
        {"jsonrpc": "2.0", "id": params["request_id"], "result": {"approved": False, "reason": "用户拒绝"}}
    )
    result = await pending
    assert result.block is True
    assert result.reason == "用户拒绝"


@pytest.mark.anyio
async def test_gate_timeout_blocks_with_timeout_reason(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM(), permission_timeout_ms=60)
    await _initialize(server, tmp_path, mode="strict")

    gate = server.agent.permission_gate  # type: ignore[union-attr]
    assert gate is not None
    # strict 模式下只读工具也要审批；客户端不应答 → 超时拒绝
    result = await gate(ToolCallHook(tool_call_id="call_read", tool_name="read", args={"path": "a.txt"}))

    assert result.block is True
    assert result.reason == "审批超时，已自动拒绝"
    assert server._permission_requests == {}
    message = _permission_requests(out)[0]
    assert message["params"]["timeout_ms"] == 60


@pytest.mark.anyio
async def test_initialize_accepts_permission_timeout_param(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path, mode="strict", permission_timeout_ms=40)

    assert server.permission_timeout_ms == 40
    gate = server.agent.permission_gate  # type: ignore[union-attr]
    assert gate is not None
    result = await gate(ToolCallHook(tool_call_id="c", tool_name="read", args={"path": "a.txt"}))
    assert result.block is True
    assert result.reason == "审批超时，已自动拒绝"
    assert _permission_requests(out)[0]["params"]["timeout_ms"] == 40


@pytest.mark.anyio
async def test_malformed_permission_response_is_rejected(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    request = PermissionRequest(action="bash", target="ls", tool_name="bash")
    pending = asyncio.create_task(server._request_permission(request))
    await _wait_until(lambda: bool(_permission_requests(out)))

    await server.handle_request({"jsonrpc": "2.0", "id": "perm-1", "result": "definitely-not-approved"})
    assert await pending is False
    assert request.deny_reason == "审批响应格式无效，已自动拒绝"


@pytest.mark.anyio
async def test_permission_error_response_is_rejected(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    request = PermissionRequest(action="bash", target="ls", tool_name="bash")
    pending = asyncio.create_task(server._request_permission(request))
    await _wait_until(lambda: bool(_permission_requests(out)))

    await server.handle_request(
        {"jsonrpc": "2.0", "id": "perm-1", "error": {"code": -32000, "message": "client exploded"}}
    )
    assert await pending is False
    assert "client exploded" in (request.deny_reason or "")


@pytest.mark.anyio
async def test_concurrent_permission_requests_resolve_independently(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    first = asyncio.create_task(
        server._request_permission(PermissionRequest(action="bash", target="first", tool_name="bash"))
    )
    await _wait_until(lambda: len(_permission_requests(out)) == 1)
    second = asyncio.create_task(
        server._request_permission(PermissionRequest(action="bash", target="second", tool_name="bash"))
    )
    await _wait_until(lambda: len(_permission_requests(out)) == 2)

    ids = [message["id"] for message in _permission_requests(out)]
    assert ids == ["perm-1", "perm-2"]

    # 乱序应答：第二个先批，第一个后拒
    await server.handle_request({"jsonrpc": "2.0", "id": "perm-2", "result": {"approved": True}})
    assert await second is True
    await server.handle_request(
        {"jsonrpc": "2.0", "id": "perm-1", "result": {"approved": False, "reason": "no"}}
    )
    assert await first is False
    assert server._permission_requests == {}


@pytest.mark.anyio
async def test_run_forever_resolves_permission_response_without_rpc_error(tmp_path: Path):
    stdin = QueueStdin()
    out = io.StringIO()
    server = RpcServer(stdin=stdin, stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path)

    loop_task = asyncio.create_task(server.run_forever())
    pending: asyncio.Task[bool] | None = None
    try:
        pending = asyncio.create_task(
            server._request_permission(PermissionRequest(action="bash", target="ls -la", tool_name="bash"))
        )
        await _wait_until(lambda: bool(_permission_requests(out)))
        permission_id = _permission_requests(out)[0]["id"]

        stdin.push(
            {"jsonrpc": "2.0", "id": permission_id, "result": {"approved": True, "scope": "once"}}
        )
        assert await asyncio.wait_for(pending, 2) is True

        # 未知 id 的响应与普通通知都不应触发 -32601 / 崩溃
        stdin.push({"jsonrpc": "2.0", "id": "perm-999", "result": {"approved": True}})
        stdin.push({"jsonrpc": "2.0", "id": 99, "method": "shutdown", "params": {}})
        stdin.close()
        await asyncio.wait_for(loop_task, 2)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        if not loop_task.done():
            loop_task.cancel()

    assert server.is_shutting_down is True
    messages = _messages(out)
    assert not any(message.get("error", {}).get("code") == -32601 for message in messages)
    # 服务端绝不回复客户端发来的审批响应
    assert not any(
        message.get("id") == "perm-999" and ("result" in message or "error" in message) for message in messages
    )
    assert any(message.get("id") == 99 for message in messages)


@pytest.mark.anyio
async def test_client_disconnect_rejects_pending_permission(tmp_path: Path):
    stdin = QueueStdin()
    out = io.StringIO()
    server = RpcServer(stdin=stdin, stdout=out, llm=FakeLLM(), permission_timeout_ms=5000)
    await _initialize(server, tmp_path)

    loop_task = asyncio.create_task(server.run_forever())
    request = PermissionRequest(action="bash", target="ls", tool_name="bash")
    pending = asyncio.create_task(server._request_permission(request))
    try:
        await _wait_until(lambda: bool(_permission_requests(out)))
        stdin.close()  # 管道 EOF = 客户端断开
        assert await asyncio.wait_for(pending, 2) is False
        await asyncio.wait_for(loop_task, 2)
    finally:
        if not pending.done():
            pending.cancel()
        if not loop_task.done():
            loop_task.cancel()

    assert request.deny_reason == "客户端已断开，已自动拒绝"


@pytest.mark.anyio
async def test_yolo_mode_never_requests_permission(tmp_path: Path):
    out = io.StringIO()
    server = RpcServer(stdout=out, llm=FakeLLM())
    await _initialize(server, tmp_path, mode="yolo")

    gate = server.agent.permission_gate  # type: ignore[union-attr]
    assert gate is not None
    assert gate.mode == "autonomous"

    result = await gate(ToolCallHook(tool_call_id="c", tool_name="write", args={"path": "a.txt", "content": "x"}))
    assert result.block is False
    assert _permission_requests(out) == []
