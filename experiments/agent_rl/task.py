"""任务定义与生成器框架。

一个任务由四部分组成：
1. **初始 workspace**：模型看到的文件内容（含 bug 或待补全的骨架）；
2. **题面**：给模型的自然语言描述；
3. **判定函数**：纯代码，输入 workspace，返回 (成功与否, 细节)；
4. **元信息**：模板、难度、变体编号。

判定必须是**确定性**的（跑测试 / 比对文件 / 检查退出码），不调用模型——这是 RLVR 的前提。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from experiments.agent_rl.sandbox import Sandbox, SandboxResult

Difficulty = Literal["easy", "hard"]

Templates = Literal["t1_bugfix", "t2_feature", "t3_refactor", "t4_transform", "t5_cli"]


@dataclass
class Task:
    """一个可执行、可判定的任务。"""

    task_id: str
    template: Templates
    difficulty: Difficulty
    prompt: str
    files: dict[str, str]
    """初始 workspace：相对路径 -> 内容。"""
    check_cmd: list[str]
    """判定命令（在沙盒内执行，退出码 0 视为通过）。"""
    timeout_s: int = 180
    meta: dict = field(default_factory=dict)

    # ── 落盘与判定 ────────────────────────────────────────────────────────

    def materialize(self, root: Path) -> Path:
        """把初始文件写到 root 下。"""
        root.mkdir(parents=True, exist_ok=True)
        for rel, content in self.files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            # shell 脚本必须用 LF：CRLF 会让 test/sed 等命令带上多余的 \r
            if rel.endswith((".sh", ".py")) or rel in {"check.sh"}:
                content = content.replace("\r\n", "\n")
            path.write_text(content, encoding="utf-8", newline="\n" if rel.endswith(".sh") else None)
        return root

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "template": self.template,
            "difficulty": self.difficulty,
            "prompt": self.prompt,
            "files": self.files,
            "check_cmd": self.check_cmd,
            "timeout_s": self.timeout_s,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            task_id=data["task_id"],
            template=data["template"],
            difficulty=data["difficulty"],
            prompt=data["prompt"],
            files=data["files"],
            check_cmd=data["check_cmd"],
            timeout_s=data.get("timeout_s", 180),
            meta=data.get("meta", {}),
        )

    def check(self, workspace: Path) -> SandboxResult:
        """用沙盒判定当前 workspace 是否达标。"""
        with Sandbox(workspace, task_id=self.task_id, timeout_s=self.timeout_s) as box:
            return box.run(self.check_cmd)


def check_in_sandbox(task: Task, workspace: Path) -> SandboxResult:
    """判定入口：把 workspace 交给沙盒跑 check_cmd。"""
    return task.check(workspace)


# ── 生成器注册表 ──────────────────────────────────────────────────────────────

Generator = Callable[[], list[Task]]
_REGISTRY: dict[str, Generator] = {}


def register(name: str) -> Callable[[Generator], Generator]:
    """把模板生成器注册进全局表。"""

    def deco(fn: Generator) -> Generator:
        _REGISTRY[name] = fn
        return fn

    return deco


def all_templates() -> list[str]:
    return sorted(_REGISTRY)


def generate_all() -> list[Task]:
    """生成全部任务（按模板名排序，任务 id 稳定）。"""
    tasks: list[Task] = []
    for name in all_templates():
        tasks.extend(_REGISTRY[name]())
    return tasks


def save_tasks(tasks: list[Task], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for task in tasks:
            fh.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")


def load_tasks(path: Path) -> list[Task]:
    tasks: list[Task] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                tasks.append(Task.from_dict(json.loads(line)))
    return tasks
