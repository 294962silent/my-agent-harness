"""边界训练任务第二批（t8f）：按已验证的 5 条规则设计。

规则（t8e-001/002 已验证成功）：
1. 4-6 个待实现文件
2. 每文件 15-30 行实现量
3. 有依赖顺序
4. 测试严格
5. 总实现量 90-130 行
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage


def _mk(task_id: str, title: str, prompt: str, stages: list[Stage]) -> LongTask:
    return LongTask(task_id=task_id, title=title, prompt=prompt, stages=stages)


# ── F1: 简易 ORM 查询构造器（5 文件） ─────────────────────────
_F1 = [
    Stage(
        "sql",
        {"miniorm/__init__.py": "", "miniorm/sql.py": '"""SQL 片段（待实现）"""\n\n\ndef quote(name):\n    raise NotImplementedError\n\n\ndef table(name):\n    raise NotImplementedError\n'},
        "tests/test_sql.py",
        '''from miniorm.sql import quote, table


def test_quote():
    assert quote("user") == '"user"'


def test_quote_escape():
    assert quote('a"b') == '"a""b"'


def test_table():
    assert table("user") == '"user"'
''',
    ),
    Stage(
        "condition",
        {"miniorm/condition.py": '"""条件（待实现）"""\n\n\nclass Condition:\n    def __init__(self):\n        raise NotImplementedError\n\n    def eq(self, field, value):\n        raise NotImplementedError\n\n    def build(self):\n        raise NotImplementedError\n'},
        "tests/test_condition.py",
        '''from miniorm.condition import Condition


def test_eq():
    c = Condition()
    c.eq("name", "alice")
    sql, params = c.build()
    assert sql == '"name" = ?'
    assert params == ["alice"]


def test_multiple():
    c = Condition()
    c.eq("a", 1)
    c.eq("b", 2)
    sql, params = c.build()
    assert sql == '"a" = ? AND "b" = ?'
    assert params == [1, 2]


def test_empty():
    sql, params = Condition().build()
    assert sql == ""
    assert params == []
''',
    ),
    Stage(
        "query",
        {"miniorm/query.py": '"""查询（待实现）"""\n\n\nclass Select:\n    def __init__(self, tname):\n        raise NotImplementedError\n\n    def where(self, cond):\n        raise NotImplementedError\n\n    def limit(self, n):\n        raise NotImplementedError\n\n    def build(self):\n        raise NotImplementedError\n'},
        "tests/test_query.py",
        '''from miniorm.condition import Condition
from miniorm.query import Select


def test_simple():
    sql, params = Select("user").build()
    assert sql == 'SELECT * FROM "user"'
    assert params == []


def test_where_limit():
    c = Condition()
    c.eq("id", 1)
    sql, params = Select("user").where(c).limit(5).build()
    assert sql == 'SELECT * FROM "user" WHERE "id" = ? LIMIT 5'
    assert params == [1]
''',
    ),
    Stage(
        "insert",
        {"miniorm/insert.py": '"""插入（待实现）"""\n\n\nclass Insert:\n    def __init__(self, tname):\n        raise NotImplementedError\n\n    def values(self, row):\n        raise NotImplementedError\n\n    def build(self):\n        raise NotImplementedError\n'},
        "tests/test_insert.py",
        '''from miniorm.insert import Insert


def test_insert():
    sql, params = Insert("user").values({"name": "a", "age": 3}).build()
    assert sql == 'INSERT INTO "user" ("name", "age") VALUES (?, ?)'
    assert params == ["a", 3]


def test_empty_row():
    import pytest
    with pytest.raises(ValueError):
        Insert("user").values({}).build()
''',
    ),
    Stage(
        "model",
        {"miniorm/model.py": '"""模型门面（待实现）"""\n\n\nclass Model:\n    table_name = None\n\n    @classmethod\n    def select(cls):\n        raise NotImplementedError\n\n    @classmethod\n    def insert(cls, row):\n        raise NotImplementedError\n'},
        "tests/test_model.py",
        '''from miniorm.model import Model


class User(Model):
    table_name = "user"


def test_select():
    sql, params = User.select().build()
    assert sql == 'SELECT * FROM "user"'


def test_insert():
    sql, params = User.insert({"name": "a"}).build()
    assert sql == 'INSERT INTO "user" ("name") VALUES (?)'
    assert params == ["a"]


def test_no_table():
    import pytest
    with pytest.raises(ValueError):
        Model.select()
''',
    ),
]

_F1_PROMPT = """实现一个简易 ORM 查询构造器，5 个模块（按依赖顺序）：

1. `miniorm/sql.py` — `quote(name)`：加双引号并转义内部引号；`table(name)` 同 quote
2. `miniorm/condition.py` — `Condition`：`eq(field, value)` 累加条件；`build() -> (sql, params)`，用 ` AND ` 连接，占位符 `?`
3. `miniorm/query.py` — `Select(tname)`：`where(cond)` / `limit(n)` / `build()`
4. `miniorm/insert.py` — `Insert(tname)`：`values(row)` / `build()`，空 row 抛 ValueError
5. `miniorm/model.py` — `Model`：类属性 `table_name`，类方法 `select()` / `insert(row)`，无 table_name 抛 ValueError

