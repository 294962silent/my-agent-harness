"""t8f-001/002 的参考解法。"""

F1 = {
    "miniorm/__init__.py": "",
    "miniorm/sql.py": '''"""SQL 片段"""


def quote(name):
    escaped = str(name).replace('"', '""')
    return f'"{escaped}"'


def table(name):
    return quote(name)
''',
    "miniorm/condition.py": '''"""条件"""

from miniorm.sql import quote


class Condition:
    def __init__(self):
        self._clauses = []
        self._params = []

    def eq(self, field, value):
        self._clauses.append(f"{quote(field)} = ?")
        self._params.append(value)
        return self

    def build(self):
        return " AND ".join(self._clauses), list(self._params)
''',
    "miniorm/query.py": '''"""查询"""

from miniorm.sql import table


class Select:
    def __init__(self, tname):
        self.tname = tname
        self._cond = None
        self._limit = None

    def where(self, cond):
        self._cond = cond
        return self

    def limit(self, n):
        self._limit = n
        return self

    def build(self):
        sql = f"SELECT * FROM {table(self.tname)}"
        params = []
        if self._cond is not None:
            clause, cond_params = self._cond.build()
            if clause:
                sql += f" WHERE {clause}"
                params.extend(cond_params)
        if self._limit is not None:
            sql += f" LIMIT {int(self._limit)}"
        return sql, params
''',
    "miniorm/insert.py": '''"""插入"""

from miniorm.sql import quote, table


class Insert:
    def __init__(self, tname):
        self.tname = tname
        self._row = None

    def values(self, row):
        self._row = dict(row)
        return self

    def build(self):
        if not self._row:
            raise ValueError("empty row")
        fields = list(self._row.keys())
        cols = ", ".join(quote(f) for f in fields)
        marks = ", ".join("?" for _ in fields)
        return f"INSERT INTO {table(self.tname)} ({cols}) VALUES ({marks})", [self._row[f] for f in fields]
''',
    "miniorm/model.py": '''"""模型门面"""

from miniorm.insert import Insert
from miniorm.query import Select


class Model:
    table_name = None

    @classmethod
    def _require_table(cls):
        if not cls.table_name:
            raise ValueError("table_name is not set")
        return cls.table_name

    @classmethod
    def select(cls):
        return Select(cls._require_table())

    @classmethod
    def insert(cls, row):
        return Insert(cls._require_table()).values(row)
''',
}

F2 = {
    "evsrc/__init__.py": "",
    "evsrc/event.py": '''"""事件"""


class Event:
    def __init__(self, kind, payload, seq=0):
        self.kind = kind
        self.payload = payload or {}
        self.seq = seq

    def __repr__(self):
        return f"Event({self.kind!r}, {self.payload!r}, seq={self.seq})"
''',
    "evsrc/log.py": '''"""事件日志"""


class EventLog:
    def __init__(self):
        self._events = []

    def append(self, event):
        self._events.append(event)

    def all(self):
        return list(self._events)

    def since(self, seq):
        return [e for e in self._events if e.seq > seq]
''',
    "evsrc/reducer.py": '''"""归约"""


def reduce_events(events, initial=None):
    state = dict(initial) if initial else {}
    for event in events or []:
        if event.kind == "inc":
            state["count"] = state.get("count", 0) + event.payload.get("by", 0)
        elif event.kind == "set":
            state[event.payload.get("key")] = event.payload.get("value")
    return state
''',
    "evsrc/snapshot.py": '''"""快照"""


class SnapshotStore:
    def __init__(self):
        self._snapshots = []

    def save(self, seq, state):
        self._snapshots.append((seq, dict(state or {})))

    def latest(self):
        if not self._snapshots:
            return (None, None)
        return max(self._snapshots, key=lambda item: item[0] if item[0] is not None else -1)
''',
    "evsrc/store.py": '''"""聚合"""

from evsrc.event import Event
from evsrc.reducer import reduce_events


class Aggregate:
    def __init__(self):
        self._events = []
        self._seq = 0

    def emit(self, kind, payload):
        self._seq += 1
        self._events.append(Event(kind, payload, seq=self._seq))

    def state(self):
        return reduce_events(self._events, {})
''',
}

T8F_SOLUTIONS = {
    "t8f-001": F1,
    "t8f-002": F2,
}
