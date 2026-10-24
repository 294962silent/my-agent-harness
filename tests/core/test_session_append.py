# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Session 增量追加落盘测试：零重写、指针记录、load 等价性、并发与事件循环不阻塞。

覆盖 format_version=2：
- 仅新增消息 → 只向文件尾部追加，历史前缀逐字节不变；
- rewind/compaction 等结构性变更 → 原子全量重写，必要时在末尾写一条指针记录；
- Session.load 恢复出的 tree/current_id/compaction_floor 与「全量重写版本」一致；
- 旧格式文件仍按首行 metadata 恢复，首次写入后升级为 v2。
"""

from __future__ import annotations

import json
import threading

import pytest

from my_agent_core.agent import Agent
from my_agent_core.session import Session
from tests.core.conftest import FakeLLM, make_response, multiply


def _structural_snapshot(session: Session) -> dict:
    """树结构 + 指针 + floor 的完整快照（跨 id 比较用）。"""
    return {
        "entries": [
            (
                e.id,
                e.parent_id,
                getattr(e, "type", None),
                getattr(e, "content", None),
                getattr(e, "summary", None),
            )
            for e in session.tree.entries.values()
        ],
        "current_id": session.tree.current_id,
        "root_id": session.tree.root_id,
        "compaction_floor": session.compaction_floor,
    }


def _full_rewrite_oracle(session: Session, path) -> Session:
    """把当前内存状态强制全量重写一份（新会话无基线）→ 作为 load 等价性基准。"""
    clone = Session(path=path, cwd=session.cwd, metadata=dict(session.metadata))
    clone.id = session.id
    clone.created_at = session.created_at
    clone.tree = session.tree
    clone.compaction_floor = session.compaction_floor
    clone.save()
    return clone


def test_incremental_append_keeps_prefix_and_line_count(tmp_path):
    """新增消息只追加：历史前缀不变，行数逐个增加。"""
    session = Session(path=tmp_path / "s.jsonl")
    session.add_message("user", "q1")
    after_first = session.path.read_text(encoding="utf-8").splitlines()

    session.add_message("assistant", "a1")
    session.add_message("user", "q2")
    lines = session.path.read_text(encoding="utf-8").splitlines()

    assert lines[: len(after_first)] == after_first  # 前缀逐字节不变（未重写）
    assert len(lines) == len(after_first) + 2
    loaded = Session.load(session.path)
    assert [e.content for e in loaded.tree.get_current_path()] == ["q1", "a1", "q2"]


def test_incremental_append_never_calls_full_rewrite(tmp_path, monkeypatch):
    """纯消息追加路径绝不触发 _rewrite_full（O(1) 追加，非 O(n) 重写）。"""

    def fail(_self):
        raise AssertionError("unexpected full rewrite on append-only path")

    session = Session(path=tmp_path / "s.jsonl")
    session.add_message("user", "q1")
    monkeypatch.setattr(Session, "_rewrite_full", fail)
    session.add_message("assistant", "a1")
    session.add_message("user", "q2")
    loaded = Session.load(session.path)
    assert [e.content for e in loaded.tree.get_current_path()] == ["q1", "a1", "q2"]


def test_pointer_record_written_when_current_is_not_tail(tmp_path):
    """rewind 后全量重写：末尾追加一条指针记录，明确 current 指针。"""
    session = Session(path=tmp_path / "s.jsonl")
    q1 = session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    session.add_message("user", "q2")

    session.rewind(q1.id)

    lines = session.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["metadata"]["format_version"] == 2
    pointer_record = json.loads(lines[-1])
    assert pointer_record["type"] == "custom"
    assert pointer_record["namespace"] == "my_agent_core.session"
    assert pointer_record["data"]["current_id"] == q1.id
    assert pointer_record["data"]["root_id"] == q1.id

    loaded = Session.load(session.path)
    assert loaded.tree.current_id == q1.id
    assert len(loaded.tree.entries) == 3


def test_rewind_then_append_advances_pointer(tmp_path):
    """rewind（全量重写 + 指针记录）后再追加消息：只追加，load 指针推进到新消息。"""
    session = Session(path=tmp_path / "s.jsonl")
    q1 = session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    session.add_message("user", "q2")
    session.rewind(q1.id)

    session.add_message("assistant", "a2")  # 增量追加（不重写）
    loaded = Session.load(session.path)
    assert loaded.tree.current_id == session.tree.current_id
    assert [e.content for e in loaded.tree.get_current_path()] == ["q1", "a2"]
    # 旧分支仍保留
    assert "a1" in [getattr(e, "content", None) for e in loaded.tree.entries.values()]


def test_pointer_record_supports_none_current(tmp_path):
    """current 回到根之前（None）也能持久化：指针记录显式携带 null，load 还原为空指针。"""
    session = Session(path=tmp_path / "s.jsonl")
    session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    root_id = session.tree.root_id

    session.tree.current_id = None
    session.save()

    lines = session.path.read_text(encoding="utf-8").splitlines()
    pointer_record = json.loads(lines[-1])
    assert pointer_record["data"]["current_id"] is None
    loaded = Session.load(session.path)
    assert loaded.tree.current_id is None
    assert loaded.tree.root_id == root_id

    # 从根重新长枝：后续增量追加仍能推进指针
    session.add_message("user", "q2")
    loaded2 = Session.load(session.path)
    assert loaded2.tree.current_id == session.tree.current_id
    assert [e.content for e in loaded2.tree.get_current_path()] == ["q2"]


def test_incremental_load_matches_full_rewrite(tmp_path):
    """追加若干消息（含压缩/回退）后 load → tree/current/floor 与全量重写版本一致。"""
    session = Session(path=tmp_path / "inc.jsonl")
    q1 = session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    session.add_message("user", "q2")
    session.rewind(q1.id)  # 结构性：全量重写 + 指针记录
    session.add_message("assistant", "a2")  # 增量
    session.add_message("user", "q3")  # 增量
    session.add_summary_cache(  # 结构性：floor 变更 → 全量重写 + 指针记录
        "## summary",
        covered_count=2,
        retained_tail=[{"role": "user", "content": "q3"}],
        tokens_before=100,
    )
    session.add_message("assistant", "a3")  # 增量（floor 未变）

    loaded_inc = Session.load(session.path)
    full = _full_rewrite_oracle(session, tmp_path / "full.jsonl")
    loaded_full = Session.load(full.path)

    assert _structural_snapshot(loaded_inc) == _structural_snapshot(loaded_full)
    assert loaded_inc.compaction_floor == session.compaction_floor
    assert loaded_inc.tree.current_id == session.tree.current_id
    assert [e.content for e in loaded_inc.tree.get_current_path()] == ["q1", "a2", "q3", "a3"]


def test_compaction_append_keeps_pointer(tmp_path):
    """压缩 entry（不动 current）追加后，指针仍停在压缩时的活动叶。"""
    session = Session(path=tmp_path / "s.jsonl")
    session.add_message("user", "q1")
    current_before = session.tree.current_id
    session.add_summary_cache(
        "## summary",
        covered_count=1,
        retained_tail=[{"role": "user", "content": "q1"}],
        tokens_before=10,
    )
    session.add_summary_cache(  # 第二次压缩：floor 不变，走追加 + 指针记录
        "## summary2",
        covered_count=1,
        retained_tail=[{"role": "user", "content": "q1"}],
        tokens_before=20,
    )
    loaded = Session.load(session.path)
    assert loaded.tree.current_id == current_before
    assert loaded.compaction_floor == current_before


def test_external_file_change_forces_full_rewrite(tmp_path):
    """文件被外部改写（尺寸变化）→ 放弃增量，全量重写保证内存即事实。"""
    session = Session(path=tmp_path / "s.jsonl")
    session.add_message("user", "q1")
    with open(session.path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "external",
                    "parent_id": "nope",
                    "timestamp": 1.0,
                    "message": {"role": "user", "content": "external"},
                }
            )
            + "\n"
        )
    session.add_message("assistant", "a1")
    loaded = Session.load(session.path)
    contents = [getattr(e, "content", None) for e in loaded.tree.entries.values()]
    assert "external" not in contents
    assert [e.content for e in loaded.tree.get_current_path()] == ["q1", "a1"]


def test_legacy_file_pointer_semantics_then_upgrade(tmp_path):
    """旧格式（无 format_version）仍按首行 metadata 恢复指针；首次写入升级为 v2。"""
    path = tmp_path / "legacy.jsonl"
    header = {
        "type": "session_info",
        "id": "legacy-id",
        "cwd": ".",
        "createdAt": 1700000000.0,
        "metadata": {"current_id": "e1", "root_id": "e1"},
    }
    lines = [
        json.dumps(header),
        json.dumps(
            {
                "type": "message",
                "id": "e1",
                "parent_id": None,
                "timestamp": 1700000001.0,
                "message": {"role": "user", "content": "q1"},
            }
        ),
        json.dumps(
            {
                "type": "message",
                "id": "e2",
                "parent_id": "e1",
                "timestamp": 1700000002.0,
                "message": {"role": "assistant", "content": "a1"},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = Session.load(path)
    assert loaded.tree.current_id == "e1"  # 旧格式：首行 metadata 权威（rewind 态）
    assert [e.content for e in loaded.tree.entries.values()] == ["q1", "a1"]

    loaded.add_message("user", "q2")  # 旧文件不允许增量 → 全量重写升级
    text = path.read_text(encoding="utf-8")
    assert '"format_version":2' in text
    upgraded = Session.load(path)
    assert upgraded.tree.current_id == loaded.tree.current_id
    assert [e.content for e in upgraded.tree.get_current_path()] == ["q1", "q2"]


def test_concurrent_saves_are_serialized(tmp_path):
    """多线程同时 save：文件行完整可解析，load 不丢 entry。"""
    session = Session(path=tmp_path / "s.jsonl")
    for i in range(20):
        session.tree.add_entry("user", f"m{i}")

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            session.save()
        except BaseException as exc:  # noqa: BLE001 - 测试收集线程异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    loaded = Session.load(session.path)
    assert len(loaded.tree.entries) == 20
    assert [e.content for e in loaded.tree.entries.values()] == [f"m{i}" for i in range(20)]


@pytest.mark.anyio
async def test_agent_persists_off_event_loop_thread(tmp_path):
    """Agent 的 MessageEnd 落盘跑在工作线程（不阻塞事件循环）。"""
    session = Session(path=tmp_path / "s.jsonl")
    original_save = session.save
    loop_thread = threading.get_ident()
    save_threads: list[int] = []

    def recording_save() -> None:
        save_threads.append(threading.get_ident())
        original_save()

    session.save = recording_save  # type: ignore[method-assign]
    agent = Agent(
        llm=FakeLLM([make_response(content="hi")]),
        tools=[multiply],
        session=session,
        skill_dirs=[],
    )

    await agent.run("q1")

    assert save_threads, "expected at least one save"
    assert all(tid != loop_thread for tid in save_threads)
    loaded = Session.load(session.path)
    assert [e.content for e in loaded.tree.get_current_path()] == ["q1", "hi"]