所有测试必须通过。
"""


# ── F2: 事件溯源日志（5 文件） ────────────────────────────────
_F2 = [
    Stage(
        "event",
        {"evsrc/__init__.py": "", "evsrc/event.py": '"""事件（待实现）"""\n\n\nclass Event:\n    def __init__(self, kind, payload, seq=0):\n        raise NotImplementedError\n'},
        "tests/test_event.py",
        '''from evsrc.event import Event


def test_fields():
    e = Event("created", {"id": 1}, seq=3)
    assert e.kind == "created"
    assert e.payload == {"id": 1}
    assert e.seq == 3


def test_default_seq():
    assert Event("x", {}).seq == 0
''',
    ),
    Stage(
        "log",
        {"evsrc/log.py": '"""事件日志（待实现）"""\n\n\nclass EventLog:\n    def __init__(self):\n        raise NotImplementedError\n\n    def append(self, event):\n        raise NotImplementedError\n\n    def all(self):\n        raise NotImplementedError\n\n    def since(self, seq):\n        raise NotImplementedError\n'},
        "tests/test_log.py",
        '''from evsrc.event import Event
from evsrc.log import EventLog


def test_append_order():
    log = EventLog()
    log.append(Event("a", {}))
    log.append(Event("b", {}))
    assert [e.kind for e in log.all()] == ["a", "b"]


def test_since():
    log = EventLog()
    log.append(Event("a", {}, seq=1))
    log.append(Event("b", {}, seq=2))
    assert [e.kind for e in log.since(1)] == ["b"]


def test_empty():
    assert EventLog().all() == []
''',
    ),
    Stage(
        "reducer",
        {"evsrc/reducer.py": '"""归约（待实现）"""\n\n\ndef reduce_events(events, initial=None):\n    raise NotImplementedError\n'},
        "tests/test_reducer.py",
        '''from evsrc.event import Event
from evsrc.reducer import reduce_events


def test_counter():
    events = [Event("inc", {"by": 1}), Event("inc", {"by": 2})]
    state = reduce_events(events, {"count": 0})
    assert state["count"] == 3


def test_set():
    events = [Event("set", {"key": "x", "value": 9})]
    state = reduce_events(events, {})
    assert state["x"] == 9


def test_no_initial():
    assert reduce_events([], None) == {}
''',
    ),
    Stage(
        "snapshot",
        {"evsrc/snapshot.py": '"""快照（待实现）"""\n\n\nclass SnapshotStore:\n    def __init__(self):\n        raise NotImplementedError\n\n    def save(self, seq, state):\n        raise NotImplementedError\n\n    def latest(self):\n        raise NotImplementedError\n'},
        "tests/test_snapshot.py",
        '''from evsrc.snapshot import SnapshotStore


def test_save_latest():
    s = SnapshotStore()
    s.save(1, {"a": 1})
    s.save(5, {"a": 2})
    seq, state = s.latest()
    assert seq == 5
    assert state["a"] == 2


def test_empty():
    assert SnapshotStore().latest() == (None, None)
''',
    ),
    Stage(
        "store",
        {"evsrc/store.py": '"""聚合（待实现）"""\n\n\nclass Aggregate:\n    def __init__(self):\n        raise NotImplementedError\n\n    def emit(self, kind, payload):\n        raise NotImplementedError\n\n    def state(self):\n        raise NotImplementedError\n'},
        "tests/test_store.py",
        '''from evsrc.store import Aggregate


def test_emit_and_state():
    a = Aggregate()
    a.emit("set", {"key": "n", "value": 7})
    assert a.state()["n"] == 7


def test_multiple():
    a = Aggregate()
    a.emit("set", {"key": "x", "value": 1})
    a.emit("set", {"key": "y", "value": 2})
    s = a.state()
    assert s["x"] == 1 and s["y"] == 2
''',
    ),
]

_F2_PROMPT = """实现一个事件溯源日志系统，5 个模块（按依赖顺序）：

1. `es/event.py` — `Event(kind, payload, seq=0)`：带 kind、payload、seq 字段
2. `es/log.py` — `EventLog`：`append(event)` / `all() -> list` / `since(seq) -> list`（seq 之后的事件）
3. `es/reducer.py` — `reduce_events(events, initial=None) -> dict`：
   - `inc` 事件把 payload["by"] 累加到 state["count"]
   - `set` 事件设置 state[payload["key"]] = payload["value"]
   - initial 为 None 时从 {} 开始
4. `es/snapshot.py` — `SnapshotStore`：`save(seq, state)` / `latest() -> (seq, state)`，空时返回 (None, None)
5. `es/store.py` — `Aggregate`：`emit(kind, payload)` 内部记录事件；`state()` 返回当前状态

所有测试必须通过。
"""


_SPECS = [
    ("t8f-001", "简易 ORM 查询构造器（5 模块）", _F1_PROMPT, _F1),
    ("t8f-002", "事件溯源日志系统（5 模块）", _F2_PROMPT, _F2),
]


def generate() -> list:
    return [_mk(tid, title, prompt, stages) for tid, title, prompt, stages in _SPECS]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8f_boundary2"] = lambda: [t.to_task() for t in generate()]
