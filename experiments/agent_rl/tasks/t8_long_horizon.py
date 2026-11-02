"""长程任务生成器：构造需要 50+ 步才能完成的任务。

背景：现有任务平均 10.9 步、最长 22 步，**算不上真正的「长程」**。
方向一（长程 Harness 工程）需要能测出模型在长时间跨度上的表现。

设计原则：
1. **多阶段**：任务分解为 5-8 个阶段，每个阶段有独立验收
2. **强依赖**：后一阶段依赖前一阶段的产物，跳步会失败
3. **长上下文**：文件多、约束多，需要持续记住上下文
4. **可验证**：每阶段有独立测试，能测「完成了几个阶段」

任务形态：实现一个多模块的小系统，逐层交付。

示例（L1）：实现一个 CSV 数据管道
  阶段1：解析器（读 CSV → dict 列表）
  阶段2：校验器（字段类型检查）
  阶段3：转换器（日期/金额规整）
  阶段4：聚合器（分组统计）
  阶段5：导出器（写回 CSV/JSON）
  阶段6：CLI 入口（串联以上）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from experiments.agent_rl.task import Task

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"]


@dataclass
class Stage:
    """一个阶段。"""

    name: str
    files: dict[str, str]
    test_file: str
    test_content: str


@dataclass
class LongTask:
    """一个长程任务。"""

    task_id: str
    title: str
    prompt: str
    stages: list[Stage] = field(default_factory=list)

    def to_task(self) -> Task:
        """转成标准 Task（所有阶段合在一起）。"""
        files: dict[str, str] = {"README.md": f"# {self.title}\n\n{self.prompt}\n"}
        tests: dict[str, str] = {}
        for stage in self.stages:
            files.update(stage.files)
            tests[stage.test_file] = stage.test_content
        files.update(tests)
        return Task(
            task_id=self.task_id,
            template="t8_long_horizon",
            difficulty="hard",
            prompt=self.prompt,
            files=files,
            check_cmd=_CHECK,
            timeout_s=1200,
            meta={
                "stages": [s.name for s in self.stages],
                "num_stages": len(self.stages),
                "long_horizon": True,
            },
        )


# ── L1: CSV 数据管道 ──────────────────────────────────────────
_L1_STAGES = [
    Stage(
        name="parser",
        files={
            "pipeline/__init__.py": "",
            "pipeline/parser.py": '"""CSV 解析器（待实现）"""\n\n\ndef parse_csv(text):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_parser.py",
        test_content=(
            "from pipeline.parser import parse_csv\n\n\n"
            "def test_basic():\n"
            "    rows = parse_csv('name,age\\nalice,30\\nbob,25\\n')\n"
            "    assert len(rows) == 2\n"
            "    assert rows[0]['name'] == 'alice'\n\n\n"
            "def test_empty_lines():\n"
            "    rows = parse_csv('name,age\\nalice,30\\n\\n')\n"
            "    assert len(rows) == 1\n"
        ),
    ),
    Stage(
        name="validator",
        files={
            "pipeline/validator.py": '"""校验器（待实现）"""\n\n\ndef validate(rows, schema):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_validator.py",
        test_content=(
            "import pytest\n\n"
            "from pipeline.validator import validate\n\n\n"
            "def test_ok():\n"
            "    rows = [{'age': '30'}]\n"
            "    assert validate(rows, {'age': int}) == rows\n\n\n"
            "def test_bad_type():\n"
            "    rows = [{'age': 'abc'}]\n"
            "    with pytest.raises(ValueError):\n"
            "        validate(rows, {'age': int})\n"
        ),
    ),
    Stage(
        name="transformer",
        files={
            "pipeline/transformer.py": '"""转换器（待实现）"""\n\n\ndef normalize_dates(rows, field):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_transformer.py",
        test_content=(
            "from pipeline.transformer import normalize_dates\n\n\n"
            "def test_iso_passthrough():\n"
            "    rows = [{'d': '2024-01-05'}]\n"
            "    assert normalize_dates(rows, 'd')[0]['d'] == '2024-01-05'\n\n\n"
            "def test_us_format():\n"
            "    rows = [{'d': '01/05/2024'}]\n"
            "    assert normalize_dates(rows, 'd')[0]['d'] == '2024-01-05'\n"
        ),
    ),
    Stage(
        name="aggregator",
        files={
            "pipeline/aggregator.py": '"""聚合器（待实现）"""\n\n\ndef group_sum(rows, key, value_field):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_aggregator.py",
        test_content=(
            "from pipeline.aggregator import group_sum\n\n\n"
            "def test_sum():\n"
            "    rows = [{'k': 'a', 'v': '1'}, {'k': 'a', 'v': '2'}, {'k': 'b', 'v': '3'}]\n"
            "    assert group_sum(rows, 'k', 'v') == {'a': 3.0, 'b': 3.0}\n"
        ),
    ),
    Stage(
        name="exporter",
        files={
            "pipeline/exporter.py": '"""导出器（待实现）"""\n\n\ndef to_csv(rows, path):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_exporter.py",
        test_content=(
            "import csv\n\n"
            "from pipeline.exporter import to_csv\n\n\n"
            "def test_write(tmp_path):\n"
            "    p = tmp_path / 'out.csv'\n"
            "    to_csv([{'a': '1', 'b': '2'}], str(p))\n"
            "    with open(p, newline='') as fh:\n"
            "        rows = list(csv.DictReader(fh))\n"
            "    assert rows[0]['a'] == '1'\n"
        ),
    ),
    Stage(
        name="cli",
        files={
            "pipeline/cli.py": '"""CLI 入口（待实现）"""\n\n\ndef main(argv):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_cli.py",
        test_content=(
            "from pipeline.cli import main\n\n\n"
            "def test_main_exists():\n"
            "    assert callable(main)\n\n\n"
            "def test_help(capsys):\n"
            "    main(['--help'])\n"
            "    out = capsys.readouterr().out\n"
            "    assert 'usage' in out.lower()\n"
        ),
    ),
]

_L1_PROMPT = """实现一个 CSV 数据管道，包含 6 个模块（按依赖顺序）：

