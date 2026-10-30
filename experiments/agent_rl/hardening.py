"""难度倍增器：在不改变任务语义的前提下，给任务叠加"真实工程"的摩擦。

四个手段（可组合）：
1. **隐藏测试**：把测试挪进 `.hidden/` 或改名，题面不再直说测试文件名——
   模型必须先探索仓库才能验证。
2. **干扰文件**：塞入相似命名但无关的模块，迫使模型读内容而不是猜文件名。
3. **噪声历史**：给目标文件加长注释、死代码、未使用的 import，把真正的改动点藏起来。
4. **多缺陷**：把单点 bug 复制成多处同类缺陷（只修一处测试仍失败）。

这些都不改变判定逻辑，只是让"找到要改的地方"变难。
"""

from __future__ import annotations

import random

from experiments.agent_rl.task import Task

_NOISE_IMPORTS = [
    "import os",
    "import sys",
    "import json",
    "import re",
    "import time",
    "import itertools",
    "import functools",
    "from collections import defaultdict, Counter, OrderedDict",
    "from typing import Any, Iterable, Iterator, Sequence",
]

_DEAD_CODE = '''

# --- 历史遗留代码，勿动 ---
def _legacy_helper_a(values):
    # 旧版本遗留，当前未使用
    total = 0
    for value in values:
        total = total + value if value is not None else total
    return total


def _legacy_helper_b(mapping):
    # 旧版本遗留，当前未使用
    result = {}
    for key, value in mapping.items():
        if key is None:
            continue
        result[str(key)] = value
    return result
'''


def hide_tests(task: Task, rng: random.Random) -> Task:
    """把测试文件藏到 .hidden/ 下，并去掉题面对测试名的指向。"""
    new_files = dict(task.files)
    test_files = [name for name in new_files if name.startswith("test_") or "/test_" in name]
    if not test_files:
        return task
    for name in test_files:
        content = new_files.pop(name)
        hidden_name = f".hidden/{name}"
        new_files[hidden_name] = content

    # 判定命令需要指向隐藏位置
    new_check = [
        "python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", ".hidden"
    ]
    prompt = task.prompt.replace("`python -m pytest -q`", "`python -m pytest .hidden -q`")
    prompt += "\n测试在 `.hidden/` 目录下，可以先读它们了解验收标准。"
    return Task(
        task_id=task.task_id,
        template=task.template,
        difficulty=task.difficulty,
        prompt=prompt,
        files=new_files,
        check_cmd=new_check,
        timeout_s=task.timeout_s,
        meta={**task.meta, "hardened": "hide_tests"},
    )


def add_distractors(task: Task, rng: random.Random, count: int = 3) -> Task:
    """加入与目标相似的干扰模块。"""
    new_files = dict(task.files)
    stems = [name.rsplit("/", 1)[-1].rsplit(".", 1)[0] for name in new_files if name.endswith(".py")]
    stem = stems[0] if stems else "mod"
    for i in range(count):
        name = f"{stem}_legacy_{i}.py" if i else f"{stem}_old.py"
        new_files[name] = (
            f'"""旧版本实现，保留仅供参考，不要修改。"""\n\n\n'
            f"def {stem}_v{i}(value):\n"
            f"    # 这是已废弃的实现，行为与当前版本不同\n"
            f"    return value\n"
        )
    return Task(
        task_id=task.task_id,
        template=task.template,
        difficulty=task.difficulty,
        prompt=task.prompt
        + "\n注意：仓库里有若干旧版本文件，它们已废弃，不要修改，也不要参考其行为。",
        files=new_files,
        check_cmd=task.check_cmd,
        timeout_s=task.timeout_s,
        meta={**task.meta, "hardened": task.meta.get("hardened", "") + "+distractors"},
    )


def add_noise(task: Task, rng: random.Random, lines: int = 40) -> Task:
    """给主模块加噪声：无用 import + 死代码 + 长注释。"""
    new_files = dict(task.files)
    targets = [name for name in new_files if name.endswith(".py") and not name.startswith("test_")]
    if not targets:
        return task
    target = targets[0]
    header = "\n".join(rng.sample(_NOISE_IMPORTS, k=min(5, len(_NOISE_IMPORTS))))
    padding = (
        "# 本文件由多个历史提交叠加而成，下面的注释与死代码不要删除，\n"
        "# 有下游脚本依赖它们的行号。\n"
        + "\n".join(f"# TODO(legacy-{i}): 待清理，编号 {i}" for i in range(lines // 4))
    )
    new_files[target] = f"{header}\n\n{padding}\n\n" + new_files[target] + _DEAD_CODE
    return Task(
        task_id=task.task_id,
        template=task.template,
        difficulty=task.difficulty,
        prompt=task.prompt + "\n主文件里有历史遗留的死代码与注释，不要删除它们，只做必要的修改。",
        files=new_files,
        check_cmd=task.check_cmd,
        timeout_s=task.timeout_s,
        meta={**task.meta, "hardened": task.meta.get("hardened", "") + "+noise"},
    )


def harden(task: Task, level: int = 1, seed: int = 0) -> Task:
    """按等级叠加难度。

    level 1：隐藏测试
    level 2：隐藏测试 + 干扰
    level 3：隐藏测试 + 干扰 + 噪声
    """
    rng = random.Random(seed or hash(task.task_id) & 0xFFFF)
    result = task
    if level >= 1:
        result = hide_tests(result, rng)
    if level >= 2:
        result = add_distractors(result, rng)
    if level >= 3:
        result = add_noise(result, rng)
    return result


def harden_all(tasks: list[Task], level: int = 2) -> list[Task]:
    return [harden(task, level=level) for task in tasks]
