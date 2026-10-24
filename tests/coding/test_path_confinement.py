"""路径边界约束测试：绝对路径 / .. 穿越 / symlink 指向外部 / tool_call_id 注入。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from my_agent_llm import Message

from my_agent_core.context import budget_tool_results
from my_coding_agent.tools.base import PathEscapeError, resolve_path
from my_coding_agent.tools.edit import make_edit_tool
from my_coding_agent.tools.find import make_find_tool
from my_coding_agent.tools.grep import make_grep_tool
from my_coding_agent.tools.ls import make_ls_tool
from my_coding_agent.tools.read import make_read_tool
from my_coding_agent.tools.write import make_write_tool

pytestmark = pytest.mark.anyio


def _workspace_and_outside(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("top secret\n", encoding="utf-8")
    return workspace, outside


# ── resolve_path 边界 ──


def test_resolve_path_absolute_escape_raises(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)
    with pytest.raises(PathEscapeError) as exc_info:
        resolve_path(workspace, str(outside))
    assert str(workspace) in str(exc_info.value)
    assert str(outside) in str(exc_info.value)
    assert "路径在工作区之外" in str(exc_info.value)


def test_resolve_path_absolute_inside_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    target = workspace / "sub" / "a.txt"
    assert resolve_path(workspace, str(target)) == target.resolve()


def test_resolve_path_parent_traversal_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_path(workspace, "../outside.txt")


def test_resolve_path_parent_traversal_back_inside_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    target = resolve_path(workspace, "sub/../inside.txt")
    assert target == workspace / "inside.txt"


async def test_resolve_path_symlink_escape_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    link = workspace / "link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
        file_link = workspace / "leak.txt"
        file_link.symlink_to(outside_file)
    except (OSError, NotImplementedError) as exc:  # Windows 未授予 symlink 权限
        pytest.skip(f"当前环境无法创建 symlink: {exc}")

    with pytest.raises(PathEscapeError):
        resolve_path(workspace, "link/secret.txt")

    # 直接传入 symlink 文件同样被拒绝
    with pytest.raises(PathEscapeError):
        resolve_path(workspace, "leak.txt")

    # grep 遍历工作区时也不能跟随 symlink 读取外部内容
    grep_tool = make_grep_tool(workspace)
    res = await grep_tool.execute(pattern="secret")
    assert "leak.txt:" not in str(res)


# ── 写类工具：越界必须硬失败 ──


async def test_write_outside_workspace_hard_fails(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)
    tool = make_write_tool(workspace)

    res = await tool.execute(path=str(outside), content="pwned")

    assert res.ok is False
    assert "路径在工作区之外" in str(res)
    assert outside.read_text(encoding="utf-8") == "top secret\n"


async def test_write_parent_traversal_hard_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = make_write_tool(workspace)

    res = await tool.execute(path="../escape.txt", content="pwned")

    assert res.ok is False
    assert "路径在工作区之外" in str(res)
    assert not (tmp_path / "escape.txt").exists()


async def test_edit_outside_workspace_hard_fails(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)
    tool = make_edit_tool(workspace)

    res = await tool.execute(
        path="../outside.txt",
        edits=[{"oldText": "top secret", "newText": "pwned"}],
    )

    assert res.ok is False
    assert "路径在工作区之外" in str(res)
    assert outside.read_text(encoding="utf-8") == "top secret\n"


# ── 只读工具：默认拒绝，显式开关可放宽 ──


async def test_read_only_tools_reject_outside_by_default(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)

    cases = [
        (make_read_tool(workspace), {"path": str(outside)}),
        (make_grep_tool(workspace), {"pattern": "top", "path": str(outside)}),
        (make_find_tool(workspace), {"pattern": "*.txt", "path": str(tmp_path)}),
        (make_ls_tool(workspace), {"path": str(tmp_path)}),
    ]
    for tool, args in cases:
        res = await tool.execute(**args)
        assert res.ok is False, f"{tool.name} 应拒绝越界访问"
        assert "路径在工作区之外" in str(res), f"{tool.name} 错误信息不清晰"


async def test_read_allow_outside_reads_switch(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)
    tool = make_read_tool(workspace, allow_outside_reads=True)

    res = await tool.execute(path=str(outside))

    assert res.ok is True
    assert "top secret" in str(res)


async def test_grep_allow_outside_reads_switch(tmp_path: Path) -> None:
    workspace, outside = _workspace_and_outside(tmp_path)
    tool = make_grep_tool(workspace, allow_outside_reads=True)

    res = await tool.execute(pattern="top secret", path=str(outside))

    assert res.ok is True
    assert "top secret" in str(res)


# ── 工具输出落盘文件名清洗 ──


@pytest.mark.parametrize("tool_call_id", ["../../evil", "..\\..\\evil", "bad id/with:chars"])
def test_tool_call_id_traversal_is_hashed(tmp_path: Path, tool_call_id: str) -> None:
    results_dir = tmp_path / "results"
    messages = [
        Message(
            role="tool",
            content="z" * 100,
            metadata={"tool_call_id": tool_call_id},
        )
    ]

    view = budget_tool_results(messages, max_chars=10, results_dir=results_dir)

    expected_name = hashlib.sha1(tool_call_id.encode("utf-8", errors="replace")).hexdigest()[:12] + ".txt"
    assert (results_dir / expected_name).is_file()
    assert "<persisted-output>" in view[0].content
    assert expected_name in view[0].content
    # 原始注入若直接拼接 results_dir / f"{tid}.txt"，会落到 results_dir 之外
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (results_dir / "evil.txt").exists()


def test_safe_tool_call_id_keeps_filename(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    messages = [Message(role="tool", content="z" * 100, metadata={"tool_call_id": "call_1.abc-2"})]

    budget_tool_results(messages, max_chars=10, results_dir=results_dir)

    assert (results_dir / "call_1.abc-2.txt").is_file()
