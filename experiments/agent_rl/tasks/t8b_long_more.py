"""T8 长程任务扩充：从 3 个扩到 10 个。

现有问题：只有 3 个任务，其中 t8-001（6 模块）是唯一稳定失败的，
t8-002/003（4 模块）稳定通过。样本太少，无法画出一条可信的「难度曲线」。

目标：每个模块数档位都有 2-3 个任务，形成：
  3 模块（难度的下界）→ 4 模块 → 5 模块 → 6 模块（明显超出能力）

这样就能回答：**分界线在第几档？**
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage


def _t(name: str, title: str, prompt: str, stages: list[Stage]) -> LongTask:
    return LongTask(task_id=f"t8-{name}", title=title, prompt=prompt, stages=stages)


# ── 5 模块：配置加载系统 ──────────────────────────────────────
_L4_STAGES = [
    Stage(
        name="reader",
        files={
            "config/__init__.py": "",
            "config/reader.py": '"""配置读取（待实现）"""\n\n\ndef read_text(path):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_reader.py",
        test_content=(
            "from config.reader import read_text\n\n\n"
            "def test_read(tmp_path):\n"
            "    p = tmp_path / 'c.txt'\n"
            "    p.write_text('hello')\n"
            "    assert read_text(str(p)) == 'hello'\n"
        ),
    ),
    Stage(
        name="parser",
        files={
            "config/parser.py": '"""解析（待实现）"""\n\n\ndef parse(text):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_parser.py",
        test_content=(
            "from config.parser import parse\n\n\n"
            "def test_kv():\n"
            "    assert parse('a=1\\nb=2') == {'a': '1', 'b': '2'}\n\n\n"
            "def test_comments():\n"
            "    assert parse('# note\\na=1') == {'a': '1'}\n"
        ),
    ),
    Stage(
        name="coerce",
        files={
            "config/coerce.py": '"""类型转换（待实现）"""\n\n\ndef coerce(value):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_coerce.py",
        test_content=(
            "from config.coerce import coerce\n\n\n"
            "def test_int():\n"
            "    assert coerce('42') == 42\n\n\n"
            "def test_bool():\n"
            "    assert coerce('true') is True\n"
            "    assert coerce('false') is False\n"
            "    assert coerce('0') == 0\n"
        ),
    ),
    Stage(
        name="merge",
        files={
            "config/merge.py": '"""合并（待实现）"""\n\n\ndef merge(base, override):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_merge.py",
        test_content=(
            "from config.merge import merge\n\n\n"
            "def test_override():\n"
            "    assert merge({'a': 1, 'b': 2}, {'b': 3}) == {'a': 1, 'b': 3}\n\n\n"
            "def test_new_key():\n"
            "    assert merge({'a': 1}, {'b': 2})['b'] == 2\n"
        ),
    ),
    Stage(
        name="loader",
        files={
            "config/loader.py": '"""加载器门面（待实现）"""\n\n\ndef load(path, defaults=None):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_loader.py",
        test_content=(
            "from config.loader import load\n\n\n"
            "def test_end_to_end(tmp_path):\n"
            "    p = tmp_path / 'c.cfg'\n"
            "    p.write_text('a=1\\nb=true\\n')\n"
            "    assert load(str(p)) == {'a': 1, 'b': True}\n\n\n"
            "def test_defaults(tmp_path):\n"
            "    p = tmp_path / 'c.cfg'\n"
            "    p.write_text('a=1\\n')\n"
            "    assert load(str(p), {'b': 9})['b'] == 9\n"
        ),
    ),
]

_L4_PROMPT = """实现一个配置加载系统，5 个模块（按依赖顺序）：

1. `config/reader.py` — `read_text(path) -> str`
2. `config/parser.py` — `parse(text) -> dict`：解析 `key=value`，跳过 `#` 注释行
3. `config/coerce.py` — `coerce(value) -> int|bool|str`：'42'→42，'true'/'false'→bool
4. `config/merge.py` — `merge(base, override) -> dict`：override 优先
5. `config/loader.py` — `load(path, defaults=None) -> dict`：串联以上四个

