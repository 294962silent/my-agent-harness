"""工具超时语义测试（缺陷 2）。

写盘类工具标记 uncancellable 后，timeout 退化为「只告警不中断」：绝不能出现
「工具报超时失败、但文件已经被改完」的转录本/磁盘不一致；普通工具保持原有超时终止行为。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from my_agent_core.tools import tool


@pytest.mark.anyio
async def test_uncancellable_sync_tool_succeeds_and_writes_despite_timeout(tmp_path: Path) -> None:
    """慢的 mutating 同步工具（sleep 0.3s，timeout=0.05s）→ 最终成功，且文件确实写入。"""
    target = tmp_path / "written.txt"

    @tool(timeout=0.05, uncancellable=True)
    def slow_write(path: str, content: str) -> str:
        time.sleep(0.3)
        Path(path).write_text(content, encoding="utf-8")
        return "written"

    result = await slow_write.execute({"path": str(target), "content": "persisted"})

    assert result.ok is True
    assert result.error is None
    assert result.data == "written"
    assert target.read_text(encoding="utf-8") == "persisted"


@pytest.mark.anyio
async def test_ordinary_sync_tool_still_times_out() -> None:
    """普通（可取消）同步工具保持既有超时语义：超时返回错误，不再等待。"""

    @tool(timeout=0.05)
    def slow_read(delay: float) -> str:
        time.sleep(delay)
        return "late"

    started = time.monotonic()
    result = await slow_read.execute({"delay": 0.3})
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "timed out after 0.05s" in str(result.error)
    assert elapsed < 0.3  # 确实是超时提前返回，而不是等线程跑完


@pytest.mark.anyio
async def test_uncancellable_timeout_warns_but_is_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """mutating 工具不会因超时被标成 error：结果成功、落入告警日志。"""
    target = tmp_path / "data.txt"

    @tool(timeout=0.02, uncancellable=True)
    def slow_append(path: str) -> str:
        time.sleep(0.15)
        Path(path).write_text("done", encoding="utf-8")
        return "ok"

    with caplog.at_level(logging.WARNING, logger="my_agent_core.tools.core"):
        result = await slow_append.execute({"path": str(target)})

    assert result.ok is True
    assert result.error is None
    assert "timed out" not in str(result.data).lower()
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("不可取消" in message for message in warnings)
    assert target.read_text(encoding="utf-8") == "done"


@pytest.mark.anyio
async def test_uncancellable_async_tool_also_ignores_timeout() -> None:
    """uncancellable 同样适用于 async 工具（write/edit 本身即 async）。"""

    @tool(timeout=0.01, uncancellable=True)
    async def slow_async() -> str:
        await asyncio.sleep(0.1)
        return "done"

    result = await slow_async.execute({})

    assert result.ok is True
    assert result.data == "done"


def test_write_and_edit_tools_are_marked_uncancellable(tmp_path: Path) -> None:
    """write / edit 工厂产物必须携带 uncancellable 能力标记。"""
    from my_coding_agent.tools import make_edit_tool, make_write_tool

    assert make_write_tool(tmp_path).uncancellable is True
    assert make_edit_tool(tmp_path).uncancellable is True


@pytest.mark.anyio
async def test_write_tool_with_tiny_timeout_still_writes(tmp_path: Path) -> None:
    """write 工具即使配置了极小 timeout，也等写盘真正完成并返回成功。"""
    from my_coding_agent.tools import make_write_tool

    write_tool = make_write_tool(tmp_path)
    write_tool.timeout = 0.000001
    result = await write_tool.execute({"path": "tiny.txt", "content": "hello"})

    assert result.ok is True
    assert (tmp_path / "tiny.txt").read_text(encoding="utf-8") == "hello"
