"""故障注入场景：构造真实崩溃/中断后可能出现的「脏转录本」。

每个场景只描述「历史长什么样」，由 runner 写入 Session 后再发起一次新请求。
四类故障对应运行时自愈要解决的四种损坏形态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    """一条要写入会话历史的消息。"""

    role: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _call(call_id: str, name: str = "read", path: str = "a.txt") -> dict[str, Any]:
    return {"id": call_id, "name": name, "args": {"path": path}}


@dataclass
class Scenario:
    name: str
    family: str
    turns: list[Turn]
    description: str


def _interrupted(index: int) -> Scenario:
    """断头调用：assistant 声明了两个 tool_call，只回来了一个结果（进程被杀）。"""
    return Scenario(
        name=f"interrupted-{index:02d}",
        family="断头调用",
        turns=[
            Turn("user", f"读取 a.txt 和 b.txt（case {index}）"),
            Turn("assistant", "", {"tool_calls": [_call(f"c{index}a"), _call(f"c{index}b")]}),
            Turn("tool", "contents of a.txt", {"tool_call_id": f"c{index}a"}),
        ],
        description="assistant 发起两个调用，只有第一个有结果",
    )


def _orphan(index: int) -> Scenario:
    """孤儿结果：存在一条找不到来源的 tool 消息（历史被裁剪/合并过）。"""
    return Scenario(
        name=f"orphan-{index:02d}",
        family="孤儿结果",
        turns=[
            Turn("user", f"看一下目录（case {index}）"),
            Turn("assistant", "", {"tool_calls": [_call(f"o{index}")]}),
            Turn("tool", "contents of a.txt", {"tool_call_id": f"o{index}"}),
            Turn("tool", "orphan result", {"tool_call_id": f"ghost{index}"}),
        ],
        description="尾部多出一条无主的 tool 消息",
    )


def _empty_failed(index: int) -> Scenario:
    """空失败轮：一次被中断的 assistant 轮，既无正文也无工具调用。"""
    return Scenario(
        name=f"empty-{index:02d}",
        family="空失败轮",
        turns=[
            Turn("user", f"解释这段代码（case {index}）"),
            Turn("assistant", "   "),
            Turn("user", f"继续（case {index}）"),
        ],
        description="历史中残留一条空白 assistant 消息",
    )


def _mixed(index: int) -> Scenario:
    """混排：断头调用 + 空失败轮 + 孤儿结果同时出现。"""
    return Scenario(
        name=f"mixed-{index:02d}",
        family="混排",
        turns=[
            Turn("user", f"重构模块（case {index}）"),
            Turn("assistant", "", {"tool_calls": [_call(f"m{index}a"), _call(f"m{index}b")]}),
            Turn("tool", "contents of a.txt", {"tool_call_id": f"m{index}a"}),
            Turn("assistant", ""),
            Turn("tool", "orphan result", {"tool_call_id": f"lost{index}"}),
            Turn("user", f"继续（case {index}）"),
        ],
        description="同时含断头、空轮、孤儿结果",
    )


def _clean(index: int) -> Scenario:
    """对照组：完全合法的多轮工具调用历史（自愈不应改动任何一个字节）。"""
    return Scenario(
        name=f"clean-{index:02d}",
        family="干净历史（对照）",
        turns=[
            Turn("user", f"读取 a.txt（case {index}）"),
            Turn("assistant", "", {"tool_calls": [_call(f"k{index}")]}),
            Turn("tool", "alpha", {"tool_call_id": f"k{index}"}),
            Turn("assistant", "已经读到内容，继续下一步。"),
            Turn("user", f"再确认一次（case {index}）"),
        ],
        description="合法转录本，用于检验自愈是否误伤",
    )


BUILDERS = (_interrupted, _orphan, _empty_failed, _mixed)


def build_scenarios(per_family: int = 8) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for builder in BUILDERS:
        scenarios.extend(builder(i) for i in range(1, per_family + 1))
    scenarios.extend(_clean(i) for i in range(1, per_family + 1))
    return scenarios
