"""循环打断器：检测 agent 陷入重复动作时强制改变策略。

背景：531 条轨迹的实测显示 87.4% 有 ≥3 次重复动作，平均循环长度 4.03，
最严重的样例是 `bash` 连跑 11 次不改代码。

这是长程 Harness 的核心能力之一——**模型自己出不来，Harness 要拉它出来**。

三层干预（由轻到重）：
1. **提示注入**：重复 2 次 → 提示"你刚做过同样的动作"
2. **强制换工具**：重复 3 次 → 禁用刚用过的工具 n 步
3. **预算兜底**：同一动作超过 5 次 → 直接终止该轨迹（continue-until-timeout 的反面）

与 `simple_loop.py` 配合使用。
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

# 工具间的"等效替代"，用于强制换工具时给出建议
_ALTERNATIVES = {
    "bash": ["read", "edit"],
    "read": ["bash", "edit"],
    "ls": ["find", "bash"],
    "find": ["ls", "bash"],
    "grep": ["read", "bash"],
    "edit": ["read", "write"],
    "write": ["read", "edit"],
}


@dataclass
class Intervention:
    """一次干预记录。"""

    step: int
    level: int  # 1=提示 2=换工具 3=终止
    reason: str
    message: str


@dataclass
class LoopBreaker:
    """检测并打断循环。"""

    window: int = 3
    hint_at: int = 2
    switch_at: int = 3
    terminate_at: int = 5
    recent: deque = field(default_factory=lambda: deque(maxlen=100))
    consecutive: Counter = field(default_factory=Counter)
    banned: dict[str, int] = field(default_factory=dict)  # 工具 → 解禁步数
    interventions: list[Intervention] = field(default_factory=list)
    step: int = 0
    terminated: bool = False

    def observe(self, tool: str, signature: str) -> tuple[str, str]:
        """记录一次动作，返回 (level, 给模型的提示)。

        level: "none" | "hint" | "switch" | "terminate"
        """
        self.step += 1
        self.recent.append((tool, signature))

        # 统计连续相同的 signature
        if len(self.recent) >= 2 and self.recent[-2][1] == signature:
            self.consecutive[signature] += 1
        else:
            self.consecutive.clear()
            self.consecutive[signature] = 1

        count = self.consecutive[signature]

        # 周期循环检测：最近 6 步内是否有 ABAB / ABCABC 模式
        # 这类循环比"连续相同"更常见（read→bash→read→bash）
        cycle_len = self._detect_cycle()
        if cycle_len >= 3:
            count = max(count, cycle_len)

        if count >= self.terminate_at:
            if self.terminated:
                # 已终止过：不再重复记录干预
                return "terminate", ""
            self.terminated = True
            msg = f"[HARNESS] 相同动作重复 {count} 次，判定为无效循环，终止本次尝试。"
            self.interventions.append(Intervention(self.step, 3, f"repeat={count}", msg))
            return "terminate", msg

        if count >= self.switch_at:
            banned_tool = tool
            self.banned[banned_tool] = self.step + 3  # 禁 3 步
            alts = _ALTERNATIVES.get(tool, ["read", "bash"])
            msg = (
                f"[HARNESS] 你已经重复执行同一个 `{tool}` 动作 {count} 次，没有产生进展。\n"
                f"该工具暂时不可用。请改用：{', '.join(alts)}。\n"
                f"如果卡住了，先 `read` 相关文件确认当前内容，再决定改什么。"
            )
            self.interventions.append(Intervention(self.step, 2, f"repeat={count}", msg))
            return "switch", msg

        if count >= self.hint_at:
            msg = (
                f"[HARNESS] 注意：你刚刚执行过完全相同的 `{tool}` 动作（第 {count} 次）。\n"
                f"它没有改变任何状态。请换一个思路。"
            )
            self.interventions.append(Intervention(self.step, 1, f"repeat={count}", msg))
            return "hint", msg

        return "none", ""

    def _detect_cycle(self) -> int:
        """检测最近动作的周期重复，返回重复次数（不重复返回 0）。

        例：[read, bash, read, bash, read, bash] → 周期 2，重复 3 次 → 返回 3
        """
        sigs = [s for _, s in self.recent]
        if len(sigs) < 4:
            return 0
        for period in (1, 2, 3):
            need = period * 3
            if len(sigs) < need:
                continue
            tail = sigs[-need:]
            pattern = tail[:period]
            if all(tail[i] == pattern[i % period] for i in range(need)):
                return need // period
        return 0

    def is_banned(self, tool: str) -> bool:
        """该工具当前是否被禁用。"""
        until = self.banned.get(tool)
        if until is None:
            return False
        if self.step >= until:
            del self.banned[tool]
            return False
        return True

    def available_tools(self, tools: list[str]) -> list[str]:
        return [t for t in tools if not self.is_banned(t)]

    def stats(self) -> dict:
        levels = Counter(i.level for i in self.interventions)
        return {
            "total_interventions": len(self.interventions),
            "hints": levels.get(1, 0),
            "forced_switches": levels.get(2, 0),
            "terminations": levels.get(3, 0),
        }


def detect_repeat_pattern(tools: list[str], min_period: int = 1, max_period: int = 6) -> int:
    """检测最长的重复周期模式，返回重复次数。

    与 process_metrics.detect_loop 一致，这里独立实现便于单独测试。
    """
    if not tools:
        return 0
    best = 0
    for period in range(min_period, min(max_period + 1, len(tools) // 2 + 1)):
        repeats = 0
        for start in range(len(tools) - period):
            if tools[start : start + period] == tools[start + period : start + 2 * period]:
                repeats += 1
            else:
                best = max(best, repeats + 1) if repeats else best
                repeats = 0
        if repeats:
            best = max(best, repeats + 1)
    return best
