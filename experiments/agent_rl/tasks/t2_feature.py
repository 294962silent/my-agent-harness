"""T2：功能补全。

给模型一个**有签名和文档字符串但未实现**的函数（`raise NotImplementedError`），
要求实现到测试通过。判定：`python -m pytest -q` 退出码为 0。

easy：单文件、单函数；hard：多函数 + 边界条件 + 需要跨模块协作。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]

# (变体名, 需求描述, 骨架代码, 参考实现, 测试代码)
_CASES: list[tuple[str, str, str, str, str]] = [
    (
        "clamp",
        "把数值限制在 [lo, hi] 区间内",
        'def clamp(value, lo, hi):\n    """把 value 限制在 [lo, hi] 内并返回。"""\n    raise NotImplementedError\n',
        "def clamp(value, lo, hi):\n    return max(lo, min(hi, value))\n",
        "from mod import clamp\n\n\ndef test_inside():\n    assert clamp(5, 1, 10) == 5\n\n\ndef test_below():\n    assert clamp(0, 1, 10) == 1\n\n\ndef test_above():\n    assert clamp(20, 1, 10) == 10\n",
    ),
    (
        "unique_preserve",
        "去重且保持首次出现顺序",
        'def unique(items):\n    """返回去重后的列表，保持首次出现顺序。"""\n    raise NotImplementedError\n',
        "def unique(items):\n    seen = set()\n    out = []\n    for item in items:\n        if item not in seen:\n            seen.add(item)\n            out.append(item)\n    return out\n",
        "from mod import unique\n\n\ndef test_dups():\n    assert unique([1, 2, 1, 3, 2]) == [1, 2, 3]\n\n\ndef test_empty():\n    assert unique([]) == []\n\n\ndef test_strings():\n    assert unique(['a', 'b', 'a']) == ['a', 'b']\n",
    ),
    (
        "word_count",
        "统计单词出现次数（忽略大小写）",
        'def word_count(text):\n    """返回 {小写单词: 次数}，以空白分隔，忽略大小写。"""\n    raise NotImplementedError\n',
        "def word_count(text):\n    counts = {}\n    for word in text.lower().split():\n        counts[word] = counts.get(word, 0) + 1\n    return counts\n",
        "from mod import word_count\n\n\ndef test_basic():\n    assert word_count('a b a') == {'a': 2, 'b': 1}\n\n\ndef test_case():\n    assert word_count('Hello hello') == {'hello': 2}\n\n\ndef test_empty():\n    assert word_count('') == {}\n",
    ),
    (
        "chunk",
        "把列表按固定大小切块",
        'def chunk(items, size):\n    """把 items 按 size 切分成若干块；size<=0 抛 ValueError。"""\n    raise NotImplementedError\n',
        'def chunk(items, size):\n    if size <= 0:\n        raise ValueError("size must be positive")\n    return [items[i:i + size] for i in range(0, len(items), size)]\n',
        "import pytest\nfrom mod import chunk\n\n\ndef test_even():\n    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]\n\n\ndef test_remainder():\n    assert chunk([1, 2, 3], 2) == [[1, 2], [3]]\n\n\ndef test_bad_size():\n    with pytest.raises(ValueError):\n        chunk([1], 0)\n",
    ),
    (
        "flatten",
        "把嵌套一层的列表拍平",
        'def flatten(nested):\n    """把 [[1,2],[3]] 变成 [1,2,3]。"""\n    raise NotImplementedError\n',
        "def flatten(nested):\n    return [item for group in nested for item in group]\n",
        "from mod import flatten\n\n\ndef test_basic():\n    assert flatten([[1, 2], [3]]) == [1, 2, 3]\n\n\ndef test_empty_groups():\n    assert flatten([[], [1], []]) == [1]\n\n\ndef test_all_empty():\n    assert flatten([]) == []\n",
    ),
    (
        "group_by",
        "按某个键函数分组",
        'def group_by(items, key_fn):\n    """按 key_fn(item) 分组，返回 {key: [items]}。"""\n    raise NotImplementedError\n',
        "def group_by(items, key_fn):\n    out = {}\n    for item in items:\n        out.setdefault(key_fn(item), []).append(item)\n    return out\n",
        "from mod import group_by\n\n\ndef test_by_len():\n    assert group_by(['a', 'bb', 'c'], len) == {1: ['a', 'c'], 2: ['bb']}\n\n\ndef test_empty():\n    assert group_by([], len) == {}\n",
    ),
    (
        "safe_div",
        "安全除法，除数为 0 时返回默认值",
        'def safe_div(a, b, default=None):\n    """b 为 0 时返回 default，否则返回 a/b。"""\n    raise NotImplementedError\n',
        "def safe_div(a, b, default=None):\n    if b == 0:\n        return default\n    return a / b\n",
        "from mod import safe_div\n\n\ndef test_normal():\n    assert safe_div(6, 3) == 2\n\n\ndef test_zero_default():\n    assert safe_div(1, 0) is None\n\n\ndef test_zero_custom():\n    assert safe_div(1, 0, -1) == -1\n",
    ),
    (
        "truncate_text",
        "截断文本并加省略号",
        'def truncate(text, limit, suffix="..."):\n    """超过 limit 时截断并追加 suffix；limit<=len(suffix) 时直接硬截断。"""\n    raise NotImplementedError\n',
        'def truncate(text, limit, suffix="..."):\n    if len(text) <= limit:\n        return text\n    if limit <= len(suffix):\n        return text[:limit]\n    return text[: limit - len(suffix)] + suffix\n',
        "from mod import truncate\n\n\ndef test_short():\n    assert truncate('abc', 10) == 'abc'\n\n\ndef test_long():\n    assert truncate('abcdef', 5) == 'ab...'\n\n\ndef test_tiny_limit():\n    assert truncate('abcdef', 2) == 'ab'\n",
    ),
    (
        "merge_intervals",
        "合并重叠的区间",
        'def merge_intervals(intervals):\n    """合并重叠或相邻区间，返回按起点排序的列表。"""\n    raise NotImplementedError\n',
        'def merge_intervals(intervals):\n    if not intervals:\n        return []\n    ordered = sorted(intervals)\n    out = [list(ordered[0])]\n    for start, end in ordered[1:]:\n        if start <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], end)\n        else:\n            out.append([start, end])\n    return [tuple(x) for x in out]\n',
        "from mod import merge_intervals\n\n\ndef test_overlap():\n    assert merge_intervals([(1, 3), (2, 6)]) == [(1, 6)]\n\n\ndef test_disjoint():\n    assert merge_intervals([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]\n\n\ndef test_empty():\n    assert merge_intervals([]) == []\n\n\ndef test_unsorted():\n    assert merge_intervals([(5, 7), (1, 2)]) == [(1, 2), (5, 7)]\n",
    ),
    (
        "parse_duration",
        "把 '1h30m' 解析成秒数",
        'def parse_duration(text):\n    """解析形如 1h30m / 45s / 2h 的字符串为秒数；无法解析抛 ValueError。"""\n    raise NotImplementedError\n',
        'import re\n\n_UNITS = {"h": 3600, "m": 60, "s": 1}\n\n\ndef parse_duration(text):\n    parts = re.findall(r"(\\d+)([hms])", text or "")\n    if not parts:\n        raise ValueError(f"invalid duration: {text!r}")\n    return sum(int(value) * _UNITS[unit] for value, unit in parts)\n',
        "import pytest\nfrom mod import parse_duration\n\n\ndef test_hours_minutes():\n    assert parse_duration('1h30m') == 5400\n\n\ndef test_seconds():\n    assert parse_duration('45s') == 45\n\n\ndef test_hours():\n    assert parse_duration('2h') == 7200\n\n\ndef test_invalid():\n    with pytest.raises(ValueError):\n        parse_duration('abc')\n",
        ),
    # ── hard：多函数 + 跨模块 ──
    (
        "lru_cache",
        "实现带容量上限的 LRU 缓存类",
        'class LRUCache:\n    """按插入/访问顺序淘汰的最久未使用缓存。"""\n\n    def __init__(self, capacity):\n        raise NotImplementedError\n\n    def get(self, key):\n        raise NotImplementedError\n\n    def put(self, key, value):\n        raise NotImplementedError\n',
        'from collections import OrderedDict\n\n\nclass LRUCache:\n    def __init__(self, capacity):\n        self.capacity = capacity\n        self._data = OrderedDict()\n\n    def get(self, key):\n        if key not in self._data:\n            return None\n        self._data.move_to_end(key)\n        return self._data[key]\n\n    def put(self, key, value):\n        if key in self._data:\n            self._data.move_to_end(key)\n        self._data[key] = value\n        if len(self._data) > self.capacity:\n            self._data.popitem(last=False)\n',
        "from mod import LRUCache\n\n\ndef test_evict():\n    c = LRUCache(2)\n    c.put('a', 1); c.put('b', 2)\n    c.put('c', 3)\n    assert c.get('a') is None\n\n\ndef test_touch():\n    c = LRUCache(2)\n    c.put('a', 1); c.put('b', 2)\n    assert c.get('a') == 1\n    c.put('c', 3)\n    assert c.get('b') is None\n    assert c.get('a') == 1\n",
    ),
    (
        "topological_sort",
        "对有向无环图做拓扑排序",
        'def topo_sort(graph):\n    """graph: {node: [依赖节点]}；返回拓扑序；有环抛 ValueError。"""\n    raise NotImplementedError\n',
        'def topo_sort(graph):\n    visited = {}\n    order = []\n\n    def visit(node, stack):\n        if visited.get(node) == "done":\n            return\n        if visited.get(node) == "visiting":\n            raise ValueError(f"cycle detected at {node}")\n        visited[node] = "visiting"\n        for dep in graph.get(node, []):\n            visit(dep, stack)\n        visited[node] = "done"\n        order.append(node)\n\n    for node in graph:\n        visit(node, [])\n    return order\n',
        "import pytest\nfrom mod import topo_sort\n\n\ndef test_linear():\n    assert topo_sort({'b': ['a'], 'a': []}) == ['a', 'b']\n\n\ndef test_diamond():\n    result = topo_sort({'d': ['b', 'c'], 'b': ['a'], 'c': ['a'], 'a': []})\n    assert result.index('a') < result.index('b') < result.index('d')\n\n\ndef test_cycle():\n    with pytest.raises(ValueError):\n        topo_sort({'a': ['b'], 'b': ['a']})\n",
    ),
    (
        "retry_backoff",
        "实现带指数退避的重试装饰器",
        'def retry(times):\n    """装饰器：失败时重试 times 次，间隔按 2 的幂递增（测试中会零延迟）。"""\n    raise NotImplementedError\n',
        'import functools\nimport time\n\n\ndef retry(times):\n    def deco(fn):\n        @functools.wraps(fn)\n        def wrapper(*args, **kwargs):\n            last = None\n            for attempt in range(times):\n                try:\n                    return fn(*args, **kwargs)\n                except Exception as exc:  # noqa: BLE001\n                    last = exc\n                    if attempt < times - 1:\n                        time.sleep(0)\n            raise last\n\n        return wrapper\n\n    return deco\n',
        "import pytest\nfrom mod import retry\n\n\ndef test_success_first_try():\n    calls = []\n\n    @retry(3)\n    def ok():\n        calls.append(1)\n        return 'yes'\n\n    assert ok() == 'yes'\n    assert len(calls) == 1\n\n\ndef test_retries_then_succeeds():\n    calls = []\n\n    @retry(3)\n    def flaky():\n        calls.append(1)\n        if len(calls) < 3:\n            raise RuntimeError('boom')\n        return 'done'\n\n    assert flaky() == 'done'\n    assert len(calls) == 3\n\n\ndef test_exhausted():\n    @retry(2)\n    def always_fails():\n        raise RuntimeError('nope')\n\n    with pytest.raises(RuntimeError):\n        always_fails()\n",
    ),
    (
        "json_patch",
        "实现 JSON 的浅合并补丁（支持 null 删除）",
        'def apply_patch(document, patch):\n    """把 patch 合并进 document：值为 None 表示删除该键；返回新字典。"""\n    raise NotImplementedError\n',
        'def apply_patch(document, patch):\n    out = dict(document)\n    for key, value in patch.items():\n        if value is None:\n            out.pop(key, None)\n        else:\n            out[key] = value\n    return out\n',
        "from mod import apply_patch\n\n\ndef test_update():\n    assert apply_patch({'a': 1}, {'a': 2}) == {'a': 2}\n\n\ndef test_delete():\n    assert apply_patch({'a': 1, 'b': 2}, {'a': None}) == {'b': 2}\n\n\ndef test_add():\n    assert apply_patch({}, {'x': 9}) == {'x': 9}\n\n\ndef test_original_untouched():\n    doc = {'a': 1}\n    apply_patch(doc, {'a': 2})\n    assert doc == {'a': 1}\n",
    ),
    (
        "text_table",
        "把二维数据渲染成等宽文本表格",
        'def render_table(rows):\n    """rows: 第一行为表头；返回带边框的等宽文本，列宽取该列最长值。"""\n    raise NotImplementedError\n',
        'def render_table(rows):\n    if not rows:\n        return ""\n    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]\n    lines = []\n    for row in rows:\n        cells = [str(cell).ljust(widths[i]) for i, cell in enumerate(row)]\n        lines.append("| " + " | ".join(cells) + " |")\n    return "\\n".join(lines)\n',
        'from mod import render_table\n\n\ndef test_simple():\n    out = render_table([["a", "bb"], ["ccc", "d"]])\n    assert out.splitlines()[0] == "| a   | bb |"\n\n\ndef test_alignment():\n    out = render_table([["name", "x"], ["bob", "yy"]])\n    assert out.splitlines()[1].startswith("| bob ")\n',
    ),
    (
        "state_machine",
        "实现一个简单的订单状态机",
        'TRANSITIONS = {}\n\n\ndef transition(current, event):\n    """按 TRANSITIONS 定义推进状态；非法转移抛 ValueError。"""\n    raise NotImplementedError\n',
        'TRANSITIONS = {\n    ("pending", "pay"): "paid",\n    ("paid", "ship"): "shipped",\n    ("shipped", "deliver"): "delivered",\n    ("paid", "refund"): "refunded",\n}\n\n\ndef transition(current, event):\n    try:\n        return TRANSITIONS[(current, event)]\n    except KeyError:\n        raise ValueError(f"invalid transition: {current} --{event}-->") from None\n',
        "import pytest\nfrom mod import transition\n\n\ndef test_happy_path():\n    state = 'pending'\n    for event, expected in [('pay', 'paid'), ('ship', 'shipped'), ('deliver', 'delivered')]:\n        state = transition(state, event)\n        assert state == expected\n\n\ndef test_refund():\n    assert transition('paid', 'refund') == 'refunded'\n\n\ndef test_invalid():\n    with pytest.raises(ValueError):\n        transition('pending', 'deliver')\n",
    ),
    (
        "binary_search",
        "实现二分查找（返回下标或 -1）",
        'def bisect_index(items, target):\n    """在已排序列表中查找 target，返回下标；不存在返回 -1。要求 O(log n)。"""\n    raise NotImplementedError\n',
        'def bisect_index(items, target):\n    lo, hi = 0, len(items) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if items[mid] == target:\n            return mid\n        if items[mid] < target:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n',
        "from mod import bisect_index\n\n\ndef test_found():\n    assert bisect_index([1, 3, 5, 7], 5) == 2\n\n\ndef test_missing():\n    assert bisect_index([1, 3, 5], 4) == -1\n\n\ndef test_empty():\n    assert bisect_index([], 1) == -1\n\n\ndef test_single():\n    assert bisect_index([2], 2) == 0\n",
    ),
    (
        "csv_filter",
        "过滤 CSV 行并按某列排序",
        'def filter_and_sort(rows, column, predicate):\n    """rows 为 dict 列表；返回满足 predicate(row[column]) 的行，按该列升序排序。"""\n    raise NotImplementedError\n',
        'def filter_and_sort(rows, column, predicate):\n    kept = [row for row in rows if predicate(row[column])]\n    return sorted(kept, key=lambda row: row[column])\n',
        "from mod import filter_and_sort\n\n\ndef test_filter_and_sort():\n    rows = [{'n': 3}, {'n': 1}, {'n': 2}]\n    out = filter_and_sort(rows, 'n', lambda v: v > 1)\n    assert [r['n'] for r in out] == [2, 3]\n\n\ndef test_all_filtered():\n    assert filter_and_sort([{'n': 0}], 'n', lambda v: v > 5) == []\n",
    ),
    (
        "incremental_stats",
        "增量维护均值与方差",
        'class Stats:\n    """增量统计：add 累加样本，mean/variance 返回当前值（样本方差）。"""\n\n    def __init__(self):\n        raise NotImplementedError\n\n    def add(self, value):\n        raise NotImplementedError\n\n    @property\n    def mean(self):\n        raise NotImplementedError\n\n    @property\n    def variance(self):\n        raise NotImplementedError\n',
        'class Stats:\n    def __init__(self):\n        self.n = 0\n        self.total = 0.0\n        self.total_sq = 0.0\n\n    def add(self, value):\n        self.n += 1\n        self.total += value\n        self.total_sq += value * value\n\n    @property\n    def mean(self):\n        return self.total / self.n if self.n else 0.0\n\n    @property\n    def variance(self):\n        if self.n < 2:\n            return 0.0\n        return (self.total_sq - self.total ** 2 / self.n) / (self.n - 1)\n',
        "import pytest\nfrom mod import Stats\n\n\ndef test_mean():\n    s = Stats()\n    for v in (1, 2, 3):\n        s.add(v)\n    assert s.mean == 2\n\n\ndef test_variance():\n    s = Stats()\n    for v in (1, 2, 3, 4):\n        s.add(v)\n    assert abs(s.variance - 1.6667) < 0.01\n\n\ndef test_empty():\n    s = Stats()\n    assert s.mean == 0\n    assert s.variance == 0\n",
    ),
    (
        "matrix_mul",
        "实现矩阵乘法（含维度校验）",
        'def matmul(a, b):\n    """a: m×n，b: n×p；维度不匹配抛 ValueError。"""\n    raise NotImplementedError\n',
        'def matmul(a, b):\n    if not a or not b:\n        return []\n    if len(a[0]) != len(b):\n        raise ValueError("dimension mismatch")\n    rows, inner, cols = len(a), len(b), len(b[0])\n    out = [[0] * cols for _ in range(rows)]\n    for i in range(rows):\n        for j in range(cols):\n            out[i][j] = sum(a[i][k] * b[k][j] for k in range(inner))\n    return out\n',
        "import pytest\nfrom mod import matmul\n\n\ndef test_identity():\n    assert matmul([[1, 2]], [[1, 0], [0, 1]]) == [[1, 2]]\n\n\ndef test_square():\n    assert matmul([[1, 2], [3, 4]], [[5, 6], [7, 8]]) == [[19, 22], [43, 50]]\n\n\ndef test_mismatch():\n    with pytest.raises(ValueError):\n        matmul([[1, 2], [3, 4]], [[1]])\n",
    ),
    (
        "event_sourcing",
        "事件溯源：重放事件得到最终状态",
        'def replay(events, initial=None):\n    """events: [{"type": "set"/"inc"/"delete", "key": ..., "value": ...}]；返回最终状态 dict。"""\n    raise NotImplementedError\n',
        'def replay(events, initial=None):\n    state = dict(initial or {})\n    for event in events:\n        kind = event.get("type")\n        key = event.get("key")\n        if kind == "set":\n            state[key] = event.get("value")\n        elif kind == "inc":\n            state[key] = state.get(key, 0) + int(event.get("value") or 0)\n        elif kind == "delete":\n            state.pop(key, None)\n    return state\n',
        "from mod import replay\n\n\ndef test_set_inc_delete():\n    events = [\n        {'type': 'set', 'key': 'a', 'value': 1},\n        {'type': 'inc', 'key': 'a', 'value': 2},\n        {'type': 'set', 'key': 'b', 'value': 5},\n        {'type': 'delete', 'key': 'b'},\n    ]\n    assert replay(events) == {'a': 3}\n\n\ndef test_initial():\n    assert replay([], {'x': 1}) == {'x': 1}\n",
    ),
    (
        "pagination",
        "把长列表分页并生成导航信息",
        'def paginate(items, page, per_page):\n    """返回 dict: items/page/per_page/total_pages/has_next/has_prev；page 从 1 开始。"""\n    raise NotImplementedError\n',
        'import math\n\n\ndef paginate(items, page, per_page):\n    per_page = max(1, per_page)\n    total_pages = max(1, math.ceil(len(items) / per_page))\n    page = min(max(1, page), total_pages)\n    start = (page - 1) * per_page\n    return {\n        "items": items[start:start + per_page],\n        "page": page,\n        "per_page": per_page,\n        "total_pages": total_pages,\n        "has_next": page < total_pages,\n        "has_prev": page > 1,\n    }\n',
        "from mod import paginate\n\n\ndef test_first_page():\n    out = paginate(list(range(10)), 1, 3)\n    assert out['items'] == [0, 1, 2]\n    assert out['total_pages'] == 4\n    assert out['has_prev'] is False\n    assert out['has_next'] is True\n\n\ndef test_clamped():\n    out = paginate(list(range(5)), 99, 2)\n    assert out['page'] == 3\n    assert out['has_next'] is False\n",
    ),
]


def _build(index: int) -> Task:
    name, desc, skeleton, implementation, tests = _CASES[index]
    difficulty = "easy" if index < 10 else "hard"

    files: dict[str, str] = {
        "mod.py": skeleton,
        "test_mod.py": tests,
        "README.md": f"# 任务\n\n实现 `mod.py` 中的功能：{desc}\n\n运行 `python -m pytest -q` 验证。\n",
    }

    if difficulty == "hard":
        files["mod.py"] = "from impl.core import *\n"
        files["impl/__init__.py"] = ""
        files["impl/core.py"] = skeleton
        files["impl/draft.py"] = "# 草稿：早期尝试，未完成，不要作为最终实现\n\ndef placeholder():\n    raise NotImplementedError\n"
        files["README.md"] = (
            f"# 任务\n\n实现功能：{desc}\n\n"
            "注意：`mod.py` 只是再导出，实现写在 `impl/core.py`。\n\n运行 `python -m pytest -q` 验证。\n"
        )
        return Task(
            task_id=f"t2-{index + 1:03d}",
            template="t2_feature",
            difficulty=difficulty,
            prompt=(
                f"实现功能：{desc}\n"
                "`mod.py` 只做再导出，真正的实现位置需要你自己找。\n"
                "使 `python -m pytest -q` 全部通过；不要修改测试文件。"
            ),
            files=files,
            check_cmd=_CHECK,
            meta={"case": name, "reference_files": {"impl/core.py": implementation}},
        )

    return Task(
        task_id=f"t2-{index + 1:03d}",
        template="t2_feature",
        difficulty=difficulty,
        prompt=(
            f"实现 `mod.py` 中的功能：{desc}\n使 `python -m pytest -q` 全部通过；不要修改测试文件。"
        ),
        files=files,
        check_cmd=_CHECK,
        meta={"case": name, "reference_files": {"mod.py": implementation}},
    )


@register("t2_feature")
def generate() -> list[Task]:
    """10 个 easy + 10 个 hard，共 20 个任务。"""
    return [_build(i) for i in range(20)]
