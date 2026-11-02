"""边界训练任务第三批（t8g）：一次 4 个，加速扩充。

沿用已验证的 5 条规则：
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


# ── G1: 工作流引擎（5 文件） ──────────────────────────────────
_G1 = [
    Stage(
        "node",
        {"wf/__init__.py": "", "wf/node.py": '"""节点（待实现）"""\n\n\nclass Node:\n    def __init__(self, name, fn):\n        raise NotImplementedError\n'},
        "tests/test_node.py",
        '''from wf.node import Node


def test_fields():
    n = Node("a", lambda: 1)
    assert n.name == "a"
    assert n.fn() == 1
''',
    ),
    Stage(
        "graph",
        {"wf/graph.py": '"""图（待实现）"""\n\n\nclass Graph:\n    def __init__(self):\n        raise NotImplementedError\n\n    def add(self, node):\n        raise NotImplementedError\n\n    def edge(self, src, dst):\n        raise NotImplementedError\n\n    def deps(self, name):\n        raise NotImplementedError\n'},
        "tests/test_graph.py",
        '''import pytest

from wf.graph import Graph
from wf.node import Node


def test_add_and_deps():
    g = Graph()
    g.add(Node("a", lambda: 1))
    g.add(Node("b", lambda: 2))
    g.edge("a", "b")
    assert g.deps("b") == ["a"]
    assert g.deps("a") == []


def test_duplicate():
    g = Graph()
    g.add(Node("a", lambda: 1))
    with pytest.raises(ValueError):
        g.add(Node("a", lambda: 2))


def test_missing_edge():
    g = Graph()
    g.add(Node("a", lambda: 1))
    with pytest.raises(KeyError):
        g.edge("a", "zz")
''',
    ),
    Stage(
        "topo",
        {"wf/topo.py": '"""拓扑排序（待实现）"""\n\n\ndef order(graph):\n    raise NotImplementedError\n'},
        "tests/test_topo.py",
        '''import pytest

from wf.graph import Graph
from wf.node import Node
from wf.topo import order


def test_linear():
    g = Graph()
    for n in "abc":
        g.add(Node(n, lambda: 1))
    g.edge("a", "b")
    g.edge("b", "c")
    assert order(g) == ["a", "b", "c"]


def test_cycle():
    g = Graph()
    for n in "ab":
        g.add(Node(n, lambda: 1))
    g.edge("a", "b")
    g.edge("b", "a")
    with pytest.raises(ValueError):
        order(g)
''',
    ),
    Stage(
        "executor",
        {"wf/executor.py": '"""执行器（待实现）"""\n\n\nclass Executor:\n    def __init__(self, graph):\n        raise NotImplementedError\n\n    def run(self):\n        raise NotImplementedError\n'},
        "tests/test_executor.py",
        '''from wf.executor import Executor
from wf.graph import Graph
from wf.node import Node


def test_run_order():
    g = Graph()
    seen = []
    g.add(Node("a", lambda: seen.append("a")))
    g.add(Node("b", lambda: seen.append("b")))
    g.edge("a", "b")
    Executor(g).run()
    assert seen == ["a", "b"]


def test_failure_stops():
    g = Graph()
    seen = []

    def boom():
        raise RuntimeError("x")

    g.add(Node("a", boom))
    g.add(Node("b", lambda: seen.append("b")))
    g.edge("a", "b")
    try:
        Executor(g).run()
    except RuntimeError:
        pass
    assert seen == []
''',
    ),
    Stage(
        "result",
        {"wf/result.py": '"""结果（待实现）"""\n\n\nclass Result:\n    def __init__(self):\n        raise NotImplementedError\n\n    def ok(self, name):\n        raise NotImplementedError\n\n    def fail(self, name, error):\n        raise NotImplementedError\n\n    def summary(self):\n        raise NotImplementedError\n'},
        "tests/test_result.py",
        '''from wf.result import Result


def test_summary():
    r = Result()
    r.ok("a")
    r.fail("b", "boom")
    s = r.summary()
    assert s["succeeded"] == ["a"]
    assert s["failed"] == ["b"]


def test_empty():
    s = Result().summary()
    assert s["succeeded"] == [] and s["failed"] == []
''',
    ),
]

_G1_PROMPT = """实现一个工作流引擎，5 个模块（按依赖顺序）：

