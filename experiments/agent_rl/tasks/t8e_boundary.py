"""边界难度训练任务（t8e）：按「总需求 ≈ 预算」设计，目标产生 gap。

依据（DIRECTION_1.md 第十六节）：
- t8-001（6 文件，每个中等复杂）→ k=4 下 1/4，gap=1.0
- t8-005（5 文件，含 LRU 链表）→ k=4 下 3/4，gap=1.0

设计要点（从这两个成功案例归纳）：
1. **4–6 个待实现文件**（不是 1 个也不是 12 个）
2. **每个文件 15–30 行实现量**（不是 3 行也不是 95 行）
3. **有依赖顺序**（写错顺序要返工）
4. **测试严格**（边界条件，写错就失败）
5. **总实现量 90–130 行**

这 5 条同时满足，就落在「总需求 40–55 步」的边界区间。
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"]


def _mk(task_id: str, title: str, prompt: str, stages: list[Stage]) -> LongTask:
    return LongTask(task_id=task_id, title=title, prompt=prompt, stages=stages)


# ── E1: 任务队列（5 文件） ────────────────────────────────────
_E1 = [
    Stage(
        "job",
        {"jobq/__init__.py": "", "jobq/job.py": '"""作业（待实现）"""\n\n\nclass Job:\n    def __init__(self, name, fn, priority=0):\n        raise NotImplementedError\n'},
        "tests/test_job.py",
        '''from jobq.job import Job


def test_fields():
    j = Job("a", lambda: 1, priority=5)
    assert j.name == "a"
    assert j.priority == 5


def test_default_priority():
    j = Job("b", lambda: 1)
    assert j.priority == 0
''',
    ),
    Stage(
        "heap",
        {"jobq/heap.py": '"""优先堆（待实现）"""\n\n\nclass JobHeap:\n    def __init__(self):\n        raise NotImplementedError\n\n    def push(self, job):\n        raise NotImplementedError\n\n    def pop(self):\n        raise NotImplementedError\n'},
        "tests/test_heap.py",
        '''from jobq.heap import JobHeap
from jobq.job import Job


def test_priority_order():
    h = JobHeap()
    h.push(Job("low", lambda: 1, 1))
    h.push(Job("high", lambda: 1, 9))
    assert h.pop().name == "high"


def test_empty():
    h = JobHeap()
    assert h.pop() is None


def test_same_priority_fifo():
    h = JobHeap()
    h.push(Job("first", lambda: 1, 5))
    h.push(Job("second", lambda: 1, 5))
    assert h.pop().name == "first"
''',
    ),
    Stage(
        "retry",
        {"jobq/retry.py": '"""重试策略（待实现）"""\n\n\ndef should_retry(job, attempts):\n    raise NotImplementedError\n'},
        "tests/test_retry.py",
        '''from jobq.job import Job
from jobq.retry import should_retry


def test_below_limit():
    j = Job("a", lambda: 1)
    assert should_retry(j, 1) is True


def test_at_limit():
    j = Job("a", lambda: 1)
    assert should_retry(j, 3) is False
''',
    ),
    Stage(
        "runner",
        {"jobq/runner.py": '"""执行器（待实现）"""\n\n\nclass Runner:\n    def __init__(self, heap):\n        raise NotImplementedError\n\n    def run_all(self):\n        raise NotImplementedError\n'},
        "tests/test_runner.py",
        '''from jobq.heap import JobHeap
from jobq.job import Job
from jobq.runner import Runner


def test_run_all():
    h = JobHeap()
    seen = []
    h.push(Job("a", lambda: seen.append("a"), 1))
    h.push(Job("b", lambda: seen.append("b"), 9))
    Runner(h).run_all()
    assert seen == ["b", "a"]


def test_retry_then_success():
    h = JobHeap()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("boom")

    h.push(Job("f", flaky, 1))
    Runner(h).run_all()
    assert len(calls) == 2
''',
    ),
    Stage(
        "stats",
        {"jobq/stats.py": '"""统计（待实现）"""\n\n\nclass Stats:\n    def __init__(self):\n        raise NotImplementedError\n\n    def record(self, name, ok):\n        raise NotImplementedError\n\n    def report(self):\n        raise NotImplementedError\n'},
        "tests/test_stats.py",
        '''from jobq.stats import Stats


def test_report():
    s = Stats()
    s.record("a", True)
    s.record("a", False)
    s.record("b", True)
    r = s.report()
    assert r["a"] == (1, 1)
    assert r["b"] == (1, 0)


def test_empty_report():
    assert Stats().report() == {}
''',
    ),
]

_E1_PROMPT = """实现一个任务队列系统，5 个模块（按依赖顺序）：

1. `queue/job.py` — `Job(name, fn, priority=0)`：带 name、fn、priority 字段
2. `queue/heap.py` — `JobHeap`：`push(job)` / `pop()`，按 priority 降序，同优先级先进先出
3. `queue/retry.py` — `should_retry(job, attempts) -> bool`：attempts < 3 返回 True
4. `queue/runner.py` — `Runner(heap)`：`run_all()` 按顺序执行，失败自动重试（最多 3 次）
5. `queue/stats.py` — `Stats`：`record(name, ok)` / `report() -> dict[name, (成功数, 失败数)]`


