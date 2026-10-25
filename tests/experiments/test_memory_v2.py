"""记忆系统 v2 的核心机制测试：时序有效性（supersede）与检索排序。

这些测试不依赖模型、不花 token，验证的是「新值覆盖旧值」这条不变量：
- 同 (subject, key) 的新事实必须把旧事实的 valid_to 置为新事实的 valid_from；
- 检索默认只返回仍然有效的事实；
- 被取代的事实保留在库里（可审计），但不会注入上下文。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.longmemeval.memory_v2 import Consolidator, Fact, MemoryV2


def _fact(value: str, valid_from: str, key: str = "wifi_password") -> Fact:
    return Fact(subject="user", key=key, value=value, valid_from=valid_from, importance=0.8)


def test_new_value_supersedes_old_one() -> None:
    c = Consolidator()
    old = _fact("1234", "2023-01-01")
    new = _fact("9876", "2023-06-01")
    c.add(old)
    c.add(new)

    assert old.valid_to == "2023-06-01", "旧事实的失效时间应等于新事实的生效时间"
    assert new.valid_to is None
    assert [f.value for f in c.active()] == ["9876"]


def test_same_value_is_not_treated_as_conflict() -> None:
    c = Consolidator()
    a = _fact("1234", "2023-01-01")
    b = _fact("1234", "2023-06-01")
    c.add(a)
    c.add(b)

    assert a.valid_to is None, "值相同不算冲突，不应互相取代"
    assert len(c.active()) == 2


def test_different_keys_do_not_interfere() -> None:
    c = Consolidator()
    c.add(_fact("1234", "2023-01-01", key="wifi_password"))
    c.add(_fact("9000", "2023-02-01", key="port"))
    c.add(_fact("9876", "2023-06-01", key="wifi_password"))

    active = {f.key: f.value for f in c.active()}
    assert active == {"wifi_password": "9876", "port": "9000"}


def test_superseded_fact_is_kept_for_audit_but_not_retrieved() -> None:
    c = Consolidator()
    c.add(_fact("1234", "2023-01-01"))
    c.add(_fact("9876", "2023-06-01"))

    assert len(c.facts) == 2, "旧事实应保留（可审计）"
    rendered = [f.render() for f in c.active()]
    assert any("9876" in line for line in rendered)
    assert not any("1234" in line for line in rendered), "被取代的事实不应进入上下文"

    # 渲染全部（含失效）时能看到 superseded 标记
    all_rendered = "\n".join(f.render() for f in c.facts)
    assert "superseded" in all_rendered


def test_retrieval_prefers_latest_value() -> None:
    memory = MemoryV2()
    record = {
        "question_date": "2023/07/01 (Sat) 10:00",
        "haystack_dates": ["2023/01/01 (Sun) 10:00"],
        "haystack_sessions": [[{"role": "user", "content": "my wifi password is 1234"}]],
        "haystack_session_ids": ["s1"],
    }
    memory.ingest(record)
    memory.consolidator.facts.clear()
    memory.consolidator.add(_fact("1234", "2023-01-01"))
    memory.consolidator.add(_fact("9876", "2023-06-01"))

    retrieved = memory.retrieve("what is my wifi password?", k_facts=5, k_chunks=0)
    context = memory.render_context(retrieved)
    assert "9876" in context
    assert "1234" not in context


def test_timeline_lists_sessions_in_order() -> None:
    memory = MemoryV2()
    record = {
        "question_date": "2023/07/01 (Sat) 10:00",
        "haystack_dates": ["2023/05/01 (Mon) 10:00", "2023/06/01 (Thu) 10:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "bought a bike"}],
            [{"role": "user", "content": "bought a helmet"}],
        ],
        "haystack_session_ids": ["a", "b"],
    }
    memory.ingest(record)
    timeline = memory.render_timeline()
    assert timeline.index("2023/05/01") < timeline.index("2023/06/01")
    assert "bike" in timeline and "helmet" in timeline
