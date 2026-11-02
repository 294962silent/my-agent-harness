"""t8g-001..004 的参考解法。"""

G1 = {
    "wf/__init__.py": "",
    "wf/node.py": '''"""节点"""


class Node:
    def __init__(self, name, fn):
        self.name = name
        self.fn = fn
''',
    "wf/graph.py": '''"""图"""


class Graph:
    def __init__(self):
        self._nodes = {}
        self._deps = {}

    def add(self, node):
        if node.name in self._nodes:
            raise ValueError(f"duplicate node {node.name!r}")
        self._nodes[node.name] = node
        self._deps.setdefault(node.name, [])

    def edge(self, src, dst):
        if src not in self._nodes or dst not in self._nodes:
            raise KeyError(f"unknown node in edge {src!r}->{dst!r}")
        self._deps[dst].append(src)

    def deps(self, name):
        return list(self._deps.get(name, []))

    def nodes(self):
        return dict(self._nodes)
''',
    "wf/topo.py": '''"""拓扑排序"""


def order(graph):
    nodes = graph.nodes()
    visited = {}
    result = []

    def visit(name, stack):
        state = visited.get(name)
        if state == "done":
            return
        if state == "visiting":
            raise ValueError(f"cycle detected at {name!r}")
        visited[name] = "visiting"
        for dep in graph.deps(name):
            visit(dep, stack + [name])
        visited[name] = "done"
        result.append(name)

    for name in nodes:
        visit(name, [])
    return result
''',
    "wf/executor.py": '''"""执行器"""

from wf.topo import order


class Executor:
    def __init__(self, graph):
        self.graph = graph

    def run(self):
        nodes = self.graph.nodes()
        for name in order(self.graph):
            nodes[name].fn()
''',
    "wf/result.py": '''"""结果"""


class Result:
    def __init__(self):
        self._ok = []
        self._fail = []

    def ok(self, name):
        self._ok.append(name)

    def fail(self, name, error):
        self._fail.append(name)

    def summary(self):
        return {"succeeded": list(self._ok), "failed": list(self._fail)}
''',
}

G2 = {
    "limiter/__init__.py": "",
    "limiter/clock.py": '''"""时钟"""


class Clock:
    def __init__(self, now=0.0):
        self._now = float(now)

    def time(self):
        return self._now

    def advance(self, dt):
        self._now += float(dt)
        return self._now
''',
    "limiter/bucket.py": '''"""令牌桶"""


class TokenBucket:
    def __init__(self, rate, capacity):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = None

    def allow(self, now):
        now = float(now)
        if self._last is None:
            self._last = now
        else:
            elapsed = max(0.0, now - self._last)
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False
''',
    "limiter/sliding.py": '''"""滑动窗口"""


class SlidingWindow:
    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self._hits = []

    def allow(self, now):
        now = float(now)
        self._hits = [t for t in self._hits if now - t < self.window]
        if len(self._hits) < self.limit:
            self._hits.append(now)
            return True
        return False
''',
    "limiter/composite.py": '''"""组合限流"""


class Composite:
    def __init__(self, limiters):
        self.limiters = list(limiters or [])

    def allow(self, now):
        if not self.limiters:
            return True
        # 全部必须通过；先全部试一遍再决定（避免部分消耗）
        return all(limiter.allow(now) for limiter in self.limiters)
''',
    "limiter/metrics.py": '''"""指标"""


class Metrics:
    def __init__(self):
        self._allowed = 0
        self._total = 0

    def record(self, allowed):
        self._total += 1
        if allowed:
            self._allowed += 1

    def ratio(self):
        if self._total == 0:
            return 0.0
        return self._allowed / self._total
''',
}

G3 = {
    "cfgv/__init__.py": "",
    "cfgv/errors.py": '''"""错误"""


class ValidationError(Exception):
    def __init__(self, path, message):
        self.path = str(path) if path else ""
        self.message = str(message)
        prefix = f"{self.path}: " if self.path else ""
        super().__init__(prefix + self.message)
''',
    "cfgv/types.py": '''"""类型校验"""

from cfgv.errors import ValidationError


def check_type(value, expected, path=""):
    # bool 是 int 的子类，需要特殊处理
    if expected is int and isinstance(value, bool):
        raise ValidationError(path, f"expected int, got bool ({value!r})")
    if expected is bool and not isinstance(value, bool):
        raise ValidationError(path, f"expected bool, got {type(value).__name__}")
    if not isinstance(value, expected):
        raise ValidationError(path, f"expected {expected.__name__}, got {type(value).__name__}")
    return value
''',
    "cfgv/schema.py": '''"""模式"""

from cfgv.errors import ValidationError
from cfgv.types import check_type


class Schema:
    def __init__(self):
        self._fields = {}

    def field(self, name, typ, required=True):
        self._fields[name] = (typ, required)
        return self

    def validate(self, data):
        out = {}
        for name, (typ, required) in self._fields.items():
            if name not in data:
                if required:
                    raise ValidationError(name, "required field missing")
                continue
            out[name] = check_type(data[name], typ, name)
        return out
''',
    "cfgv/loader.py": '''"""加载"""

import json

from cfgv.errors import ValidationError


def load_json(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError("", f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("", f"expected object, got {type(data).__name__}")
    return data
''',
    "cfgv/api.py": '''"""门面"""

from cfgv.loader import load_json


def validate_config(text, schema):
    data = load_json(text)
    return schema.validate(data)
''',
}

G4 = {
    "tmpl/__init__.py": "",
    "tmpl/lexer.py": '''"""词法"""

import re

_PATTERN = re.compile(r"\\{\\{(.*?)\\}\\}")


def tokenize(text):
    tokens = []
    pos = 0
    for match in _PATTERN.finditer(text or ""):
        if match.start() > pos:
            tokens.append(("text", text[pos : match.start()]))
        tokens.append(("var", match.group(1).strip()))
        pos = match.end()
    if pos < len(text or ""):
        tokens.append(("text", text[pos:]))
    return tokens
''',
    "tmpl/resolve.py": '''"""变量解析"""


def resolve(path, ctx):
    current = ctx
    for part in str(path).split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        else:
            raise KeyError(path)
    return current
''',
    "tmpl/filters.py": '''"""过滤器"""


def apply_filter(name, value, *args):
    if name == "upper":
        return str(value).upper()
    if name == "default":
        if value is None and args:
            return args[0]
        return value
    raise ValueError(f"unknown filter {name!r}")
''',
    "tmpl/render.py": '''"""渲染"""

from tmpl.filters import apply_filter
from tmpl.lexer import tokenize
from tmpl.resolve import resolve


def render(text, ctx):
    parts = []
    for kind, value in tokenize(text):
        if kind == "text":
            parts.append(value)
            continue
        expr = value
        filt = None
        if "|" in expr:
            expr, filt = expr.split("|", 1)
            expr = expr.strip()
            filt = filt.strip()
        result = resolve(expr, ctx)
        if filt:
            result = apply_filter(filt, result)
        parts.append("" if result is None else str(result))
    return "".join(parts)
''',
    "tmpl/engine.py": '''"""引擎门面"""

from tmpl.render import render as _render


class Engine:
    def __init__(self):
        self._cache = {}

    def render(self, text, ctx):
        if text not in self._cache:
            self._cache[text] = True
        return _render(text, ctx)

    def cache_size(self):
        return len(self._cache)
''',
}

T8G_SOLUTIONS = {
    "t8g-001": G1,
    "t8g-002": G2,
    "t8g-003": G3,
    "t8g-004": G4,
}
