"""T3：多文件重构。

要求模型把某个符号改名 / 抽出模块，并**同步更新所有引用**。
判定：`pytest` 全绿 **且** 全仓库不再出现旧符号名（grep 为空）。

这条“无残留”检查是关键：只改测试能过、但留下孤儿引用的做法会被判失败。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

_CHECK_TEMPLATE = (
    'python -m pytest -q --no-header -p no:cacheprovider && '
    '! grep -rn --include="*.py" "{old}" . | grep -v test_'
)

# (变体名, 旧名, 新名, 文件字典, 测试)
_CASES: list[tuple[str, str, str, dict[str, str], str]] = [
    (
        "rename_helper",
        "calc_total",
        "compute_total",
        {
            "app/__init__.py": "",
            "app/utils.py": "def calc_total(items):\n    return sum(items)\n",
            "app/service.py": "from app.utils import calc_total\n\n\ndef report(items):\n    return {'total': calc_total(items)}\n",
        },
        "from app.service import report\n\n\ndef test_report():\n    assert report([1, 2, 3]) == {'total': 6}\n",
    ),
    (
        "rename_class",
        "OldClient",
        "HttpClient",
        {
            "app/__init__.py": "",
            "app/client.py": "class OldClient:\n    def __init__(self, base):\n        self.base = base\n\n    def url(self, path):\n        return self.base + path\n",
            "app/api.py": "from app.client import OldClient\n\n\ndef make_client(base):\n    return OldClient(base)\n",
        },
        "from app.api import make_client\n\n\ndef test_url():\n    assert make_client('http://x').url('/a') == 'http://x/a'\n",
    ),
    (
        "rename_const",
        "DEFAULT_TIMEOUT",
        "REQUEST_TIMEOUT",
        {
            "app/__init__.py": "",
            "app/config.py": "DEFAULT_TIMEOUT = 30\n",
            "app/net.py": "from app.config import DEFAULT_TIMEOUT\n\n\ndef timeout():\n    return DEFAULT_TIMEOUT\n",
        },
        "from app.net import timeout\n\n\ndef test_timeout():\n    assert timeout() == 30\n",
    ),
    (
        "rename_module_function",
        "parse_input",
        "parse_payload",
        {
            "app/__init__.py": "",
            "app/parse.py": "def parse_input(raw):\n    return raw.strip()\n",
            "app/handler.py": "from app.parse import parse_input\n\n\ndef handle(raw):\n    return parse_input(raw)\n",
        },
        "from app.handler import handle\n\n\ndef test_handle():\n    assert handle('  x  ') == 'x'\n",
    ),
    (
        "rename_method",
        "get_value",
        "read_value",
        {
            "app/__init__.py": "",
            "app/store.py": "class Store:\n    def __init__(self, data):\n        self.data = data\n\n    def get_value(self, key):\n        return self.data.get(key)\n",
            "app/view.py": "from app.store import Store\n\n\ndef render(store, key):\n    return str(store.get_value(key))\n",
        },
        "from app.store import Store\nfrom app.view import render\n\n\ndef test_render():\n    assert render(Store({'a': 1}), 'a') == '1'\n",
    ),
    (
        "rename_private",
        "_normalize",
        "_canonicalize",
        {
            "app/__init__.py": "",
            "app/text.py": "def _normalize(text):\n    return text.lower().strip()\n\n\ndef clean(text):\n    return _normalize(text)\n",
        },
        "from app.text import clean\n\n\ndef test_clean():\n    assert clean('  AB ') == 'ab'\n",
    ),
    (
        "extract_module",
        "inline_logic",
        "moved_logic",
        {
            "app/__init__.py": "",
            "app/main.py": "def inline_logic(x):\n    return x * 3\n\n\ndef run(x):\n    return inline_logic(x)\n",
        },
        "from app.main import run\n\n\ndef test_run():\n    assert run(4) == 12\n",
    ),
    (
        "rename_package_symbol",
        "helper_twice",
        "support_twice",
        {
            "app/__init__.py": "",
            "app/helpers/__init__.py": "def helper_twice(x):\n    return x * 2\n",
            "app/core.py": "from app.helpers import helper_twice\n\n\ndef quad(x):\n    return helper_twice(helper_twice(x))\n",
        },
        "from app.core import quad\n\n\ndef test_quad():\n    assert quad(3) == 12\n",
    ),
    (
        "rename_dataclass_field",
        "user_name",
        "display_name",
        {
            "app/__init__.py": "",
            "app/model.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass User:\n    user_name: str\n",
            "app/serializer.py": "from app.model import User\n\n\ndef to_dict(user):\n    return {'name': user.user_name}\n",
        },
        "from app.model import User\nfrom app.serializer import to_dict\n\n\ndef test_serialize():\n    assert to_dict(User('ada')) == {'name': 'ada'}\n",
    ),
    (
        "rename_batch",
        "process_item",
        "handle_item",
        {
            "app/__init__.py": "",
            "app/worker.py": "def process_item(x):\n    return x + 1\n\n\ndef process_all(items):\n    return [process_item(i) for i in items]\n",
            "app/queue.py": "from app.worker import process_item\n\n\ndef enqueue(x):\n    return process_item(x)\n",
        },
        "from app.queue import enqueue\nfrom app.worker import process_all\n\n\ndef test_both():\n    assert enqueue(1) == 2\n    assert process_all([1, 2]) == [2, 3]\n",
    ),
    (
        "rename_exception",
        "CustomError",
        "DomainError",
        {
            "app/__init__.py": "",
            "app/errors.py": "class CustomError(Exception):\n    pass\n",
            "app/risky.py": "from app.errors import CustomError\n\n\ndef boom():\n    raise CustomError('x')\n",
        },
        "import pytest\nfrom app.risky import boom\nfrom app.errors import DomainError\n\n\ndef test_raises():\n    with pytest.raises(DomainError):\n        boom()\n",
    ),
    (
        "rename_enum",
        "Color",
        "Palette",
        {
            "app/__init__.py": "",
            "app/paint.py": "from enum import Enum\n\n\nclass Color(Enum):\n    RED = 'r'\n    BLUE = 'b'\n",
            "app/brush.py": "from app.paint import Color\n\n\ndef default_color():\n    return Color.RED\n",
        },
        "from app.brush import default_color\nfrom app.paint import Palette\n\n\ndef test_default():\n    assert default_color() == Palette.RED\n",
    ),
    (
        "rename_kwarg",
        "max_retries",
        "retry_limit",
        {
            "app/__init__.py": "",
            "app/net.py": "def fetch(url, max_retries=3):\n    return (url, max_retries)\n",
            "app/task.py": "from app.net import fetch\n\n\ndef run(url):\n    return fetch(url, max_retries=1)\n",
        },
        "from app.task import run\n\n\ndef test_run():\n    assert run('u') == ('u', 1)\n",
    ),
    (
        "rename_protocol",
        "Shape",
        "Figure",
        {
            "app/__init__.py": "",
            "app/shapes.py": "from typing import Protocol\n\n\nclass Shape(Protocol):\n    def area(self) -> float: ...\n",
            "app/report.py": "from app.shapes import Shape\n\n\ndef total_area(shapes: list[Shape]) -> float:\n    return sum(s.area() for s in shapes)\n",
        },
        "from app.report import total_area\n\n\nclass Sq:\n    def area(self):\n        return 4.0\n\n\ndef test_total():\n    assert total_area([Sq()]) == 4.0\n",
    ),
    (
        "rename_config_key",
        "api_url",
        "service_url",
        {
            "app/__init__.py": "",
            "app/config.py": "DEFAULTS = {'api_url': 'http://localhost'}\n",
            "app/client.py": "from app.config import DEFAULTS\n\n\ndef base_url():\n    return DEFAULTS['api_url']\n",
        },
        "from app.client import base_url\n\n\ndef test_base():\n    assert base_url() == 'http://localhost'\n",
    ),
    (
        "split_module",
        "monolith_run",
        "pipeline_run",
        {
            "app/__init__.py": "",
            "app/mono.py": "def monolith_run(x):\n    return x + 1\n\n\ndef helper(y):\n    return monolith_run(y) * 2\n",
        },
        "from app.mono import helper, pipeline_run\n\n\ndef test_both():\n    assert helper(1) == 4\n    assert pipeline_run(1) == 2\n",
    ),
    (
        "rename_base_class",
        "BaseHandler",
        "HandlerBase",
        {
            "app/__init__.py": "",
            "app/base.py": "class BaseHandler:\n    def handle(self, x):\n        return x\n",
            "app/impl.py": "from app.base import BaseHandler\n\n\nclass Echo(BaseHandler):\n    pass\n",
        },
        "from app.impl import Echo\n\n\ndef test_echo():\n    assert Echo().handle('x') == 'x'\n",
    ),
    (
        "rename_constant_pool",
        "MODE_FAST",
        "MODE_TURBO",
        {
            "app/__init__.py": "",
            "app/modes.py": "MODE_FAST = 'fast'\nMODE_SAFE = 'safe'\n",
            "app/engine.py": "from app.modes import MODE_FAST\n\n\ndef default_mode():\n    return MODE_FAST\n",
        },
        "from app.engine import default_mode\nfrom app.modes import MODE_TURBO\n\n\ndef test_mode():\n    assert default_mode() == MODE_TURBO\n",
    ),
    (
        "rename_decorator",
        "timed",
        "measured",
        {
            "app/__init__.py": "",
            "app/deco.py": "import functools\n\n\ndef timed(fn):\n    @functools.wraps(fn)\n    def wrapper(*a, **k):\n        return fn(*a, **k)\n    return wrapper\n",
            "app/task.py": "from app.deco import timed\n\n\n@timed\ndef work():\n    return 7\n",
        },
        "from app.task import work\n\n\ndef test_work():\n    assert work() == 7\n",
    ),
    (
        "rename_interface_param",
        "ctx",
        "context",
        {
            "app/__init__.py": "",
            "app/plugin.py": "def run_plugin(ctx, x):\n    return ctx + x\n",
            "app/host.py": "from app.plugin import run_plugin\n\n\ndef host(x):\n    return run_plugin(1, x)\n",
        },
        "from app.host import host\n\n\ndef test_host():\n    assert host(2) == 3\n",
    ),
]


def _build(index: int) -> Task:
    name, old, new, files, tests = _CASES[index]
    difficulty = "easy" if index < 10 else "hard"
    payload = dict(files)
    payload["test_app.py"] = tests
    payload["README.md"] = (
        f"# 任务\n\n把符号 `{old}` 重命名为 `{new}`，并更新所有引用。\n\n"
        "要求：`python -m pytest -q` 全部通过，且仓库中不能再出现旧名字。\n"
    )
    return Task(
        task_id=f"t3-{index + 1:03d}",
        template="t3_refactor",
        difficulty=difficulty,
        prompt=(
            f"把代码里的 `{old}` 全部重命名为 `{new}`，包括定义与所有引用。\n"
            "验证方式：`python -m pytest -q` 通过，且 grep 不到旧的符号名。\n不要修改测试文件。"
        ),
        files=payload,
        check_cmd=["sh", "-lc", _CHECK_TEMPLATE.format(old=old)],
        meta={"case": name, "old": old, "new": new},
    )


@register("t3_refactor")
def generate() -> list[Task]:
    """20 个重构任务：10 easy + 10 hard。"""
    return [_build(i) for i in range(len(_CASES))]