所有测试必须通过。
"""


# ── 5 模块：LRU 缓存与索引 ────────────────────────────────────
_L5_STAGES = [
    Stage(
        name="node",
        files={
            "cache/__init__.py": "",
            "cache/node.py": '"""链表节点（待实现）"""\n\n\nclass Node:\n    def __init__(self, key, value):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_node.py",
        test_content=(
            "from cache.node import Node\n\n\n"
            "def test_links():\n"
            "    a = Node('a', 1)\n"
            "    b = Node('b', 2)\n"
            "    a.next = b\n"
            "    b.prev = a\n"
            "    assert a.next is b and b.prev is a\n"
        ),
    ),
    Stage(
        name="list",
        files={
            "cache/dlist.py": '"""双向链表（待实现）"""\n\n\nclass DList:\n    def __init__(self):\n        raise NotImplementedError\n\n    def push_front(self, node):\n        raise NotImplementedError\n\n    def remove(self, node):\n        raise NotImplementedError\n\n    def pop_back(self):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_dlist.py",
        test_content=(
            "from cache.dlist import DList\n"
            "from cache.node import Node\n\n\n"
            "def test_push_pop():\n"
            "    l = DList()\n"
            "    l.push_front(Node('a', 1))\n"
            "    l.push_front(Node('b', 2))\n"
            "    assert l.pop_back().key == 'a'\n\n\n"
            "def test_remove():\n"
            "    l = DList()\n"
            "    n = Node('a', 1)\n"
            "    l.push_front(n)\n"
            "    l.remove(n)\n"
            "    assert l.pop_back() is None\n"
        ),
    ),
    Stage(
        name="lru",
        files={
            "cache/lru.py": '"""LRU 缓存（待实现）"""\n\n\nclass LRUCache:\n    def __init__(self, capacity):\n        raise NotImplementedError\n\n    def get(self, key):\n        raise NotImplementedError\n\n    def put(self, key, value):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_lru.py",
        test_content=(
            "from cache.lru import LRUCache\n\n\n"
            "def test_basic():\n"
            "    c = LRUCache(2)\n"
            "    c.put('a', 1)\n"
            "    c.put('b', 2)\n"
            "    assert c.get('a') == 1\n\n\n"
            "def test_evict():\n"
            "    c = LRUCache(2)\n"
            "    c.put('a', 1)\n"
            "    c.put('b', 2)\n"
            "    c.put('c', 3)\n"
            "    assert c.get('a') is None\n"
            "    assert c.get('c') == 3\n"
        ),
    ),
    Stage(
        name="ttl",
        files={
            "cache/ttl.py": '"""带 TTL 的缓存（待实现）"""\n\n\nclass TTLCache:\n    def __init__(self, capacity, ttl_at):\n        raise NotImplementedError\n\n    def get(self, key, now):\n        raise NotImplementedError\n\n    def put(self, key, value, now):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_ttl.py",
        test_content=(
            "from cache.ttl import TTLCache\n\n\n"
            "def test_expire():\n"
            "    c = TTLCache(4, 10)\n"
            "    c.put('a', 1, 0)\n"
            "    assert c.get('a', 5) == 1\n"
            "    assert c.get('a', 15) is None\n"
        ),
    ),
    Stage(
        name="index",
        files={
            "cache/index.py": '"""倒排索引（待实现）"""\n\n\nclass Index:\n    def __init__(self):\n        raise NotImplementedError\n\n    def add(self, doc_id, text):\n        raise NotImplementedError\n\n    def search(self, word):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_index.py",
        test_content=(
            "from cache.index import Index\n\n\n"
            "def test_search():\n"
            "    i = Index()\n"
            "    i.add(1, 'hello world')\n"
            "    i.add(2, 'hello there')\n"
            "    assert sorted(i.search('hello')) == [1, 2]\n"
            "    assert i.search('world') == [1]\n"
        ),
    ),
]

_L5_PROMPT = """实现一个缓存与索引系统，5 个模块（按依赖顺序）：

1. `cache/node.py` — `Node(key, value)`：带 prev/next 指针
2. `cache/dlist.py` — `DList`：`push_front` / `remove` / `pop_back`（空时返回 None）
3. `cache/lru.py` — `LRUCache(capacity)`：`get` / `put`，满时淘汰最久未用
4. `cache/ttl.py` — `TTLCache(capacity, ttl_at)`：`get(key, now)` 过期返回 None
5. `cache/index.py` — `Index`：`add(doc_id, text)` / `search(word) -> list[doc_id]`

所有测试必须通过。
"""


# ── 3 模块：简单但需要三步（难度的下界） ──────────────────────
_L6_STAGES = [
    Stage(
        name="slugify",
        files={"textutil/__init__.py": "", "textutil/slug.py": '"""slug（待实现）"""\n\n\ndef slugify(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_slug.py",
        test_content=(
            "from textutil.slug import slugify\n\n\n"
            "def test_basic():\n"
            "    assert slugify('Hello World') == 'hello-world'\n\n\n"
            "def test_punct():\n"
            "    assert slugify('a & b!') == 'a-b'\n"
        ),
    ),
    Stage(
        name="wrap",
        files={"textutil/wrap.py": '"""折行（待实现）"""\n\n\ndef wrap(text, width):\n    raise NotImplementedError\n'},
        test_file="tests/test_wrap.py",
        test_content=(
            "from textutil.wrap import wrap\n\n\n"
            "def test_wrap():\n"
            "    assert wrap('aaa bbb ccc', 7) == ['aaa bbb', 'ccc']\n"
        ),
    ),
    Stage(
        name="truncate",
        files={"textutil/truncate.py": '"""截断（待实现）"""\n\n\ndef truncate(text, limit):\n    raise NotImplementedError\n'},
        test_file="tests/test_truncate.py",
        test_content=(
            "from textutil.truncate import truncate\n\n\n"
            "def test_short():\n"
            "    assert truncate('abc', 10) == 'abc'\n\n\n"
            "def test_long():\n"
            "    assert truncate('abcdef', 4) == 'abc…'\n"
        ),
    ),
]

_L6_PROMPT = """实现三个文本工具函数：

1. `textutil/slug.py` — `slugify(text) -> str`：小写、空格转连字符、去掉标点
2. `textutil/wrap.py` — `wrap(text, width) -> list[str]`：按宽度折行，不切断单词
3. `textutil/truncate.py` — `truncate(text, limit) -> str`：超长时截断并加 `…`（含省略号不超过 limit）

所有测试必须通过。
"""


_SPECS = [
    ("004", "配置加载系统（5 阶段）", _L4_PROMPT, _L4_STAGES),
    ("005", "缓存与索引系统（5 阶段）", _L5_PROMPT, _L5_STAGES),
    ("006", "文本工具（3 阶段）", _L6_PROMPT, _L6_STAGES),
]


def generate() -> list:
    """生成 3 个新长程任务。"""
    return [_t(task_id, title, prompt, stages) for task_id, title, prompt, stages in _SPECS]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    def _gen():
        return [t.to_task() for t in generate()]

    _REGISTRY["t8b_long_horizon"] = _gen
