"""多缺陷注入：把一个单点 bug 复制成多个独立缺陷。

动机：单点 bug 模型一次就能改对（100% 通过），
但"一题 3 个独立缺陷、必须全部改对"会产生部分成功——这正是 pass^k 需要的中间态。

做法：对 T1 类任务，把同一模块里**多个互不相关的 bug** 组合起来。
每个 bug 由独立测试覆盖，改对一个只能让部分测试通过。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]

# 每个组件是 (buggy 代码片段, 修正后片段, 对应的测试)
_BUG_COMPONENTS: list[dict[str, str]] = [
    {
        "name": "range_bound",
        "buggy": "def sum_to(n):\n    total = 0\n    for i in range(n - 1):\n        total += i\n    return total\n",
        "fixed": "def sum_to(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n",
        "test": "from mod import sum_to\n\n\ndef test_sum_to():\n    assert sum_to(5) == 10\n",
    },
    {
        "name": "string_strip",
        "buggy": "def clean(text):\n    return text.lower()\n",
        "fixed": "def clean(text):\n    return text.strip().lower()\n",
        "test": "from mod import clean\n\n\ndef test_clean():\n    assert clean('  Ab ') == 'ab'\n",
    },
    {
        "name": "dict_default",
        "buggy": "def score(table, key):\n    return table[key]\n",
        "fixed": "def score(table, key):\n    return table.get(key, 0)\n",
        "test": "from mod import score\n\n\ndef test_score_missing():\n    assert score({'a': 1}, 'b') == 0\n",
    },
    {
        "name": "even_check",
        "buggy": "def is_even(n):\n    return n % 2\n",
        "fixed": "def is_even(n):\n    return n % 2 == 0\n",
        "test": "from mod import is_even\n\n\ndef test_even():\n    assert is_even(4) is True\n",
    },
    {
        "name": "slice_tail",
        "buggy": "def last_two(seq):\n    return seq[-2:-1]\n",
        "fixed": "def last_two(seq):\n    return seq[-2:]\n",
        "test": "from mod import last_two\n\n\ndef test_tail():\n    assert last_two([1, 2, 3]) == [2, 3]\n",
    },
    {
        "name": "round_half",
        "buggy": "def to_int(x):\n    return int(x)\n",
        "fixed": "def to_int(x):\n    return round(x)\n",
        "test": "from mod import to_int\n\n\ndef test_round():\n    assert to_int(2.6) == 3\n",
    },
    {
        "name": "dup_bug",
        "buggy": "def unique(items):\n    return list(set(items))\n",
        "fixed": "def unique(items):\n    seen = set()\n    out = []\n    for item in items:\n        if item not in seen:\n            seen.add(item)\n            out.append(item)\n    return out\n",
        "test": "from mod import unique\n\n\ndef test_order():\n    assert unique([3, 1, 3, 2]) == [3, 1, 2]\n",
    },
    {
        "name": "truthiness",
        "buggy": "def is_positive(x):\n    return x\n",
        "fixed": "def is_positive(x):\n    return x > 0\n",
        "test": "from mod import is_positive\n\n\ndef test_zero():\n    assert is_positive(0) is False\n",
    },
]

# 组合方案：3 缺陷 / 4 缺陷 / 5 缺陷
_COMBOS: list[list[int]] = [
    [0, 1, 2],
    [0, 3, 4],
    [1, 2, 5],
    [2, 4, 6],
    [0, 1, 7],
    [0, 2, 3, 4],
    [1, 3, 5, 6],
    [0, 2, 4, 7],
    [0, 1, 2, 3, 4],
    [1, 2, 3, 5, 6],
]


def _build(index: int) -> Task:
    combo = _COMBOS[index]
    buggy_parts = [_BUG_COMPONENTS[i]["buggy"] for i in combo]
    fixed_parts = [_BUG_COMPONENTS[i]["fixed"] for i in combo]
    test_parts = [_BUG_COMPONENTS[i]["test"] for i in combo]
    names = [_BUG_COMPONENTS[i]["name"] for i in combo]

    files = {
        "mod.py": "\n\n".join(buggy_parts),
        "test_mod.py": "\n\n".join(test_parts),
        "README.md": (
            f"# 任务\n\n`mod.py` 里有 **{len(combo)} 个互不相关的 bug**。\n\n"
            "必须**全部修复**才能让测试全部通过；只修一个，其余测试仍会失败。\n\n"
            "运行 `python -m pytest -q` 验证。\n"
        ),
    }

    return Task(
        task_id=f"t7-{index + 1:03d}",
        template="t7_multi_bug",
        difficulty="hard",
        prompt=(
            f"`mod.py` 里有 {len(combo)} 个互不相关的 bug，分布在不同的函数里。\n"
            "请逐个定位并全部修复，使 `python -m pytest -q` 全部通过。\n"
            "不要修改测试文件；不要重写整个文件，只改必要的地方。"
        ),
        files=files,
        check_cmd=_CHECK,
        timeout_s=300,
        meta={
            "case": "+".join(names),
            "bug_count": len(combo),
            "reference_files": {"mod.py": "\n\n".join(fixed_parts)},
        },
    )


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t7_multi_bug"] = lambda: [_build(i) for i in range(len(_COMBOS))]
