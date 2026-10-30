"""T6：大型任务（hard-large）。

与 T1–T5 的区别：**不是修一个函数，而是在一个有多达十几个文件的小项目里完成一项跨模块需求**。

设计原则（让模型难以一次做对）：
1. **需求隐含**：题面只给用户视角的现象，不给文件位置与根本原因；
2. **跨文件**：正确解法必须同时改 3 个以上文件；
3. **有噪声**：仓库里存在相似但无关的模块、历史注释、未使用的代码；
4. **验证严格**：测试覆盖了边界与回归，漏一处就失败。

判定仍是纯代码（pytest），因此与前面模板可比。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

_CHECK = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"]

# 每个大任务是一个完整的小项目：files 是全部文件，tests 单独放
_PROJECTS: list[dict] = [
    {
        "name": "inventory_service",
        "prompt": (
            "这是一个小型的库存服务，用户反馈三个问题：\n"
            "1. 同一 SKU 连续入库两次时，第二次的数量被忽略；\n"
            "2. 出库超过库存时不会报错，库存变成负数；\n"
            "3. 按名称搜索时大小写不一致就搜不到。\n"
            "请定位并修复，使 tests/ 下全部通过。代码里有些模块是历史遗留的，不要改动无关部分。"
        ),
        "files": {
            "inventory/__init__.py": "",
            "inventory/models.py": (
                'from dataclasses import dataclass, field\n\n\n'
                "@dataclass\n"
                "class Item:\n"
                "    sku: str\n"
                "    name: str\n"
                "    quantity: int = 0\n\n\n"
                "@dataclass\n"
                "class Warehouse:\n"
                "    items: dict = field(default_factory=dict)\n"
            ),
            "inventory/service.py": (
                "from inventory.models import Item\n\n\n"
                "class InventoryService:\n"
                "    def __init__(self):\n"
                "        self.items = {}\n"
                "        self._audit = []\n\n"
                "    def stock_in(self, sku, name, quantity):\n"
                "        if sku in self.items:\n"
                "            self.items[sku].quantity = quantity\n"
                "        else:\n"
                "            self.items[sku] = Item(sku, name, quantity)\n"
                "        self._audit.append(('in', sku, quantity))\n\n"
                "    def stock_out(self, sku, quantity):\n"
                "        item = self.items[sku]\n"
                "        item.quantity -= quantity\n"
                "        self._audit.append(('out', sku, quantity))\n\n"
                "    def find_by_name(self, keyword):\n"
                "        return [i for i in self.items.values() if keyword in i.name]\n"
            ),
            "inventory/legacy_repo.py": (
                "# 已废弃：旧版仓储实现，仅保留供迁移脚本参考\n\n\n"
                "class LegacyRepository:\n"
                "    def stock_in(self, sku, name, quantity):\n"
                "        pass\n\n"
                "    def stock_out(self, sku, quantity):\n"
                "        pass\n"
            ),
            "inventory/utils.py": (
                "def normalize(text):\n"
                "    return text.strip()\n\n\n"
                "def _unused_helper(values):\n"
                "    return [v for v in values if v]\n"
            ),
            "README.md": "# inventory service\n\n库存管理小服务。`InventoryService` 是主入口。\n",
            "tests/__init__.py": "",
            "tests/test_inventory.py": (
                "import pytest\n\n"
                "from inventory.service import InventoryService\n\n\n"
                "def test_stock_in_accumulates():\n"
                "    svc = InventoryService()\n"
                "    svc.stock_in('A1', 'apple', 5)\n"
                "    svc.stock_in('A1', 'apple', 3)\n"
                "    assert svc.items['A1'].quantity == 8\n\n\n"
                "def test_stock_out_insufficient():\n"
                "    svc = InventoryService()\n"
                "    svc.stock_in('A1', 'apple', 2)\n"
                "    with pytest.raises(ValueError):\n"
                "        svc.stock_out('A1', 5)\n"
                "    assert svc.items['A1'].quantity == 2\n\n\n"
                "def test_stock_out_ok():\n"
                "    svc = InventoryService()\n"
                "    svc.stock_in('A1', 'apple', 5)\n"
                "    svc.stock_out('A1', 2)\n"
                "    assert svc.items['A1'].quantity == 3\n\n\n"
                "def test_find_by_name_case_insensitive():\n"
                "    svc = InventoryService()\n"
                "    svc.stock_in('A1', 'Green Apple', 1)\n"
                "    assert svc.find_by_name('apple')\n"
                "    assert svc.find_by_name('APPLE')\n"
            ),
        },
    },
    {
        "name": "task_queue",
        "prompt": (
            "这是一个任务队列库，用户反馈：\n"
            "1. 同一优先级的任务会乱序执行；\n"
            "2. 重试失败的任务时，重试次数没有上限，导致死循环；\n"
            "3. 取消某个任务后，它仍然会被执行。\n"
            "请修复，使 tests/ 全部通过。"
        ),
        "files": {
            "queue_lib/__init__.py": "",
            "queue_lib/core.py": (
                "import itertools\n\n\n"
                "class Task:\n"
                "    _ids = itertools.count(1)\n\n"
                "    def __init__(self, fn, priority=0, max_retries=2):\n"
                "        self.id = next(Task._ids)\n"
                "        self.fn = fn\n"
                "        self.priority = priority\n"
                "        self.max_retries = max_retries\n"
                "        self.retries = 0\n"
                "        self.cancelled = False\n"
            ),
            "queue_lib/runner.py": (
                "from queue_lib.guard import MAX_ITERATIONS, IterationLimit\n\n\n"
                "class QueueRunner:\n"
                "    def __init__(self):\n"
                "        self._tasks = []\n"
                "        self.executed = []\n\n"
                "    def add(self, task):\n"
                "        self._tasks.append(task)\n\n"
                "    def cancel(self, task_id):\n"
                "        for task in self._tasks:\n"
                "            if task.id == task_id:\n"
                "                task.cancelled = True\n\n"
                "    def run_all(self):\n"
                "        iterations = 0\n"
                "        while self._tasks:\n"
                "            iterations += 1\n"
                "            if iterations > MAX_ITERATIONS:\n"
                "                raise IterationLimit('too many iterations')\n"
                "            task = self._tasks.pop(0)\n"
                "            try:\n"
                "                task.fn()\n"
                "                self.executed.append(task.id)\n"
                "            except Exception:\n"
                "                task.retries += 1\n"
                "                self._tasks.append(task)\n"
            ),
            "queue_lib/legacy.py": "# 旧版队列，已废弃\n\n\ndef run(items):\n    for item in items:\n        item()\n",
            "README.md": "# task queue\n\n`QueueRunner.run_all()` 按优先级执行任务。\n",
            "tests/__init__.py": "",
            "tests/conftest.py": (
                "# 兜底：任何测试都不允许无限循环——超过 2000 次迭代即失败\n"
                "import pytest\n\n\n"
                "@pytest.fixture(autouse=True)\n"
                "def _guard():\n"
                "    yield\n"
                "    _ = None\n"
            ),
            "queue_lib/guard.py": (
                "# 执行次数保护：QueueRunner 每处理一个任务累加，超过上限抛 RuntimeError\n"
                "MAX_ITERATIONS = 1000\n\n\n"
                "class IterationLimit(RuntimeError):\n"
                "    pass\n"
            ),
            "tests/test_queue.py": (
                "import pytest\n\n"
                "from queue_lib.core import Task\n"
                "from queue_lib.runner import QueueRunner\n\n\n"
                "def test_priority_order():\n"
                "    runner = QueueRunner()\n"
                "    order = []\n"
                "    runner.add(Task(lambda: order.append('low'), priority=0))\n"
                "    runner.add(Task(lambda: order.append('high'), priority=10))\n"
                "    runner.add(Task(lambda: order.append('mid'), priority=5))\n"
                "    runner.run_all()\n"
                "    assert order == ['high', 'mid', 'low']\n\n\n"
                "def test_retry_limit():\n"
                "    runner = QueueRunner()\n"
                "    calls = []\n\n"
                "    def always_fail():\n"
                "        calls.append(1)\n"
                "        raise RuntimeError('boom')\n\n"
                "    runner.add(Task(always_fail, max_retries=2))\n"
                "    runner.run_all()\n"
                "    assert len(calls) == 3\n\n\n"
                "def test_cancelled_not_executed():\n"
                "    runner = QueueRunner()\n"
                "    calls = []\n"
                "    task = Task(lambda: calls.append(1))\n"
                "    runner.add(task)\n"
                "    runner.cancel(task.id)\n"
                "    runner.run_all()\n"
                "    assert calls == []\n"
            ),
        },
    },
    {
        "name": "csv_report",
        "prompt": (
            "这是一个报表生成工具，用户反馈：\n"
            "1. 金额汇总有浮点误差（例如 0.1+0.2 显示 0.30000000000000004）；\n"
            "2. 缺少字段的行会导致整个报表崩溃，而不是跳过并记录；\n"
            "3. 日期格式不统一时排序结果错乱。\n"
            "请修复，使 tests/ 全部通过。"
        ),
        "files": {
            "report/__init__.py": "",
            "report/parse.py": (
                "import csv\n\n\n"
                "def read_rows(path):\n"
                "    with open(path, newline='') as fh:\n"
                "        return list(csv.DictReader(fh))\n"
            ),
            "report/aggregate.py": (
                "from report.parse import read_rows\n\n\n"
                "def total_amount(rows):\n"
                "    total = 0.0\n"
                "    for row in rows:\n"
                "        total += float(row['amount'])\n"
                "    return total\n\n\n"
                "def sort_by_date(rows):\n"
                "    return sorted(rows, key=lambda r: r['date'])\n"
            ),
            "report/formatting.py": "def money(value):\n    return f'{value:.2f}'\n",
            "report/legacy_excel.py": "# 已废弃的 Excel 导出，保留占位\n",
            "README.md": "# csv report\n\n`aggregate.total_amount` 汇总金额。\n",
            "tests/__init__.py": "",
            "tests/test_report.py": (
                "import pytest\n\n"
                "from report.aggregate import sort_by_date, total_amount\n\n\n"
                "def test_no_float_drift():\n"
                "    rows = [{'amount': '0.1', 'date': '2024-01-01'}, {'amount': '0.2', 'date': '2024-01-02'}]\n"
                "    assert total_amount(rows) == pytest.approx(0.3)\n"
                "    assert str(total_amount(rows)) == '0.3'\n\n\n"
                "def test_missing_amount_skipped():\n"
                "    rows = [{'amount': '1.5', 'date': '2024-01-01'}, {'date': '2024-01-02'}]\n"
                "    assert total_amount(rows) == pytest.approx(1.5)\n\n\n"
                "def test_date_sorting_mixed_formats():\n"
                "    rows = [\n"
                "        {'date': '01/02/2024', 'amount': '1'},\n"
                "        {'date': '2024-01-05', 'amount': '2'},\n"
                "    ]\n"
                "    ordered = sort_by_date(rows)\n"
                "    assert ordered[0]['amount'] == '1'\n"
            ),
        },
    },
    {
        "name": "http_client",
        "prompt": (
            "这是一个简易 HTTP 客户端封装，用户反馈：\n"
            "1. 请求超时后不会重试，直接抛出异常；\n"
            "2. 4xx 错误也被当成成功返回；\n"
            "3. 重试时没有退避，瞬间打满下游。\n"
            "请修复，使 tests/ 全部通过（测试用假的 transport，不发真实网络请求）。"
        ),
        "files": {
            "client/__init__.py": "",
            "client/transport.py": (
                "class FakeResponse:\n"
                "    def __init__(self, status, body=''):\n"
                "        self.status = status\n"
                "        self.body = body\n\n\n"
                "class FakeTransport:\n"
                "    def __init__(self, responses):\n"
                "        self._responses = list(responses)\n"
                "        self.calls = 0\n\n"
                "    def send(self):\n"
                "        self.calls += 1\n"
                "        if not self._responses:\n"
                "            raise TimeoutError('no more responses')\n"
                "        return self._responses.pop(0)\n"
            ),
            "client/core.py": (
                "class HTTPError(Exception):\n"
                "    pass\n\n\n"
                "class TimeoutError_(Exception):\n"
                "    pass\n\n\n"
                "class Client:\n"
                "    def __init__(self, transport, max_retries=2):\n"
                "        self.transport = transport\n"
                "        self.max_retries = max_retries\n\n"
                "    def get(self):\n"
                "        response = self.transport.send()\n"
                "        return response.body\n"
            ),
            "client/backoff.py": "def sleep(attempt):\n    return 2 ** attempt\n",
            "README.md": "# http client\n\n`Client.get()` 发送一次请求。\n",
            "client/legacy_requests.py": "# 旧版基于 requests 的实现，已废弃\n",
            "tests/__init__.py": "",
            "tests/test_client.py": (
                "import pytest\n\n"
                "from client.core import Client, HTTPError\n"
                "from client.transport import FakeResponse, FakeTransport\n\n\n"
                "def test_retry_on_timeout_then_success():\n"
                "    transport = FakeTransport([FakeResponse(500), FakeResponse(200, 'ok')])\n"
                "    assert Client(transport, max_retries=2).get() == 'ok'\n"
                "    assert transport.calls == 2\n\n\n"
                "def test_4xx_raises():\n"
                "    transport = FakeTransport([FakeResponse(404, 'nope')])\n"
                "    with pytest.raises(HTTPError):\n"
                "        Client(transport, max_retries=0).get()\n\n\n"
                "def test_exhausted_retries_raises():\n"
                "    transport = FakeTransport([FakeResponse(500), FakeResponse(500), FakeResponse(500)])\n"
                "    with pytest.raises(Exception):\n"
                "        Client(transport, max_retries=2).get()\n"
            ),
        },
    },
    {
        "name": "event_bus",
        "prompt": (
            "这是一个事件总线库，用户反馈：\n"
            "1. 一个订阅者抛异常会导致其他订阅者收不到事件；\n"
            "2. 订阅时重复注册会重复触发；\n"
            "3. 取消订阅后仍然会被调用。\n"
            "请修复，使 tests/ 全部通过。"
        ),
        "files": {
            "bus/__init__.py": "",
            "bus/core.py": (
                "class EventBus:\n"
                "    def __init__(self):\n"
                "        self._handlers = {}\n\n"
                "    def subscribe(self, event, handler):\n"
                "        self._handlers.setdefault(event, []).append(handler)\n\n"
                "    def unsubscribe(self, event, handler):\n"
                "        handlers = self._handlers.get(event, [])\n"
                "        if handler in handlers:\n"
                "            handlers.remove(handler)\n\n"
                "    def publish(self, event, payload=None):\n"
                "        for handler in self._handlers.get(event, []):\n"
                "            handler(payload)\n"
            ),
            "bus/middleware.py": "def logging_middleware(event, payload):\n    return None\n",
            "bus/legacy_bus.py": "# 旧版总线，已废弃\n",
            "README.md": "# event bus\n\n`EventBus.publish` 同步派发事件。\n",
            "tests/__init__.py": "",
            "tests/test_bus.py": (
                "import pytest\n\n"
                "from bus.core import EventBus\n\n\n"
                "def test_one_handler_error_does_not_block_others():\n"
                "    bus = EventBus()\n"
                "    seen = []\n\n"
                "    def bad(payload):\n"
                "        raise RuntimeError('boom')\n\n"
                "    bus.subscribe('evt', bad)\n"
                "    bus.subscribe('evt', lambda p: seen.append(p))\n"
                "    bus.publish('evt', 1)\n"
                "    assert seen == [1]\n\n\n"
                "def test_duplicate_subscribe_runs_once():\n"
                "    bus = EventBus()\n"
                "    calls = []\n\n"
                "    def handler(payload):\n"
                "        calls.append(payload)\n\n"
                "    bus.subscribe('evt', handler)\n"
                "    bus.subscribe('evt', handler)\n"
                "    bus.publish('evt', 1)\n"
                "    assert calls == [1]\n\n\n"
                "def test_unsubscribe():\n"
                "    bus = EventBus()\n"
                "    calls = []\n\n"
                "    def handler(payload):\n"
                "        calls.append(payload)\n\n"
                "    bus.subscribe('evt', handler)\n"
                "    bus.unsubscribe('evt', handler)\n"
                "    bus.publish('evt', 1)\n"
                "    assert calls == []\n"
            ),
        },
    },
]


def _build(index: int) -> Task:
    project = _PROJECTS[index]
    return Task(
        task_id=f"t6-{index + 1:03d}",
        template="t6_large",
        difficulty="hard",
        prompt=project["prompt"],
        files=project["files"],
        check_cmd=_CHECK,
        timeout_s=600,
        meta={"case": project["name"], "scale": "large"},
    )


@register("t6_large")
def generate() -> list[Task]:
    """5 个大型任务（每个都是多文件项目）。"""
    return [_build(i) for i in range(len(_PROJECTS))]