1. `pipeline/parser.py` — `parse_csv(text) -> list[dict]`：解析 CSV 文本，跳过空行
2. `pipeline/validator.py` — `validate(rows, schema) -> rows`：按 schema 校验类型，不合法抛 ValueError
3. `pipeline/transformer.py` — `normalize_dates(rows, field) -> rows`：日期统一成 YYYY-MM-DD（支持 MM/DD/YYYY）
4. `pipeline/aggregator.py` — `group_sum(rows, key, value_field) -> dict`：按 key 分组求和（数值转 float）
5. `pipeline/exporter.py` — `to_csv(rows, path) -> None`：写回 CSV 文件
6. `pipeline/cli.py` — `main(argv) -> None`：命令行入口，`--help` 打印 usage

每个模块都有对应的测试文件，**必须全部通过**。
实现顺序建议：先 parser（其它模块依赖它），再 validator，依次往下。
"""


# ── L2: 事件调度器 ────────────────────────────────────────────
_L2_STAGES = [
    Stage(
        name="event",
        files={
            "sched/__init__.py": "",
            "sched/event.py": '"""事件（待实现）"""\n\n\nclass Event:\n    def __init__(self, name, at):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_event.py",
        test_content=(
            "from sched.event import Event\n\n\n"
            "def test_fields():\n"
            "    e = Event('tick', 10)\n"
            "    assert e.name == 'tick'\n"
            "    assert e.at == 10\n"
        ),
    ),
    Stage(
        name="queue",
        files={
            "sched/queue.py": '"""优先队列（待实现）"""\n\n\nclass EventQueue:\n    def push(self, event):\n        raise NotImplementedError\n\n    def pop(self):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_queue.py",
        test_content=(
            "from sched.event import Event\n"
            "from sched.queue import EventQueue\n\n\n"
            "def test_order():\n"
            "    q = EventQueue()\n"
            "    q.push(Event('c', 30))\n"
            "    q.push(Event('a', 10))\n"
            "    q.push(Event('b', 20))\n"
            "    assert q.pop().name == 'a'\n"
            "    assert q.pop().name == 'b'\n"
        ),
    ),
    Stage(
        name="scheduler",
        files={
            "sched/scheduler.py": '"""调度器（待实现）"""\n\n\nclass Scheduler:\n    def __init__(self):\n        raise NotImplementedError\n\n    def at(self, time, fn):\n        raise NotImplementedError\n\n    def run(self, until):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_scheduler.py",
        test_content=(
            "from sched.scheduler import Scheduler\n\n\n"
            "def test_run_in_order():\n"
            "    s = Scheduler()\n"
            "    seen = []\n"
            "    s.at(30, lambda: seen.append('c'))\n"
            "    s.at(10, lambda: seen.append('a'))\n"
            "    s.run(100)\n"
            "    assert seen == ['a', 'c']\n\n\n"
            "def test_until_limit():\n"
            "    s = Scheduler()\n"
            "    seen = []\n"
            "    s.at(200, lambda: seen.append('late'))\n"
            "    s.run(100)\n"
            "    assert seen == []\n"
        ),
    ),
    Stage(
        name="recurring",
        files={
            "sched/recurring.py": '"""周期事件（待实现）"""\n\n\ndef every(scheduler, interval, fn, start=0):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_recurring.py",
        test_content=(
            "from sched.recurring import every\n"
            "from sched.scheduler import Scheduler\n\n\n"
            "def test_repeats():\n"
            "    s = Scheduler()\n"
            "    count = []\n"
            "    every(s, 20, lambda: count.append(1), start=10)\n"
            "    s.run(70)\n"
            "    assert len(count) == 4\n"
        ),
    ),
]

_L2_PROMPT = """实现一个事件调度器，包含 4 个模块（按依赖顺序）：

