"""简单梯度任务（t9）：设计目标是「DeepSeek 能稳定做对、9B 做不对」。

背景（见 DIRECTION_2.md 第十六节）：
之前的「边界任务」（t8e/t8f/t8g，5 模块 × 20-30 行）实测下来
DeepSeek 也做不出来（0/2）。没有成功轨迹，整条 RL 链路就没有信号。

这一批任务的设计原则：
1. **2 个模块**，不是 5 个 —— 减小总工作量
2. **每个模块 8-12 行实现** —— 一次 write 就能写完
3. **依赖简单**（A → B），无跨模块协调
4. **测试 2-4 个用例** —— 判定清晰但不苛刻
5. **总步数目标 6-10 步** —— DeepSeek 能在 15 步内完成

难度梯度：t9-001~003 最容易，t9-004~006 中等，t9-007~008 稍难。
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage


def _mk(task_id: str, title: str, prompt: str, stages: list[Stage]) -> LongTask:
    return LongTask(task_id=task_id, title=title, prompt=prompt, stages=stages)


# ── t9-001: 温度转换（2 模块，最简） ─────────────────────────
_T9_001 = [
    Stage(
        "convert",
        {
            "temp/__init__.py": "",
            "temp/convert.py": (
                '"""温度转换（待实现）"""\n\n\n'
                "def celsius_to_fahrenheit(c):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_convert.py",
        '''from temp.convert import celsius_to_fahrenheit


def test_freezing():
    assert celsius_to_fahrenheit(0) == 32


def test_boiling():
    assert celsius_to_fahrenheit(100) == 212


def test_negative():
    assert celsius_to_fahrenheit(-40) == -40
''',
    ),
    Stage(
        "report",
        {
            "temp/report.py": (
                '"""温度报表（待实现，依赖 convert）"""\n\n\n'
                "def describe(c):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_report.py",
        '''from temp.report import describe


def test_hot():
    assert describe(100) == "212F (hot)"


def test_cold():
    assert describe(0) == "32F (cold)"
''',
    ),
]

_T9_001_PROMPT = """实现一个温度转换的小工具，2 个模块（按依赖顺序）：

1. `temp/convert.py` — `celsius_to_fahrenheit(c) -> float`：公式是 c * 9 / 5 + 32
2. `temp/report.py` — `describe(c) -> str`：
   - 调用 `celsius_to_fahrenheit(c)` 得到华氏温度
   - 如果摄氏温度 >= 30，返回形如 `"212F (hot)"`（华氏温度取整）
   - 否则返回形如 `"32F (cold)"`

所有测试必须通过。
"""


# ── t9-002: 字符串统计（2 模块） ─────────────────────────────
_T9_002 = [
    Stage(
        "count",
        {
            "textstat/__init__.py": "",
            "textstat/count.py": (
                '"""计数（待实现）"""\n\n\n'
                "def count_words(text):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_count.py",
        '''from textstat.count import count_words


def test_simple():
    assert count_words("hello world") == 2


def test_empty():
    assert count_words("") == 0


def test_spaces():
    assert count_words("  a   b  c ") == 3
''',
    ),
    Stage(
        "summary",
        {
            "textstat/summary.py": (
                '"""摘要（待实现，依赖 count）"""\n\n\n'
                "def summarize(text):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_summary.py",
        '''from textstat.summary import summarize


def test_short():
    assert summarize("one two") == "2 words"


def test_long():
    assert summarize("a b c d e f") == "6 words (long)"
''',
    ),
]

_T9_002_PROMPT = """实现文本统计工具，2 个模块（按依赖顺序）：

1. `textstat/count.py` — `count_words(text) -> int`：按空白切分计数
2. `textstat/summary.py` — `summarize(text) -> str`：
   - 调用 `count_words(text)`
   - 如果词数 > 5，返回 `"6 words (long)"` 这样的格式
   - 否则返回 `"2 words"`

所有测试必须通过。
"""


# ── t9-003: 简单栈（2 模块） ─────────────────────────────────
_T9_003 = [
    Stage(
        "stack",
        {
            "ds/__init__.py": "",
            "ds/stack.py": (
                '"""栈（待实现）"""\n\n\n'
                "class Stack:\n"
                "    def __init__(self):\n        raise NotImplementedError\n\n"
                "    def push(self, x):\n        raise NotImplementedError\n\n"
                "    def pop(self):\n        raise NotImplementedError\n"
            ),
        },
        "tests/test_stack.py",
        '''from ds.stack import Stack


def test_push_pop():
    s = Stack()
    s.push(1)
    s.push(2)
    assert s.pop() == 2
    assert s.pop() == 1


def test_empty():
    assert Stack().pop() is None
''',
    ),
    Stage(
        "checker",
        {
            "ds/checker.py": (
                '"""括号检查（待实现，依赖 Stack）"""\n\n\n'
                "def balanced(text):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_checker.py",
        '''from ds.checker import balanced


def test_ok():
    assert balanced("(a[b])") is True


def test_bad():
    assert balanced("(a]") is False


def test_empty():
    assert balanced("") is True
''',
    ),
]

_T9_003_PROMPT = """实现栈和括号检查，2 个模块（按依赖顺序）：