1. `wf/node.py` — `Node(name, fn)`：带 name 和可调用 fn
2. `wf/graph.py` — `Graph`：`add(node)`（重名抛 ValueError）/ `edge(src, dst)`（不存在抛 KeyError）/ `deps(name) -> list`
3. `wf/topo.py` — `order(graph) -> list[str]`：拓扑排序，有环抛 ValueError
4. `wf/executor.py` — `Executor(graph)`：`run()` 按拓扑顺序执行，失败立即中止
5. `wf/result.py` — `Result`：`ok(name)` / `fail(name, error)` / `summary() -> {"succeeded": [...], "failed": [...]}`

所有测试必须通过。
"""


# ── G2: 限流器（5 文件） ──────────────────────────────────────
_G2 = [
    Stage(
        "clock",
        {"limiter/__init__.py": "", "limiter/clock.py": '"""时钟（待实现）"""\n\n\nclass Clock:\n    def __init__(self, now=0.0):\n        raise NotImplementedError\n\n    def time(self):\n        raise NotImplementedError\n\n    def advance(self, dt):\n        raise NotImplementedError\n'},
        "tests/test_clock.py",
        '''from limiter.clock import Clock


def test_time():
    c = Clock(10.0)
    assert c.time() == 10.0
    c.advance(5)
    assert c.time() == 15.0
''',
    ),
    Stage(
        "token_bucket",
        {"limiter/bucket.py": '"""令牌桶（待实现）"""\n\n\nclass TokenBucket:\n    def __init__(self, rate, capacity):\n        raise NotImplementedError\n\n    def allow(self, now):\n        raise NotImplementedError\n'},
        "tests/test_bucket.py",
        '''from limiter.bucket import TokenBucket


def test_capacity():
    b = TokenBucket(rate=1.0, capacity=2)
    assert b.allow(0) is True
    assert b.allow(0) is True
    assert b.allow(0) is False


def test_refill():
    b = TokenBucket(rate=1.0, capacity=2)
    b.allow(0)
    b.allow(0)
    assert b.allow(1.0) is True


def test_partial_refill():
    b = TokenBucket(rate=2.0, capacity=1)
    b.allow(0)
    assert b.allow(0.4) is False
    assert b.allow(0.6) is True
''',
    ),
    Stage(
        "sliding",
        {"limiter/sliding.py": '"""滑动窗口（待实现）"""\n\n\nclass SlidingWindow:\n    def __init__(self, limit, window):\n        raise NotImplementedError\n\n    def allow(self, now):\n        raise NotImplementedError\n'},
        "tests/test_sliding.py",
        '''from limiter.sliding import SlidingWindow


def test_limit():
    s = SlidingWindow(limit=2, window=1.0)
    assert s.allow(0.0) is True
    assert s.allow(0.5) is True
    assert s.allow(0.9) is False


def test_window_slides():
    s = SlidingWindow(limit=2, window=1.0)
    s.allow(0.0)
    s.allow(0.5)
    assert s.allow(1.1) is True


def test_empty():
    s = SlidingWindow(limit=1, window=1.0)
    assert s.allow(5.0) is True
''',
    ),
    Stage(
        "composite",
        {"limiter/composite.py": '"""组合限流（待实现）"""\n\n\nclass Composite:\n    def __init__(self, limiters):\n        raise NotImplementedError\n\n    def allow(self, now):\n        raise NotImplementedError\n'},
        "tests/test_composite.py",
        '''from limiter.bucket import TokenBucket
from limiter.composite import Composite


def test_all_must_pass():
    b1 = TokenBucket(rate=1.0, capacity=1)
    b2 = TokenBucket(rate=1.0, capacity=1)
    c = Composite([b1, b2])
    assert c.allow(0) is True
    assert c.allow(0) is False


def test_empty_allows():
    assert Composite([]).allow(0) is True
''',
    ),
    Stage(
        "metrics",
        {"limiter/metrics.py": '"""指标（待实现）"""\n\n\nclass Metrics:\n    def __init__(self):\n        raise NotImplementedError\n\n    def record(self, allowed):\n        raise NotImplementedError\n\n    def ratio(self):\n        raise NotImplementedError\n'},
        "tests/test_metrics.py",
        '''from limiter.metrics import Metrics


def test_ratio():
    m = Metrics()
    m.record(True)
    m.record(False)
    m.record(True)
    assert m.ratio() == 2 / 3


def test_empty():
    assert Metrics().ratio() == 0.0
''',
    ),
]

_G2_PROMPT = """实现一个限流器，5 个模块（按依赖顺序）：

1. `limiter/clock.py` — `Clock(now=0.0)`：`time()` / `advance(dt)`
2. `limiter/bucket.py` — `TokenBucket(rate, capacity)`：`allow(now) -> bool`，按 rate 补充令牌，最多 capacity
3. `limiter/sliding.py` — `SlidingWindow(limit, window)`：`allow(now)`，窗口内最多 limit 次
4. `limiter/composite.py` — `Composite(limiters)`：`allow(now)`，全部通过才通过
5. `limiter/metrics.py` — `Metrics`：`record(allowed)` / `ratio() -> float`（通过率，空时为 0.0）