所有测试必须通过。
"""


# ── E2: 简单状态机执行器（5 文件） ───────────────────────────
_E2 = [
    Stage(
        "transitions",
        {"fsm/__init__.py": "", "fsm/transitions.py": '"""状态转移表（待实现）"""\n\n\ndef build_transitions():\n    raise NotImplementedError\n'},
        "tests/test_transitions.py",
        '''from fsm.transitions import build_transitions


def test_states():
    t = build_transitions()
    assert set(t.keys()) == {"idle", "running", "done", "failed"}


def test_edges():
    t = build_transitions()
    assert t["idle"].get("start") == "running"
    assert t["running"].get("finish") == "done"
''',
    ),
    Stage(
        "machine",
        {"fsm/machine.py": '"""状态机（待实现）"""\n\n\nclass Machine:\n    def __init__(self, transitions):\n        raise NotImplementedError\n\n    def fire(self, event):\n        raise NotImplementedError\n\n    @property\n    def state(self):\n        raise NotImplementedError\n'},
        "tests/test_machine.py",
        '''import pytest

from fsm.machine import Machine
from fsm.transitions import build_transitions


def test_initial():
    m = Machine(build_transitions())
    assert m.state == "idle"


def test_fire():
    m = Machine(build_transitions())
    m.fire("start")
    assert m.state == "running"


def test_invalid_raises():
    m = Machine(build_transitions())
    with pytest.raises(ValueError):
        m.fire("finish")
''',
    ),
    Stage(
        "hooks",
        {"fsm/hooks.py": '"""钩子（待实现）"""\n\n\nclass Hooks:\n    def __init__(self):\n        raise NotImplementedError\n\n    def on(self, event, fn):\n        raise NotImplementedError\n\n    def fire(self, event, *args):\n        raise NotImplementedError\n'},
        "tests/test_hooks.py",
        '''from fsm.hooks import Hooks


def test_on_fire():
    h = Hooks()
    seen = []
    h.on("x", lambda v: seen.append(v))
    h.fire("x", 1)
    assert seen == [1]


def test_multiple():
    h = Hooks()
    order = []
    h.on("x", lambda: order.append(1))
    h.on("x", lambda: order.append(2))
    h.fire("x")
    assert order == [1, 2]
''',
    ),
    Stage(
        "engine",
        {"fsm/engine.py": '"""引擎（待实现）"""\n\n\nclass Engine:\n    def __init__(self):\n        raise NotImplementedError\n\n    def start(self):\n        raise NotImplementedError\n\n    def finish(self):\n        raise NotImplementedError\n\n    @property\n    def state(self):\n        raise NotImplementedError\n'},
        "tests/test_engine.py",
        '''import pytest

from fsm.engine import Engine


def test_flow():
    e = Engine()
    assert e.state == "idle"
    e.start()
    assert e.state == "running"
    e.finish()
    assert e.state == "done"


def test_bad_order():
    e = Engine()
    with pytest.raises(ValueError):
        e.finish()
''',
    ),
    Stage(
        "history",
        {"fsm/history.py": '"""历史（待实现）"""\n\n\nclass History:\n    def __init__(self):\n        raise NotImplementedError\n\n    def add(self, state):\n        raise NotImplementedError\n\n    def path(self):\n        raise NotImplementedError\n'},
        "tests/test_history.py",
        '''from fsm.history import History


def test_path():
    h = History()
    h.add("idle")
    h.add("running")
    assert h.path() == ["idle", "running"]


def test_empty():
    assert History().path() == []
''',
    ),
]

_E2_PROMPT = """实现一个状态机引擎，5 个模块（按依赖顺序）：

1. `fsm/transitions.py` — `build_transitions() -> dict`：状态转移表
   状态：idle / running / done / failed
   边：idle→running（start），running→done（finish），running→failed（fail）
2. `fsm/machine.py` — `Machine(transitions)`：`fire(event)` 触发转移，非法转移抛 ValueError，初始状态 idle
3. `fsm/hooks.py` — `Hooks`：`on(event, fn)` 注册，`fire(event, *args)` 按注册顺序调用
4. `fsm/engine.py` — `Engine`：组合 Machine，提供 `start()` / `finish()`，状态 Property
5. `fsm/history.py` — `History`：`add(state)` / `path() -> list`

所有测试必须通过。
"""


_SPECS = [
    ("t8e-001", "任务队列系统（5 模块）", _E1_PROMPT, _E1),
    ("t8e-002", "状态机引擎（5 模块）", _E2_PROMPT, _E2),
]


def generate() -> list:
    return [_mk(tid, title, prompt, stages) for tid, title, prompt, stages in _SPECS]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8e_boundary"] = lambda: [t.to_task() for t in generate()]
