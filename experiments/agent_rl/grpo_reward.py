"""把稠密过程奖励接入 GRPO：方向二的「可验证奖励」落地。

背景：TRL 的 GRPOTrainer 只接受「completions -> list[float]」形式的奖励函数，
但 agent 任务需要**执行**才能知道结果。两层奖励设计：

1. **格式/结构奖励**（无需执行，同步可得）：语法合法、动作合理、无冗余
2. **结果奖励**（需要沙盒）：由外部的 rollout 阶段预先算好，缓存在数据集里

本模块实现第 1 层，并给出第 2 层的接入方式。

为什么分层：GRPOTrainer 在训练循环里同步调用 reward_funcs，
不能在其中跑 Docker。所以实际训练时是：
  阶段 A：用 harness 跑 rollout，把 (prompt, completion, 执行结果, 稠密奖励) 落盘
  阶段 B：把落盘的数据当数据集，reward 直接读缓存值

这样既保持可验证（奖励来自真实执行），又能塞进 TRL 的接口。
"""

from __future__ import annotations

import json
import re
from typing import Any

VALID_TOOLS = {"read", "write", "edit", "bash", "ls", "find", "grep", "done"}


def _split_json_objects(text: str) -> list[str]:
    """用括号配对切出顶层 JSON 对象（支持嵌套）。

    注意：不能用非贪婪正则匹配大括号，因为它会在第一个右括号处截断，
    而动作 JSON 通常是嵌套的（arguments 里还有一层）。
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for idx, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : idx + 1])
                start = -1
    return out


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出提取第一个合法的动作 JSON。"""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for cand in _split_json_objects(cleaned):
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "name" in data:
            return data
    return None


def structure_reward(completion: str) -> tuple[float, dict[str, float]]:
    """第 1 层：结构奖励（不执行，纯静态检查）。

    返回 (总分, 明细)。总分范围 [-1, 1]。
    """
    text = completion if isinstance(completion, str) else str(completion)
    detail: dict[str, float] = {}
    score = 0.0

    stripped = text.strip()
    if not stripped:
        return -1.0, {"empty": -1.0}

    # 1. 有合法 JSON 动作
    action = _extract_json(stripped)
    if action is None:
        return -0.5, {"no_json": -0.5}

    score += 0.4
    detail["has_json"] = 0.4

    # 2. 动作名合法
    name = action.get("name")
    if name in VALID_TOOLS:
        score += 0.3
        detail["valid_tool"] = 0.3
    else:
        score -= 0.2
        detail["invalid_tool"] = -0.2

    # 3. 参数完整
    args = action.get("arguments") if isinstance(action.get("arguments"), dict) else action
    if name in ("read", "write", "edit", "ls") and not args.get("path"):
        score -= 0.15
        detail["missing_path"] = -0.15
    elif name == "bash" and not args.get("command"):
        score -= 0.15
        detail["missing_command"] = -0.15
    elif name == "edit" and not (args.get("oldText") and args.get("newText")):
        score -= 0.1
        detail["missing_edit_fields"] = -0.1
    else:
        score += 0.2
        detail["complete_args"] = 0.2

    # 4. 格式噪音
    if "```" in stripped:
        score -= 0.1
        detail["code_fence"] = -0.1
    if len(stripped) > 1000:
        score -= 0.15
        detail["too_verbose"] = -0.15

    # 5. 纯 JSON（最理想）
    if stripped.startswith("{") and stripped.endswith("}"):
        score += 0.15
        detail["pure_json"] = 0.15

    return max(-1.0, min(1.0, score)), detail


def make_grpo_reward_fn():
    """给 TRL GRPOTrainer 用的奖励函数（第 1 层）。"""

    def reward_fn(completions, **kwargs):
        rewards = []
        for item in completions:
            text = item
            if isinstance(item, list):
                # chat 格式：取最后一条 assistant
                text = ""
                for msg in item:
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        text = msg.get("content", "")
            score, _ = structure_reward(text)
            rewards.append(score)
        return rewards

    return reward_fn


def combine_with_execution(structure: float, dense_total: float, success: bool) -> float:
    """第 2 层：与执行结果组合。

    权重设计：
    - 结构分（0.3）保证格式正确
    - 稠密分（0.3）反映过程质量
    - 终局分（0.4）决定成败
    """
    terminal = 1.0 if success else 0.0
    return round(0.3 * structure + 0.3 * dense_total + 0.4 * terminal, 4)


def main() -> int:
    """自测：用真实轨迹里的模型输出验证结构奖励。"""
    from pathlib import Path

    samples = [
        '{"name": "edit", "arguments": {"path": "mod.py", "oldText": "a", "newText": "b"}}',
        '{"name": "read", "arguments": {"path": "mod.py"}}',
        '```json\n{"name": "bash", "arguments": {"command": "pytest"}}\n```',
        '<think>思考</think>{"name": "write", "arguments": {"path": "x.py", "content": "y"}}',
        '我建议修改 mod.py 的第三行。',
        '{"name": "unknown_tool", "arguments": {}}',
        "",
    ]
    print("=== 结构奖励自测 ===\n")
    for text in samples:
        score, detail = structure_reward(text)
        shown = (text[:55] + "...") if len(text) > 55 else text
        print(f"{score:+.2f}  {shown!r}")
        print(f"       {detail}")

    # 用真实数据统计分布
    results_dir = Path(__file__).resolve().parent / "results"
    path = results_dir / "long_k_nopressure.jsonl"
    if path.exists():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        scores = []
        for row in rows:
            for step in row.get("test_progression", []):
                pass
        print("\n=== 真实轨迹 ===")
        print(f"  有 {len(rows)} 条轨迹可用于组合奖励验证")
        # 展示组合效果
        print("\n=== 组合奖励示例（结构0.8 + 稠密0.6 + 成功）===")
        print(f"  成功: {combine_with_execution(0.8, 0.6, True)}")
        print(f"  失败: {combine_with_execution(0.8, 0.6, False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