所有测试必须通过。
"""


# ── G3: 配置校验器（5 文件） ──────────────────────────────────
_G3 = [
    Stage(
        "errors",
        {"cfgv/__init__.py": "", "cfgv/errors.py": '"""错误（待实现）"""\n\n\nclass ValidationError(Exception):\n    def __init__(self, path, message):\n        raise NotImplementedError\n'},
        "tests/test_errors.py",
        '''from cfgv.errors import ValidationError


def test_message():
    e = ValidationError("a.b", "bad")
    assert "a.b" in str(e)
    assert "bad" in str(e)


def test_path_attr():
    e = ValidationError("a.b", "bad")
    assert e.path == "a.b"
''',
    ),
    Stage(
        "types",
        {"cfgv/types.py": '"""类型校验（待实现）"""\n\n\ndef check_type(value, expected, path=""):\n    raise NotImplementedError\n'},
        "tests/test_types.py",
        '''import pytest

from cfgv.errors import ValidationError
from cfgv.types import check_type


def test_ok():
    assert check_type("x", str) == "x"
    assert check_type(3, int) == 3


def test_bool_not_int():
    assert check_type(True, bool) is True
    with pytest.raises(ValidationError):
        check_type(True, int)


def test_bad():
    with pytest.raises(ValidationError):
        check_type("x", int, "a.b")
''',
    ),
    Stage(
        "schema",
        {"cfgv/schema.py": '"""模式（待实现）"""\n\n\nclass Schema:\n    def __init__(self):\n        raise NotImplementedError\n\n    def field(self, name, typ, required=True):\n        raise NotImplementedError\n\n    def validate(self, data):\n        raise NotImplementedError\n'},
        "tests/test_schema.py",
        '''import pytest

from cfgv.errors import ValidationError
from cfgv.schema import Schema


def test_ok():
    s = Schema()
    s.field("name", str)
    assert s.validate({"name": "a"}) == {"name": "a"}


def test_required_missing():
    s = Schema()
    s.field("name", str)
    with pytest.raises(ValidationError):
        s.validate({})


def test_optional_missing():
    s = Schema()
    s.field("name", str, required=False)
    assert s.validate({}) == {}


def test_extra_dropped():
    s = Schema()
    s.field("a", int)
    assert s.validate({"a": 1, "b": 2}) == {"a": 1}
''',
    ),
    Stage(
        "loader",
        {"cfgv/loader.py": '"""加载（待实现）"""\n\n\ndef load_json(text):\n    raise NotImplementedError\n'},
        "tests/test_loader.py",
        '''import pytest

from cfgv.errors import ValidationError
from cfgv.loader import load_json


def test_ok():
    assert load_json('{"a": 1}') == {"a": 1}


def test_bad_json():
    with pytest.raises(ValidationError):
        load_json("{")


def test_not_object():
    with pytest.raises(ValidationError):
        load_json("[1,2]")
''',
    ),
    Stage(
        "api",
        {"cfgv/api.py": '"""门面（待实现）"""\n\n\ndef validate_config(text, schema):\n    raise NotImplementedError\n'},
        "tests/test_api.py",
        '''import pytest

from cfgv.api import validate_config
from cfgv.errors import ValidationError
from cfgv.schema import Schema


def test_end_to_end():
    s = Schema()
    s.field("port", int)
    assert validate_config('{"port": 80}', s) == {"port": 80}


def test_failure():
    s = Schema()
    s.field("port", int)
    with pytest.raises(ValidationError):
        validate_config('{"port": "x"}', s)
''',
    ),
]

_G3_PROMPT = """实现一个配置校验器，5 个模块（按依赖顺序）：

1. `cfgv/errors.py` — `ValidationError(path, message)`：异常，带 path 属性，str 含 path 和 message
2. `cfgv/types.py` — `check_type(value, expected, path="") -> value`：类型不符抛 ValidationError
   **注意：bool 不算 int**（True 传给 int 要报错）
3. `cfgv/schema.py` — `Schema`：`field(name, typ, required=True)` / `validate(data) -> dict`
   required 缺失抛错；多余字段丢弃
4. `cfgv/loader.py` — `load_json(text) -> dict`：JSON 非法或不是对象时抛 ValidationError
5. `cfgv/api.py` — `validate_config(text, schema) -> dict`：串联 load + validate