1. `sched/event.py` — `Event(name, at)`：事件，带 name 和触发时间 at
2. `sched/queue.py` — `EventQueue`：优先队列，`push(event)` / `pop()` 按 at 升序
3. `sched/scheduler.py` — `Scheduler`：`at(time, fn)` 注册，`run(until)` 按时间顺序执行（超过 until 的不执行）
4. `sched/recurring.py` — `every(scheduler, interval, fn, start=0)`：周期事件，从 start 开始每 interval 触发一次

所有测试必须通过。注意模块间的依赖顺序。
"""


# ── L3: 文本编辑器核心 ────────────────────────────────────────
_L3_STAGES = [
    Stage(
        name="buffer",
        files={
            "editor/__init__.py": "",
            "editor/buffer.py": '"""文本缓冲区（待实现）"""\n\n\nclass Buffer:\n    def __init__(self, text=""):\n        raise NotImplementedError\n\n    def insert(self, pos, s):\n        raise NotImplementedError\n\n    def delete(self, start, end):\n        raise NotImplementedError\n\n    def text(self):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_buffer.py",
        test_content=(
            "from editor.buffer import Buffer\n\n\n"
            "def test_insert():\n"
            "    b = Buffer('hello')\n"
            "    b.insert(5, ' world')\n"
            "    assert b.text() == 'hello world'\n\n\n"
            "def test_delete():\n"
            "    b = Buffer('hello world')\n"
            "    b.delete(5, 11)\n"
            "    assert b.text() == 'hello'\n"
        ),
    ),
    Stage(
        name="undo",
        files={
            "editor/undo.py": '"""撤销栈（待实现）"""\n\n\nclass UndoStack:\n    def __init__(self):\n        raise NotImplementedError\n\n    def push(self, op):\n        raise NotImplementedError\n\n    def undo(self):\n        raise NotImplementedError\n\n    def redo(self):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_undo.py",
        test_content=(
            "from editor.undo import UndoStack\n\n\n"
            "def test_undo_redo():\n"
            "    s = UndoStack()\n"
            "    s.push('a')\n"
            "    s.push('b')\n"
            "    assert s.undo() == 'b'\n"
            "    assert s.undo() == 'a'\n"
            "    assert s.redo() == 'a'\n\n\n"
            "def test_undo_empty():\n"
            "    s = UndoStack()\n"
            "    assert s.undo() is None\n"
        ),
    ),
    Stage(
        name="search",
        files={
            "editor/search.py": '"""搜索（待实现）"""\n\n\ndef find_all(text, pattern):\n    raise NotImplementedError\n',
        },
        test_file="tests/test_search.py",
        test_content=(
            "from editor.search import find_all\n\n\n"
            "def test_positions():\n"
            "    assert find_all('ababab', 'ab') == [0, 2, 4]\n\n\n"
            "def test_none():\n"
            "    assert find_all('abc', 'z') == []\n"
        ),
    ),
    Stage(
        name="editor",
        files={
            "editor/core.py": '"""编辑器门面（待实现）"""\n\n\nclass Editor:\n    def __init__(self):\n        raise NotImplementedError\n\n    def insert(self, pos, s):\n        raise NotImplementedError\n\n    def undo(self):\n        raise NotImplementedError\n\n    def text(self):\n        raise NotImplementedError\n',
        },
        test_file="tests/test_editor.py",
        test_content=(
            "from editor.core import Editor\n\n\n"
            "def test_insert_undo():\n"
            "    e = Editor()\n"
            "    e.insert(0, 'abc')\n"
            "    assert e.text() == 'abc'\n"
            "    e.undo()\n"
            "    assert e.text() == ''\n"
        ),
    ),
]

_L3_PROMPT = """实现一个文本编辑器核心，包含 4 个模块（按依赖顺序）：

1. `editor/buffer.py` — `Buffer(text)`：`insert(pos, s)` / `delete(start, end)` / `text()`
2. `editor/undo.py` — `UndoStack`：`push(op)` / `undo()` / `redo()`，空栈返回 None
3. `editor/search.py` — `find_all(text, pattern) -> list[int]`：返回所有出现位置
4. `editor/core.py` — `Editor`：门面类，组合上面三个，`insert` 可撤销

所有测试必须通过。
"""


_SPECS = [
    ("t8-001", "CSV 数据管道（6 阶段）", _L1_PROMPT, _L1_STAGES),
    ("t8-002", "事件调度器（4 阶段）", _L2_PROMPT, _L2_STAGES),
    ("t8-003", "文本编辑器核心（4 阶段）", _L3_PROMPT, _L3_STAGES),
]


def generate() -> list[Task]:
    """生成 3 个长程任务。"""
    tasks = []
    for task_id, title, prompt, stages in _SPECS:
        long_task = LongTask(task_id=task_id, title=title, prompt=prompt, stages=stages)
        tasks.append(long_task.to_task())
    return tasks


def register_template() -> None:
    from experiments.agent_rl.task import _REGISTRY

    _REGISTRY["t8_long_horizon"] = generate
