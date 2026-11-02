"""依赖链深度对照实验：控制模块数，只改变依赖链深度。

假设（来自 6 任务观察）：难度由依赖链深度决定，而非模块数。

设计：都是 4 个模块，但：
- **扁平版**：4 个模块互不依赖，各自独立实现
- **深链版**：4 个模块串成一条链 A→B→C→D，每层依赖上一层

如果扁平版步数明显少于深链版，假设成立。

模块数、文件数、测试数量都对齐，只改依赖结构。
"""

from __future__ import annotations

from .t8_long_horizon import LongTask, Stage

# ── 扁平版：4 个独立模块 ──────────────────────────────────────
_FLAT_STAGES = [
    Stage(
        name="upper",
        files={"flat/__init__.py": "", "flat/upper.py": '"""转大写（待实现）"""\n\n\ndef upper(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_upper.py",
        test_content=(
            "from flat.upper import upper\n\n\n"
            "def test_basic():\n"
            "    assert upper('abc') == 'ABC'\n"
        ),
    ),
    Stage(
        name="reverse",
        files={"flat/reverse.py": '"""反转（待实现）"""\n\n\ndef reverse(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_reverse.py",
        test_content=(
            "from flat.reverse import reverse\n\n\n"
            "def test_basic():\n"
            "    assert reverse('abc') == 'cba'\n"
        ),
    ),
    Stage(
        name="count",
        files={"flat/count.py": '"""计数（待实现）"""\n\n\ndef count_words(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_count.py",
        test_content=(
            "from flat.count import count_words\n\n\n"
            "def test_basic():\n"
            "    assert count_words('a b c') == 3\n"
        ),
    ),
    Stage(
        name="strip",
        files={"flat/strip.py": '"""去空白（待实现）"""\n\n\ndef strip_all(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_strip.py",
        test_content=(
            "from flat.strip import strip_all\n\n\n"
            "def test_basic():\n"
            "    assert strip_all('  a  ') == 'a'\n"
        ),
    ),
]

_FLAT_PROMPT = """实现 4 个**互相独立**的文本工具（无依赖关系，可任意顺序实现）：

1. `flat/upper.py` — `upper(text) -> str`：转大写
2. `flat/reverse.py` — `reverse(text) -> str`：反转字符串
3. `flat/count.py` — `count_words(text) -> int`：统计单词数
4. `flat/strip.py` — `strip_all(text) -> str`：去掉首尾空白

所有测试必须通过。
"""


# ── 深链版：4 个模块串成一条链 ────────────────────────────────
_CHAIN_STAGES = [
    Stage(
        name="tokenizer",
        files={"chain/__init__.py": "", "chain/tokenizer.py": '"""分词（待实现）"""\n\n\ndef tokenize(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_tokenizer.py",
        test_content=(
            "from chain.tokenizer import tokenize\n\n\n"
            "def test_basic():\n"
            "    assert tokenize('a b') == ['a', 'b']\n"
        ),
    ),
    Stage(
        name="lower",
        files={"chain/lower.py": '"""小写化（依赖 tokenizer）（待实现）"""\n\n\ndef lower_tokens(text):\n    raise NotImplementedError\n'},
        test_file="tests/test_lower.py",
        test_content=(
            "from chain.lower import lower_tokens\n\n\n"
            "def test_basic():\n"
            "    assert lower_tokens('A B') == ['a', 'b']\n"
        ),
    ),
    Stage(
        name="filter",
        files={"chain/filter.py": '"""过滤（依赖 lower）（待实现）"""\n\n\ndef drop_stopwords(text, stop):\n    raise NotImplementedError\n'},
        test_file="tests/test_filter.py",
        test_content=(
            "from chain.filter import drop_stopwords\n\n\n"
            "def test_basic():\n"
            "    assert drop_stopwords('a the b', {'the'}) == ['a', 'b']\n"
        ),
    ),
    Stage(
        name="join",
        files={"chain/join.py": '"""拼接（依赖 filter）（待实现）"""\n\n\ndef normalize(text, stop):\n    raise NotImplementedError\n'},
        test_file="tests/test_join.py",
        test_content=(
            "from chain.join import normalize\n\n\n"
            "def test_basic():\n"
            "    assert normalize('A the B', {'the'}) == 'a-b'\n\n\n"
            "def test_empty():\n"
            "    assert normalize('', set()) == ''\n"
        ),
    ),
]

_CHAIN_PROMPT = """实现 4 个**串成依赖链**的模块（必须按顺序实现，每层依赖上一层）：

1. `chain/tokenizer.py` — `tokenize(text) -> list[str]`：按空白切分
2. `chain/lower.py` — `lower_tokens(text) -> list[str]`：**调用 tokenize** 后全部小写
3. `chain/filter.py` — `drop_stopwords(text, stop) -> list[str]`：**调用 lower_tokens** 后去掉停用词
4. `chain/join.py` — `normalize(text, stop) -> str`：**调用 drop_stopwords** 后用 `-` 连接

依赖关系：tokenize → lower_tokens → drop_stopwords → normalize
如果下层实现错误，上层测试也会失败。

所有测试必须通过。
"""


_SPECS = [
    ("007", "扁平版：4 个独立模块", _FLAT_PROMPT, _FLAT_STAGES),
    ("008", "深链版：4 层依赖链", _CHAIN_PROMPT, _CHAIN_STAGES),
]


def generate() -> list:
    return [
        LongTask(task_id=f"t8-{tid}", title=title, prompt=prompt, stages=stages)
        for tid, title, prompt, stages in _SPECS
    ]


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8c_chain_depth"] = lambda: [t.to_task() for t in generate()]
