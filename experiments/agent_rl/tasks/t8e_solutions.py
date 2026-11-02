"""t8e-001/002 的参考解法。"""

E1 = {
    "jobq/__init__.py": "",
    "jobq/job.py": '''"""作业"""


class Job:
    def __init__(self, name, fn, priority=0):
        self.name = name
        self.fn = fn
        self.priority = priority
        self.attempts = 0
''',
    "jobq/heap.py": '''"""优先堆"""

import heapq


class JobHeap:
    def __init__(self):
        self._heap = []
        self._seq = 0

    def push(self, job):
        # 优先队列：priority 降序，同优先级按插入顺序
        heapq.heappush(self._heap, (-job.priority, self._seq, job))
        self._seq += 1

    def pop(self):
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[2]

    def __len__(self):
        return len(self._heap)
''',
    "jobq/retry.py": '''"""重试策略"""

MAX_ATTEMPTS = 3


def should_retry(job, attempts):
    return attempts < MAX_ATTEMPTS
''',
    "jobq/runner.py": '''"""执行器"""

from jobq.job import Job
from jobq.retry import should_retry


class Runner:
    def __init__(self, heap):
        self.heap = heap

    def run_all(self):
        while len(self.heap):
            job = self.heap.pop()
            while True:
                job.attempts += 1
                try:
                    job.fn()
                    break
                except Exception:
                    if not should_retry(job, job.attempts):
                        break
                    # 重试时重新入堆，保持优先级
                    retry_job = Job(job.name, job.fn, job.priority)
                    retry_job.attempts = job.attempts
                    self.heap.push(retry_job)
                    break
''',
    "jobq/stats.py": '''"""统计"""


class Stats:
    def __init__(self):
        self._data = {}

    def record(self, name, ok):
        success, failure = self._data.get(name, (0, 0))
        if ok:
            success += 1
        else:
            failure += 1
        self._data[name] = (success, failure)

    def report(self):
        return dict(self._data)
''',
}

E2 = {
    "fsm/__init__.py": "",
    "fsm/transitions.py": '''"""状态转移表"""


def build_transitions():
    return {
        "idle": {"start": "running"},
        "running": {"finish": "done", "fail": "failed"},
        "done": {},
        "failed": {},
    }
''',
    "fsm/machine.py": '''"""状态机"""

_INITIAL = "idle"


class Machine:
    def __init__(self, transitions):
        self.transitions = transitions
        self._state = _INITIAL

    def fire(self, event):
        edges = self.transitions.get(self._state, {})
        if event not in edges:
            raise ValueError(f"cannot fire {event!r} from {self._state!r}")
        self._state = edges[event]

    @property
    def state(self):
        return self._state
''',
    "fsm/hooks.py": '''"""钩子"""


class Hooks:
    def __init__(self):
        self._handlers = {}

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def fire(self, event, *args):
        for fn in self._handlers.get(event, []):
            fn(*args)
''',
    "fsm/engine.py": '''"""引擎"""

from fsm.hooks import Hooks
from fsm.machine import Machine
from fsm.transitions import build_transitions


class Engine:
    def __init__(self):
        self._machine = Machine(build_transitions())
        self.hooks = Hooks()

    def start(self):
        self._machine.fire("start")
        self.hooks.fire("start")

    def finish(self):
        self._machine.fire("finish")
        self.hooks.fire("finish")

    @property
    def state(self):
        return self._machine.state
''',
    "fsm/history.py": '''"""历史"""


class History:
    def __init__(self):
        self._states = []

    def add(self, state):
        self._states.append(state)

    def path(self):
        return list(self._states)
''',
}

T8E_SOLUTIONS = {
    "t8e-001": E1,
    "t8e-002": E2,
}
