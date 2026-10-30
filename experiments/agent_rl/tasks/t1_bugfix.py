"""T1：单文件 / 少文件 bug 修复。

生成思路：每个变体是一段**有明确 bug 的代码 + 对应的 pytest**。
前 10 个（easy）单文件、bug 显眼；后 10 个（hard）多文件、bug 需要跨函数推理。

判定：`python -m pytest -q` 退出码为 0。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

# (变体名, bug 描述, 错误代码, 正确代码, 测试代码)
_CASES: list[tuple[str, str, str, str, str]] = [
    # ── easy：单文件，单个语法/算子错误 ──
    (
        "off_by_one",
        "range 上界少 1，返回的列表少了最后一个元素",
        "def sum_range(n):\n    total = 0\n    for i in range(n - 1):\n        total += i\n    return total\n",
        "def sum_range(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n",
        "from mod import sum_range\n\n\ndef test_zero():\n    assert sum_range(0) == 0\n\n\ndef test_five():\n    assert sum_range(5) == 10\n\n\ndef test_large():\n    assert sum_range(100) == 4950\n",
    ),
    (
        "wrong_operator",
        "减法写成了加法",
        "def diff(a, b):\n    return a + b\n",
        "def diff(a, b):\n    return a - b\n",
        "from mod import diff\n\n\ndef test_basic():\n    assert diff(5, 3) == 2\n\n\ndef test_negative():\n    assert diff(3, 5) == -2\n",
    ),
    (
        "string_compare",
        "比较使用了错误的类型",
        "def is_positive(x):\n    return x >= '0'\n",
        "def is_positive(x):\n    return x > 0\n",
        "from mod import is_positive\n\n\ndef test_positive():\n    assert is_positive(3) is True\n\n\ndef test_zero():\n    assert is_positive(0) is False\n\n\ndef test_negative():\n    assert is_positive(-1) is False\n",
    ),
    (
        "mutable_default",
        "可变默认参数导致跨调用污染",
        "def append_item(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n",
        "def append_item(item, bucket=None):\n    if bucket is None:\n        bucket = []\n    bucket.append(item)\n    return bucket\n",
        "from mod import append_item\n\n\ndef test_first():\n    assert append_item(1) == [1]\n\n\ndef test_second():\n    assert append_item(2) == [2]\n",
    ),
    (
        "sort_direction",
        "排序方向反了",
        "def top_k(nums, k):\n    return sorted(nums)[:k]\n",
        "def top_k(nums, k):\n    return sorted(nums, reverse=True)[:k]\n",
        "from mod import top_k\n\n\ndef test_basic():\n    assert top_k([3, 1, 4, 1, 5], 2) == [5, 4]\n\n\ndef test_k_larger():\n    assert top_k([1, 2], 5) == [2, 1]\n",
    ),
    (
        "division_zero",
        "除数为 0 时崩溃",
        "def average(nums):\n    return sum(nums) / len(nums)\n",
        "def average(nums):\n    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)\n",
        "from mod import average\n\n\ndef test_normal():\n    assert average([2, 4]) == 3.0\n\n\ndef test_empty():\n    assert average([]) == 0.0\n",
    ),
    (
        "string_strip",
        "没有去掉空白导致比较失败",
        "def normalize(name):\n    return name.lower()\n",
        "def normalize(name):\n    return name.strip().lower()\n",
        "from mod import normalize\n\n\ndef test_space():\n    assert normalize('  Ada Lovelace ') == 'ada lovelace'\n\n\ndef test_plain():\n    assert normalize('Bob') == 'bob'\n",
    ),
    (
        "dict_get",
        "取值没有默认值导致 KeyError",
        "def get_score(scores, name):\n    return scores[name]\n",
        "def get_score(scores, name):\n    return scores.get(name, 0)\n",
        "from mod import get_score\n\n\ndef test_present():\n    assert get_score({'a': 1}, 'a') == 1\n\n\ndef test_missing():\n    assert get_score({'a': 1}, 'b') == 0\n",
    ),
    (
        "slice_end",
        "切片漏掉最后一个元素",
        "def last_three(seq):\n    return seq[-3:-1]\n",
        "def last_three(seq):\n    return seq[-3:]\n",
        "from mod import last_three\n\n\ndef test_short():\n    assert last_three([1, 2, 3]) == [1, 2, 3]\n\n\ndef test_long():\n    assert last_three([1, 2, 3, 4, 5]) == [3, 4, 5]\n",
    ),
    (
        "bool_return",
        "返回了非布尔值",
        "def is_even(n):\n    return n % 2\n",
        "def is_even(n):\n    return n % 2 == 0\n",
        "from mod import is_even\n\n\ndef test_even():\n    assert is_even(4) is True\n\n\ndef test_odd():\n    assert is_even(3) is False\n",
    ),
    # ── hard：多文件 / 需要跨模块推理 ──
    (
        "cross_module_import",
        "hard 变体下实现在 core/impl.py 中，mod.py 只是再导出；实际 bug 是返回值没有乘 2",
        "def helper(x):\n    return x\n",
        "def helper(x):\n    return x * 2\n",
        "from mod import helper\n\n\ndef test_compute():\n    assert helper(3) == 6\n",
    ),
    (
        "boundary_logic",
        "边界条件判断用了 <，应为 <=",
        "def in_range(x, lo, hi):\n    return lo < x < hi\n",
        "def in_range(x, lo, hi):\n    return lo <= x <= hi\n",
        "from mod import in_range\n\n\ndef test_inside():\n    assert in_range(5, 1, 10)\n\n\ndef test_lower_bound():\n    assert in_range(1, 1, 10)\n\n\ndef test_upper_bound():\n    assert in_range(10, 1, 10)\n\n\ndef test_outside():\n    assert not in_range(11, 1, 10)\n",
    ),
    (
        "accumulator_reset",
        "循环内累加器被重置",
        "def running_max(nums):\n    out = []\n    best = 0\n    for n in nums:\n        best = n\n        out.append(best)\n    return out\n",
        "def running_max(nums):\n    out = []\n    best = None\n    for n in nums:\n        best = n if best is None else max(best, n)\n        out.append(best)\n    return out\n",
        "from mod import running_max\n\n\ndef test_asc():\n    assert running_max([1, 2, 3]) == [1, 2, 3]\n\n\ndef test_desc():\n    assert running_max([3, 1, 2]) == [3, 3, 3]\n\n\ndef test_negative():\n    assert running_max([-5, -2, -9]) == [-5, -2, -2]\n",
    ),
    (
        "exception_swallow",
        "异常被吞掉导致错误数据返回",
        "def safe_int(text):\n    try:\n        return int(text)\n    except ValueError:\n        return 0\n",
        "def safe_int(text, default=0):\n    try:\n        return int(text)\n    except (ValueError, TypeError):\n        return default\n",
        "from mod import safe_int\n\n\ndef test_valid():\n    assert safe_int('42') == 42\n\n\ndef test_invalid():\n    assert safe_int('abc') == 0\n\n\ndef test_custom_default():\n    assert safe_int('abc', -1) == -1\n\n\ndef test_none():\n    assert safe_int(None) == 0\n",
    ),
    (
        "cache_key",
        "缓存 key 没包含全部参数，导致串味",
        "def cached_area(width, height, scale=1):\n    key = (width, height)\n    if key in _CACHE:\n        return _CACHE[key]\n    value = width * height * scale\n    _CACHE[key] = value\n    return value\n\n\n_CACHE = {}\n",
        "def cached_area(width, height, scale=1):\n    key = (width, height, scale)\n    if key in _CACHE:\n        return _CACHE[key]\n    value = width * height * scale\n    _CACHE[key] = value\n    return value\n\n\n_CACHE = {}\n",
        "from mod import cached_area\n\n\ndef test_default():\n    assert cached_area(2, 3) == 6\n\n\ndef test_scaled():\n    assert cached_area(2, 3, scale=2) == 12\n\n\ndef test_back_to_default():\n    assert cached_area(2, 3) == 6\n",
    ),
    (
        "unicode_width",
        "按字节而非字符计算长度",
        "def display_width(text):\n    return len(text.encode('utf-8'))\n",
        "def display_width(text):\n    return len(text)\n",
        "from mod import display_width\n\n\ndef test_ascii():\n    assert display_width('abc') == 3\n\n\ndef test_cjk():\n    assert display_width('中文') == 2\n",
    ),
    (
        "rounding",
        "用 int() 截断而非四舍五入",
        "def to_cents(amount):\n    return int(amount * 100)\n",
        "def to_cents(amount):\n    return round(amount * 100)\n",
        "from mod import to_cents\n\n\ndef test_exact():\n    assert to_cents(1.5) == 150\n\n\ndef test_round_up():\n    assert to_cents(1.239) == 124\n\n\ndef test_round_down():\n    assert to_cents(1.231) == 123\n",
    ),
    (
        "token_bucket",
        "令牌桶补充逻辑永久阻塞",
        "class Bucket:\n    def __init__(self, capacity, refill):\n        self.capacity = capacity\n        self.tokens = capacity\n        self.refill = refill\n\n    def allow(self):\n        if self.tokens >= 1:\n            self.tokens -= 1\n            return True\n        return False\n\n    def tick(self):\n        self.tokens = self.capacity\n",
        "class Bucket:\n    def __init__(self, capacity, refill):\n        self.capacity = capacity\n        self.tokens = capacity\n        self.refill = refill\n\n    def allow(self):\n        if self.tokens >= 1:\n            self.tokens -= 1\n            return True\n        return False\n\n    def tick(self):\n        self.tokens = min(self.capacity, self.tokens + self.refill)\n",
        "from mod import Bucket\n\n\ndef test_initial():\n    b = Bucket(2, 1)\n    assert b.allow() and b.allow()\n    assert not b.allow()\n\n\ndef test_refill_partial():\n    b = Bucket(2, 1)\n    b.allow(); b.allow()\n    b.tick()\n    assert b.allow()\n    assert not b.allow()\n",
    ),
    (
        "merge_dicts",
        "浅合并时后者的 None 覆盖了有效值",
        "def merge(base, override):\n    out = dict(base)\n    out.update(override)\n    return out\n",
        "def merge(base, override):\n    out = dict(base)\n    for key, value in override.items():\n        if value is not None:\n            out[key] = value\n    return out\n",
        "from mod import merge\n\n\ndef test_override():\n    assert merge({'a': 1}, {'a': 2}) == {'a': 2}\n\n\ndef test_none_ignored():\n    assert merge({'a': 1, 'b': 3}, {'a': None}) == {'a': 1, 'b': 3}\n",
    ),
    (
        "async_ordering",
        "串行执行导致顺序与预期不符",
        "def process(items, fn):\n    results = []\n    for item in reversed(items):\n        results.append(fn(item))\n    return results\n",
        "def process(items, fn):\n    return [fn(item) for item in items]\n",
        "from mod import process\n\n\ndef test_order():\n    assert process([1, 2, 3], lambda x: x * 10) == [10, 20, 30]\n",
    ),
    (
        "leak_on_error",
        "出错时没有清理临时文件",
        "import os\n\n\ndef write_tmp(path, data):\n    handle = open(path, 'w')\n    handle.write(data)\n    handle.close()\n",
        "import os\n\n\ndef write_tmp(path, data):\n    with open(path, 'w') as handle:\n        handle.write(data)\n",
        "import os\nfrom mod import write_tmp\n\n\ndef test_write(tmp_path):\n    target = tmp_path / 'out.txt'\n    write_tmp(str(target), 'hello')\n    assert target.read_text() == 'hello'\n",
    ),
    (
        "final_aggregation",
        "聚合时漏掉了最后一个分片",
        "def aggregate(chunks):\n    total = 0\n    for i in range(len(chunks) - 1):\n        partial = chunks[i]\n        for value in partial:\n            total += value\n    return total\n",
        "def aggregate(chunks):\n    total = 0\n    for partial in chunks:\n        for value in partial:\n            total += value\n    return total\n",
        "from mod import aggregate\n\n\ndef test_single():\n    assert aggregate([[1, 2, 3]]) == 6\n\n\ndef test_multi():\n    assert aggregate([[1], [2, 3], [4]]) == 10\n\n\ndef test_empty():\n    assert aggregate([]) == 0\n",
    ),
]

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]


def _build(index: int) -> Task:
    name, desc, buggy, fixed, tests = _CASES[index]
    difficulty = "easy" if index < 10 else "hard"
    buggy_module = buggy.replace("def ", "def ", 1)

    files: dict[str, str] = {
        "mod.py": buggy_module,
        "test_mod.py": tests,
        "README.md": f"# 任务\n\n修复 `mod.py` 中的 bug：{desc}\n\n运行 `python -m pytest -q` 验证。\n",
    }

    # hard 变体：把核心代码拆到子模块，制造跨文件定位成本
    if difficulty == "hard":
        files["mod.py"] = "from core.impl import *\n"
        files["core/impl.py"] = buggy_module
        files["core/__init__.py"] = ""
        files["README.md"] = (
            f"# 任务\n\n修复 bug：{desc}\n\n"
            "注意：`mod.py` 只是再导出，真正实现在 `core/impl.py`。\n\n"
            "运行 `python -m pytest -q` 验证。\n"
        )
        # hard 变体里加一个诱饵模块，误导只看 mod.py 的做法
        files["core/legacy.py"] = "# 旧实现，已废弃，不要修改\n\ndef unused(x):\n    return x\n"
        return Task(
            task_id=f"t1-{index + 1:03d}",
            template="t1_bugfix",
            difficulty=difficulty,
            prompt=(
                f"仓库里有一个 bug：{desc}。\n"
                "请定位并修复它，使 `python -m pytest -q` 全部通过。\n"
                "不要修改测试文件。"
            ),
            files=files,
            check_cmd=_CHECK,
            meta={"case": name, "reference_files": {"core/impl.py": fixed}},
        )

    return Task(
        task_id=f"t1-{index + 1:03d}",
        template="t1_bugfix",
        difficulty=difficulty,
        prompt=(
            f"`mod.py` 有 bug：{desc}。\n请修复它，使 `python -m pytest -q` 全部通过。\n不要修改测试文件。"
        ),
        files=files,
        check_cmd=_CHECK,
        meta={"case": name, "reference_files": {"mod.py": fixed}},
    )


@register("t1_bugfix")
def generate() -> list[Task]:
    """10 个 easy + 10 个 hard，共 20 个任务。"""
    return [_build(i) for i in range(20)]