1. `ds/stack.py` — `Stack` 类：
   - `push(x)` 入栈
   - `pop()` 出栈并返回；空栈返回 None
2. `ds/checker.py` — `balanced(text) -> bool`：
   - 用 Stack 检查括号 `()[]{}` 是否配对
   - 空字符串算平衡

所有测试必须通过。
"""


# ── t9-004: 简单缓存（2 模块） ───────────────────────────────
_T9_004 = [
    Stage(
        "store",
        {
            "simplecache/__init__.py": "",
            "simplecache/store.py": (
                '"""存储（待实现）"""\n\n\n'
                "class Store:\n"
                "    def __init__(self):\n        raise NotImplementedError\n\n"
                "    def get(self, k):\n        raise NotImplementedError\n\n"
                "    def set(self, k, v):\n        raise NotImplementedError\n"
            ),
        },
        "tests/test_store.py",
        '''from simplecache.store import Store


def test_set_get():
    s = Store()
    s.set("a", 1)
    assert s.get("a") == 1


def test_missing():
    assert Store().get("x") is None
''',
    ),
    Stage(
        "memo",
        {
            "simplecache/memo.py": (
                '"""记忆化（待实现，依赖 Store）"""\n\n\n'
                "def memoize(fn):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_memo.py",
        '''from simplecache.memo import memoize


def test_caches():
    calls = []

    @memoize
    def add(a, b):
        calls.append(1)
        return a + b

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert len(calls) == 1
''',
    ),
]

_T9_004_PROMPT = """实现简单缓存，2 个模块（按依赖顺序）：

1. `simplecache/store.py` — `Store` 类：`set(k, v)` / `get(k)`（不存在返回 None）
2. `simplecache/memo.py` — `memoize(fn)` 装饰器：
   - 用 Store 缓存结果
   - 同一个 key 只用 Store 里缓存的值，不重复调用 fn

提示：key 可以用 `str(args)` 构造。

所有测试必须通过。
"""


# ── t9-005: 配置解析（3 模块，稍难） ─────────────────────────
_T9_005 = [
    Stage(
        "parse",
        {
            "tinycfg/__init__.py": "",
            "tinycfg/parse.py": (
                '"""解析（待实现）"""\n\n\n'
                "def parse(text):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_parse.py",
        '''from tinycfg.parse import parse


def test_basic():
    assert parse("a=1\\nb=2") == {"a": "1", "b": "2"}


def test_skip_comments():
    assert parse("# note\\na=1") == {"a": "1"}


def test_skip_blank():
    assert parse("a=1\\n\\nb=2") == {"a": "1", "b": "2"}
''',
    ),
    Stage(
        "convert",
        {
            "tinycfg/convert.py": (
                '"""类型转换（待实现）"""\n\n\n'
                "def to_int(mapping):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_convert.py",
        '''from tinycfg.convert import to_int


def test_ints():
    assert to_int({"a": "1", "b": "2"}) == {"a": 1, "b": 2}


def test_empty():
    assert to_int({}) == {}
''',
    ),
    Stage(
        "load",
        {
            "tinycfg/load.py": (
                '"""加载（待实现，依赖 parse + convert）"""\n\n\n'
                "def load_text(text):\n    raise NotImplementedError\n"
            ),
        },
        "tests/test_load.py",
        '''from tinycfg.load import load_text


def test_end_to_end():
    assert load_text("x=5\\ny=7") == {"x": 5, "y": 7}
''',
    ),
]

_T9_005_PROMPT = """实现一个极小配置解析器，3 个模块（按依赖顺序）：

1. `tinycfg/parse.py` — `parse(text) -> dict`：
   - 解析 `key=value` 每行一对
   - 跳过空行和 `#` 开头的注释行
   - value 保持字符串
2. `tinycfg/convert.py` — `to_int(mapping) -> dict`：把每个 value 转成 int
3. `tinycfg/load.py` — `load_text(text) -> dict`：串联 parse 和 to_int

所有测试必须通过。
"""


_SPECS = [
    ("t9-001", "温度转换（2 模块，最简）", _T9_001_PROMPT, _T9_001),
    ("t9-002", "文本统计（2 模块，最简）", _T9_002_PROMPT, _T9_002),
    ("t9-003", "栈与括号检查（2 模块）", _T9_003_PROMPT, _T9_003),
    ("t9-004", "简单缓存（2 模块）", _T9_004_PROMPT, _T9_004),
    ("t9-005", "极小配置解析器（3 模块）", _T9_005_PROMPT, _T9_005),
]


def generate() -> list:
    return [_mk(tid, title, prompt, stages) for tid, title, prompt, stages in _SPECS]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t9_simple"] = lambda: [t.to_task() for t in generate()]