所有测试必须通过。
"""


# ── G4: 简单模板引擎（5 文件） ────────────────────────────────
_G4 = [
    Stage(
        "lexer",
        {"tmpl/__init__.py": "", "tmpl/lexer.py": '"""词法（待实现）"""\n\n\ndef tokenize(text):\n    raise NotImplementedError\n'},
        "tests/test_lexer.py",
        '''from tmpl.lexer import tokenize


def test_plain():
    assert tokenize("abc") == [("text", "abc")]


def test_var():
    assert tokenize("a{{x}}b") == [("text", "a"), ("var", "x"), ("text", "b")]


def test_empty_var():
    assert tokenize("{{ x }}") == [("var", "x")]
''',
    ),
    Stage(
        "resolve",
        {"tmpl/resolve.py": '"""变量解析（待实现）"""\n\n\ndef resolve(path, ctx):\n    raise NotImplementedError\n'},
        "tests/test_resolve.py",
        '''import pytest

from tmpl.resolve import resolve


def test_simple():
    assert resolve("a", {"a": 1}) == 1


def test_nested():
    assert resolve("a.b", {"a": {"b": 2}}) == 2


def test_missing():
    with pytest.raises(KeyError):
        resolve("zz", {})
''',
    ),
    Stage(
        "filters",
        {"tmpl/filters.py": '"""过滤器（待实现）"""\n\n\ndef apply_filter(name, value, *args):\n    raise NotImplementedError\n'},
        "tests/test_filters.py",
        '''import pytest

from tmpl.filters import apply_filter


def test_upper():
    assert apply_filter("upper", "ab") == "AB"


def test_default():
    assert apply_filter("default", None, "x") == "x"
    assert apply_filter("default", "y", "x") == "y"


def test_unknown():
    with pytest.raises(ValueError):
        apply_filter("nope", 1)
''',
    ),
    Stage(
        "render",
        {"tmpl/render.py": '"""渲染（待实现）"""\n\n\ndef render(text, ctx):\n    raise NotImplementedError\n'},
        "tests/test_render.py",
        '''from tmpl.render import render


def test_simple():
    assert render("hi {{name}}", {"name": "a"}) == "hi a"


def test_nested():
    assert render("{{a.b}}", {"a": {"b": 1}}) == "1"


def test_filter():
    assert render("{{name|upper}}", {"name": "ab"}) == "AB"


def test_no_vars():
    assert render("plain", {}) == "plain"
''',
    ),
    Stage(
        "engine",
        {"tmpl/engine.py": '"""引擎门面（待实现）"""\n\n\nclass Engine:\n    def __init__(self):\n        raise NotImplementedError\n\n    def render(self, text, ctx):\n        raise NotImplementedError\n\n    def cache_size(self):\n        raise NotImplementedError\n'},
        "tests/test_engine.py",
        '''from tmpl.engine import Engine


def test_render():
    e = Engine()
    assert e.render("{{x}}", {"x": 1}) == "1"


def test_cache():
    e = Engine()
    e.render("{{x}}", {"x": 1})
    e.render("{{x}}", {"x": 2})
    assert e.cache_size() == 1
''',
    ),
]

_G4_PROMPT = """实现一个简单模板引擎，5 个模块（按依赖顺序）：

1. `tmpl/lexer.py` — `tokenize(text) -> list[(kind, value)]`：
   普通文本是 ("text", ...)，`{{ name }}` 是 ("var", "name")，变量名去空格
2. `tmpl/resolve.py` — `resolve(path, ctx)`：支持 `a.b` 嵌套，缺失抛 KeyError
3. `tmpl/filters.py` — `apply_filter(name, value, *args)`：支持 upper、default，未知抛 ValueError
4. `tmpl/render.py` — `render(text, ctx) -> str`：支持 `{{path}}` 和 `{{path|filter}}`
5. `tmpl/engine.py` — `Engine`：`render(text, ctx)` + `cache_size()`（按模板文本缓存，重复文本只缓存一次）

所有测试必须通过。
"""


_SPECS = [
    ("t8g-001", "工作流引擎（5 模块）", _G1_PROMPT, _G1),
    ("t8g-002", "限流器（5 模块）", _G2_PROMPT, _G2),
    ("t8g-003", "配置校验器（5 模块）", _G3_PROMPT, _G3),
    ("t8g-004", "模板引擎（5 模块）", _G4_PROMPT, _G4),
]


def generate() -> list:
    return [_mk(tid, title, prompt, stages) for tid, title, prompt, stages in _SPECS]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8g_boundary3"] = lambda: [t.to_task() for t in generate()]
