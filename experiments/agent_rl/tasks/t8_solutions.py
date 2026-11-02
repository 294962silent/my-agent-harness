"""长程任务的参考解法（用于验证任务可解）。"""

from __future__ import annotations

L1_SOLUTIONS = {
    "pipeline/parser.py": '''"""CSV 解析器"""

import csv
import io


def parse_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        if any((v or "").strip() for v in row.values()):
            rows.append(dict(row))
    return rows
''',
    "pipeline/validator.py": '''"""校验器"""


def validate(rows, schema):
    for row in rows:
        for field, caster in schema.items():
            if field not in row:
                raise ValueError(f"missing field {field}")
            try:
                caster(row[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"bad value for {field}: {row[field]}") from exc
    return rows
''',
    "pipeline/transformer.py": '''"""转换器"""


def _to_iso(value):
    value = (value or "").strip()
    if "/" in value:
        month, day, year = value.split("/")
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return value


def normalize_dates(rows, field):
    for row in rows:
        if field in row:
            row[field] = _to_iso(row[field])
    return rows
''',
    "pipeline/aggregator.py": '''"""聚合器"""


def group_sum(rows, key, value_field):
    out = {}
    for row in rows:
        k = row.get(key)
        v = float(row.get(value_field, 0) or 0)
        out[k] = out.get(k, 0.0) + v
    return out
''',
    "pipeline/exporter.py": '''"""导出器"""

import csv


def to_csv(rows, path):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
''',
    "pipeline/cli.py": '''"""CLI 入口"""

import sys


def main(argv):
    if "--help" in argv or "-h" in argv:
        print("usage: pipeline [--help] [in.csv] [out.csv]")
        return
    print("pipeline: nothing to do")


if __name__ == "__main__":
    main(sys.argv[1:])
''',
}

L2_SOLUTIONS = {
    "sched/event.py": '''"""事件"""


class Event:
    def __init__(self, name, at):
        self.name = name
        self.at = at

    def __repr__(self):
        return f"Event({self.name!r}, {self.at})"
''',
    "sched/queue.py": '''"""优先队列"""

import heapq


class EventQueue:
    def __init__(self):
        self._heap = []
        self._seq = 0

    def push(self, event):
        heapq.heappush(self._heap, (event.at, self._seq, event))
        self._seq += 1

    def pop(self):
        return heapq.heappop(self._heap)[2]

    def __len__(self):
        return len(self._heap)
''',
    "sched/scheduler.py": '''"""调度器"""

from sched.event import Event
from sched.queue import EventQueue


class Scheduler:
    def __init__(self):
        self._queue = EventQueue()

    def at(self, time, fn):
        self._queue.push(Event(fn, time))

    def run(self, until):
        while len(self._queue):
            event = self._queue.pop()
            if event.at > until:
                break
            event.name()
''',
    "sched/recurring.py": '''"""周期事件"""


def every(scheduler, interval, fn, start=0):
    if interval <= 0:
        return

    def tick(t):
        fn()
        scheduler.at(t + interval, lambda: tick(t + interval))

    scheduler.at(start, lambda: tick(start))
''',
}

L3_SOLUTIONS = {
    "editor/buffer.py": '''"""文本缓冲区"""


class Buffer:
    def __init__(self, text=""):
        self._text = text

    def insert(self, pos, s):
        self._text = self._text[:pos] + s + self._text[pos:]

    def delete(self, start, end):
        self._text = self._text[:start] + self._text[end:]

    def text(self):
        return self._text
''',
    "editor/undo.py": '''"""撤销栈"""


class UndoStack:
    def __init__(self):
        self._undo = []
        self._redo = []

    def push(self, op):
        self._undo.append(op)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return None
        op = self._undo.pop()
        self._redo.append(op)
        return op

    def redo(self):
        if not self._redo:
            return None
        op = self._redo.pop()
        self._undo.append(op)
        return op
''',
    "editor/search.py": '''"""搜索"""


def find_all(text, pattern):
    if not pattern:
        return []
    out = []
    start = 0
    while True:
        idx = text.find(pattern, start)
        if idx < 0:
            break
        out.append(idx)
        start = idx + 1
    return out
''',
    "editor/core.py": '''"""编辑器门面"""

from editor.buffer import Buffer
from editor.undo import UndoStack


class Editor:
    def __init__(self):
        self._buffer = Buffer()
        self._undo = UndoStack()

    def insert(self, pos, s):
        before = self._buffer.text()
        self._buffer.insert(pos, s)
        self._undo.push(("insert", pos, s, before))

    def undo(self):
        op = self._undo.undo()
        if op and op[0] == "insert":
            self._buffer = Buffer(op[3])

    def text(self):
        return self._buffer.text()
''',
}

SOLUTIONS = {
    "t8-001": L1_SOLUTIONS,
    "t8-002": L2_SOLUTIONS,
    "t8-003": L3_SOLUTIONS,
}
