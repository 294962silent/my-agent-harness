"""产出压力干预：针对长程任务中「只读不写」的探索惯性。

长程任务实测发现（见 LONG_HORIZON.md）：
- 失败的 2 个任务，一个 0 次 write，一个第 21 步才第一次 write
- 成功的任务第 16 步开始连续 write
- **分水岭是「什么时候开始写」，不是「会不会写」**

而循环打断器抓不到这个问题（因为每次 bash 命令都不同）。

三层产出压力：
1. **温和提醒**（6 步无产出）：提示「早点写、靠测试反馈迭代」
2. **强制产出**（10 步无产出）：要求立即写，给出具体策略
3. **最后通牒**（15 步无产出）：只写一个模块也行

关键设计：**只在跨级时提示一次**。每步重复同样的唠叨会被模型忽略，反而浪费上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRODUCTIVE_TOOLS = {"write", "edit"}


@dataclass
class ProdPressure:
    """产出压力检测器。"""

    gentle_at: int = 6
    force_at: int = 10
    last_resort_at: int = 15
    max_steps: int = 30
    steps_since_write: int = 0
    total_steps: int = 0
    writes: int = 0
    interventions: list[dict] = field(default_factory=list)
    _last_level: str = "none"

    def observe(self, tool: str) -> tuple[str, str]:
        """记录一步，返回 (级别, 提示)。级别：none/gentle/force/last_resort。"""
        self.total_steps += 1

        if tool in PRODUCTIVE_TOOLS:
            self.steps_since_write = 0
            self.writes += 1
            self._last_level = "none"
            return "none", ""

        self.steps_since_write += 1
        level = self._level_for(self.steps_since_write)

        # 只在跨级时提示一次
        if level == self._last_level:
            return "none", ""
        self._last_level = level

        if level == "none":
            return "none", ""

        remaining = max(0, self.max_steps - self.total_steps)
        msg = self._message(level, remaining)
        self.interventions.append({"level": level, "step": self.total_steps, "msg": msg[:80]})
        return level, msg

    def _level_for(self, n: int) -> str:
        if n >= self.last_resort_at:
            return "last_resort"
        if n >= self.force_at:
            return "force"
        if n >= self.gentle_at:
            return "gentle"
        return "none"

    def _message(self, level: str, remaining: int) -> str:
        if level == "last_resort":
            return (
                f"[HARNESS] 你已经有 {self.steps_since_write} 步没有任何产出，只剩 {remaining} 步。\n"
                f"现在**只选一个最简单的模块**，用 write 写出完整实现。\n"
                f"哪怕它不完美——先让一个测试通过。"
            )
        if level == "force":
            return (
                f"[HARNESS] 你已经 {self.steps_since_write} 步没有写任何代码了，只剩 {remaining} 步。\n"
                f"请**立即**用 write 输出你目前理解的实现。\n"
                f"策略：挑依赖最底层的那个模块（比如 parser 或 event），一次性写完整。"
            )
        return (
            f"[HARNESS] 提醒：你已经读了 {self.steps_since_write} 步，还没有写任何文件。\n"
            f"长程任务里，**早一点写、然后靠测试反馈迭代**，比一直读更有效。"
        )

    def stats(self) -> dict:
        return {
            "total_steps": self.total_steps,
            "writes": self.writes,
            "steps_since_write": self.steps_since_write,
            "gentle": sum(1 for i in self.interventions if i["level"] == "gentle"),
            "force": sum(1 for i in self.interventions if i["level"] == "force"),
            "last_resort": sum(1 for i in self.interventions if i["level"] == "last_resort"),
            "total": len(self.interventions),
        }
