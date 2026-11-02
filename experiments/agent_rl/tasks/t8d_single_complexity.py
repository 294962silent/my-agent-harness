"""单模块复杂度对照：控制模块数（1 个）和依赖链（无），只改变函数内部难度。

假设：难度来自单个函数内部的实现复杂度。

设计：都是**单模块、单函数**，但实现复杂度分三档：

| 任务 | 函数 | 复杂度 |
| --- | --- | --- |
| t8-009 | `add(a, b)` | 极简（1 行） |
| t8-010 | `parse_iso8601(text)` | 中等（字符串解析、边界） |
| t8-011 | `evaluate(expr)` | 高（递归下降表达式求值） |

这样能把「复杂度」这个变量单独隔离出来。
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage

# ── 极简 ──────────────────────────────────────────────────────
_EASY = [
    Stage(
        name="add",
        files={"mod/__init__.py": "", "mod/fn.py": '"""加法（待实现）"""\n\n\ndef add(a, b):\n    raise NotImplementedError\n'},
        test_file="tests/test_fn.py",
        test_content=(
            "from mod.fn import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        ),
    )
]

# ── 中等：ISO8601 时长解析 ─────────────────────────────────────
_MEDIUM = [
    Stage(
        name="duration",
        files={
            "mod/__init__.py": "",
            "mod/fn.py": '"""解析 ISO8601 时长（待实现）"""\n\n\ndef parse_duration(text):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_fn.py",
        test_content='''from mod.fn import parse_duration


def test_basic():
    assert parse_duration("PT1H30M") == 5400


def test_days():
    assert parse_duration("P1DT2H") == 93600


def test_seconds_only():
    assert parse_duration("PT45S") == 45


def test_weeks():
    assert parse_duration("P2W") == 1209600


def test_combined():
    assert parse_duration("P1Y2M3DT4H5M6S") == 36993906


def test_all_units():
    assert parse_duration("P1W2D") == 777600


def test_zero():
    assert parse_duration("PT0S") == 0


def test_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_duration("hello")
''',
    )
]

# ── 高：递归下降表达式求值 ────────────────────────────────────
_HARD = [
    Stage(
        name="evaluator",
        files={
            "mod/__init__.py": "",
            "mod/fn.py": '"""表达式求值（待实现，需支持递归下降解析）"""\n\n\ndef evaluate(expr):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_fn.py",
        test_content='''import pytest

from mod.fn import evaluate


def test_simple():
    assert evaluate("1+2") == 3


def test_precedence():
    assert evaluate("2+3*4") == 14


def test_parens():
    assert evaluate("(2+3)*4") == 20


def test_nested_parens():
    assert evaluate("((1+2)*(3+4))") == 21


def test_unary_minus():
    assert evaluate("-5+2") == -3


def test_division():
    assert evaluate("10/4") == 2.5


def test_power():
    assert evaluate("2**10") == 1024


def test_complex():
    assert evaluate("1+2*(3+4)/(5-3)") == 8


def test_whitespace():
    assert evaluate(" 1 + 2 " ) == 3


def test_modulo():
    assert evaluate("7%3") == 1


def test_invalid_raises():
    with pytest.raises(ValueError):
        evaluate("1++")


def test_div_zero_raises():
    with pytest.raises(ZeroDivisionError):
        evaluate("1/0")
''',
    )
]

_SPECS = [
    ("009", "极简单函数（加法）", "实现 `mod/fn.py` 的 `add(a, b)`：两数相加。所有测试必须通过。", _EASY),
    ("010", "中等：ISO8601 时长解析", _MEDIUM_PROMPT := """实现 `mod/fn.py` 的 `parse_duration(text) -> int`：解析 ISO8601 时长字符串为秒数。

支持：P[nY][nM][nD][T[nH][nM][nS]] 和 P[nW]
- Y=31536000 秒，M=2592000 秒，W=604800 秒，D=86400 秒，H=3600 秒，M=60 秒，S=1 秒
- 无效输入抛 ValueError

所有测试必须通过。""", _MEDIUM),
    ("011", "高：递归下降表达式求值", """实现 `mod/fn.py` 的 `evaluate(expr) -> float|int`：解析并计算算术表达式。

支持：
- 四则运算 + - * / 和取模 %
- 幂运算 **
- 括号嵌套
- 一元负号
- 运算符优先级

无效表达式抛 ValueError，除零抛 ZeroDivisionError。

所有测试必须通过。""", _HARD),
]


def generate() -> list:
    return [
        LongTask(task_id=f"t8-{tid}", title=title, prompt=prompt, stages=stages)
        for tid, title, prompt, stages in _SPECS
    ]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8d_complexity"] = lambda: [t.to_task() for t in generate()]
