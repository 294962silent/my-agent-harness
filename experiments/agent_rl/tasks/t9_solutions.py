"""t9 简单任务的参考解法。"""

T9_001 = {
    "temp/convert.py": '''"""温度转换"""


def celsius_to_fahrenheit(c):
    return c * 9 / 5 + 32
''',
    "temp/report.py": '''"""温度报表"""

from temp.convert import celsius_to_fahrenheit


def describe(c):
    f = int(round(celsius_to_fahrenheit(c)))
    if c >= 30:
        return f"{f}F (hot)"
    return f"{f}F (cold)"
''',
}

T9_002 = {
    "textstat/count.py": '''"""计数"""


def count_words(text):
    return len(text.split())
''',
    "textstat/summary.py": '''"""摘要"""

from textstat.count import count_words


def summarize(text):
    n = count_words(text)
    if n > 5:
        return f"{n} words (long)"
    return f"{n} words"
''',
}

T9_003 = {
    "ds/stack.py": '''"""栈"""


class Stack:
    def __init__(self):
        self._items = []

    def push(self, x):
        self._items.append(x)

    def pop(self):
        if not self._items:
            return None
        return self._items.pop()
''',
    "ds/checker.py": '''"""括号检查"""

from ds.stack import Stack

_PAIRS = {")": "(", "]": "[", "}": "{"}


def balanced(text):
    stack = Stack()
    for ch in text:
        if ch in "([{":
            stack.push(ch)
        elif ch in _PAIRS:
            top = stack.pop()
            if top != _PAIRS[ch]:
                return False
    return stack.pop() is None
''',
}

T9_004 = {
    "simplecache/store.py": '''"""存储"""


class Store:
    def __init__(self):
        self._d = {}

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v):
        self._d[k] = v
''',
    "simplecache/memo.py": '''"""记忆化"""

import functools

from simplecache.store import Store


def memoize(fn):
    store = Store()

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = str(args) + str(sorted(kwargs.items()))
        cached = store.get(key)
        if cached is not None:
            return cached
        value = fn(*args, **kwargs)
        store.set(key, value)
        return value

    return wrapper
''',
}

T9_005 = {
    "tinycfg/parse.py": '''"""解析"""


def parse(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
''',
    "tinycfg/convert.py": '''"""类型转换"""


def to_int(mapping):
    return {k: int(v) for k, v in mapping.items()}
''',
    "tinycfg/load.py": '''"""加载"""

from tinycfg.convert import to_int
from tinycfg.parse import parse


def load_text(text):
    return to_int(parse(text))
''',
}

T9_SOLUTIONS = {
    "t9-001": T9_001,
    "t9-002": T9_002,
    "t9-003": T9_003,
    "t9-004": T9_004,
    "t9-005": T9_005,
}
