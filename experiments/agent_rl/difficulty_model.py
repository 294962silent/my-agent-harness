"""难度预测的尝试（记录性代码，不要用于任务设计决策）。

结论：静态特征无法可靠预测步数（平均比值 1.00 但标准差 0.53）。
关键反例：t8-010（39 行）只需 3.8 步，t8-001（60 行）需要 48 步，
差异来自「单文件集中实现」vs「六文件分散实现」。

保留原因：
1. 记录尝试过程（见 DIRECTION_1.md 第十五节）
2. 定性判断仍然可用：单文件简单任务 vs 多文件复杂任务
"""

from __future__ import annotations

import sys

sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness")
sys.path.insert(0, "C:/Users/MLL/Desktop/my-agent-harness/src")

from experiments.agent_rl.task import generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401

# 来自实测的标定（t8-009/010/011 三组）
# (单模块实现行数 -> 实测平均步数)
_CALIBRATION = [
    (1, 3.0),  # add: 1 行
    (25, 3.8),  # parse_duration: ~25 行
    (95, 19.0),  # evaluate: ~95 行
]


def _interp_steps(lines: int) -> float:
    """按实现行数插值估算单模块步数（对数线性拟合）。"""
    import math

    if lines <= _CALIBRATION[0][0]:
        return _CALIBRATION[0][1]
    if lines >= _CALIBRATION[-1][0]:
        return _CALIBRATION[-1][1]
    for (x0, y0), (x1, y1) in zip(_CALIBRATION, _CALIBRATION[1:]):
        if x0 <= lines <= x1:
            t = (math.log(max(lines, 1)) - math.log(x0)) / (math.log(x1) - math.log(x0))
            return y0 + t * (y1 - y0)
    return _CALIBRATION[-1][1]


_SOLUTIONS = None


def _load_solutions() -> dict:
    """加载所有参考解法（真实实现量）。"""
    global _SOLUTIONS
    if _SOLUTIONS is not None:
        return _SOLUTIONS
    from experiments.agent_rl.tasks.t8_solutions import SOLUTIONS
    from experiments.agent_rl.tasks.t8b_solutions import T8B_SOLUTIONS
    from experiments.agent_rl.tasks.t8c_solutions import T8C_SOLUTIONS
    from experiments.agent_rl.tasks.t8d_solutions import T8D_SOLUTIONS

    _SOLUTIONS = {**SOLUTIONS, **T8B_SOLUTIONS, **T8C_SOLUTIONS, **T8D_SOLUTIONS}
    return _SOLUTIONS


def analyze_module(files: dict[str, str], test_files: dict[str, str], solution: dict | None = None) -> dict:
    """估算单个模块的复杂度和步数需求。"""
    not_impl = sum(c.count("NotImplementedError") for c in files.values())
    test_funcs = sum(c.count("def test_") for c in test_files.values())
    asserts = sum(c.count("assert ") for c in test_files.values())
    stub_lines = sum(len([line for line in c.splitlines() if line.strip()]) for c in files.values())
    # 优先用参考解法的真实行数（这才是实际工作量）
    if solution:
        est_impl_lines = sum(len([line for line in c.splitlines() if line.strip()]) for c in solution.values())
    else:
        est_impl_lines = max(stub_lines * 5, 1)
    est_steps = _interp_steps(est_impl_lines)

    return {
        "functions": not_impl,
        "tests": test_funcs,
        "asserts": asserts,
        "stub_lines": stub_lines,
        "est_impl_lines": est_impl_lines,
        "est_steps": round(est_steps, 1),
    }


def analyze_task(task) -> dict:
    """分析整个任务的步数需求。"""
    # 按目录分组（每个顶层目录视为一个模块）
    # 按**文件**分组（每个待实现的 .py 文件算一个模块）
    # 之前按目录分组会把 pipeline/parser.py、pipeline/validator.py 等 6 个文件
    # 当成 1 个模块，导致预估严重偏低。
    modules: dict[str, tuple[dict, dict]] = {}
    for name, content in task.files.items():
        is_test = name.startswith("tests/") or "/test" in name
        if not name.endswith(".py") or name.endswith("__init__.py"):
            continue
        key = name if not is_test else f"test::{name}"
        bucket = modules.setdefault(key, ({}, {}))
        bucket[1 if is_test else 0][name] = content

    # 把测试按文件名对应到实现文件（tests/test_parser.py -> pipeline/parser.py）
    def _stem(path: str) -> str:
        base = path.rsplit("/", 1)[-1]
        return base[:-3] if base.endswith(".py") else base

    test_map: dict[str, dict] = {}
    for key, (impl, tests) in list(modules.items()):
        if key.startswith("test::"):
            for test_name, content in tests.items():
                stem = _stem(test_name).replace("test_", "")
                test_map.setdefault(stem, {})[test_name] = content

    normalized: dict[str, tuple[dict, dict]] = {}
    for key, (impl, tests) in modules.items():
        if key.startswith("test::"):
            continue
        stem = _stem(key)
        combined = dict(tests)
        for k, v in test_map.get(stem, {}).items():
            combined[k] = v
        normalized[key] = (impl, combined)
    modules = normalized

    solutions = _load_solutions().get(task.task_id, {})
    per_module = []
    for key, (impl, tests) in modules.items():
        if not impl:
            continue
        # 找到这个文件对应的解法
        sol = {n: c for n, c in solutions.items() if n in impl}
        info = analyze_module(impl, tests, sol or None)
        info["module"] = key
        per_module.append(info)

    total_steps = sum(m["est_steps"] for m in per_module)
    return {
        "task_id": task.task_id,
        "num_modules": len(per_module),
        "total_est_steps": round(total_steps, 1),
        "modules": per_module,
    }


def predict(analysis: dict, budgets: list[int]) -> dict:
    """预测不同预算下的成功档位。"""
    need = analysis["total_est_steps"]
    out = {}
    for budget in budgets:
        ratio = need / budget if budget else float("inf")
        if ratio <= 0.5:
            tier = "易（大概率通过）"
        elif ratio <= 0.9:
            tier = "中（可能通过）"
        elif ratio <= 1.2:
            tier = "难（边界，有 gap）"
        else:
            tier = "极难（大概率失败）"
        out[budget] = {"ratio": round(ratio, 2), "tier": tier}
    return out


# 实测结果（用于验证模型准确度）
_MEASURED = {
    "t8-001": {30: 0 / 6, 50: 4 / 5},
    "t8-002": {50: 4 / 4},
    "t8-003": {30: 2 / 3, 50: 2 / 2},
    "t8-004": {50: 2 / 2},
    "t8-005": {50: 1 / 2},
    "t8-006": {50: 2 / 2},
    "t8-007": {40: 4 / 4},
    "t8-008": {40: 4 / 4},
    "t8-009": {40: 4 / 4},
    "t8-010": {40: 4 / 4},
    "t8-011": {40: 4 / 4},
}


def main() -> int:
    tasks = [t for t in generate_all() if "long_horizon" in t.template or t.task_id.startswith("t8-")]
    budgets = [30, 40, 50]

    print("=== 难度预测 vs 实测 ===\n")
    header = f"{'任务':9s} {'模块':>4s} {'预估步数':>8s} {'预算50':>16s} {'实测(50)':>10s} {'一致':>5s}"
    print(header)
    print("-" * len(header))

    correct = 0
    total = 0
    for task in tasks:
        analysis = analyze_task(task)
        pred = predict(analysis, budgets)
        measured = _MEASURED.get(task.task_id, {})

        m50 = measured.get(50)
        if m50 is None:
            continue
        actual_tier = "易" if m50 >= 0.9 else ("难" if m50 > 0 else "极难")
        pred_tier = pred[50]["tier"].split("（")[0]
        # 中/难 都算边界
        ok = (pred_tier in ("中", "难") and actual_tier in ("难",)) or (
            pred_tier == "易" and actual_tier == "易"
        )
        total += 1
        correct += int(ok)
        print(
            f"{task.task_id:9s} {analysis['num_modules']:>4d} {analysis['total_est_steps']:>8.1f} "
            f"{pred[50]['tier']:>16s} {m50:>9.0%} {'✓' if ok else '✗':>5s}"
        )

    print(f"\n预测准确率: {correct}/{total} = {correct/total:.0%}" if total else "无数据")
    print("\n=== 预算敏感性（t8-001）===")
    t8001 = next(t for t in tasks if t.task_id == "t8-001")
    a = analyze_task(t8001)
    for budget, info in predict(a, [25, 30, 40, 50, 60, 80]).items():
        print(f"  预算 {budget:3d}: 需求/预算 = {info['ratio']:.2f} → {info['tier']}")
    print("  （实测：30 步 0/6，50 步 4/5）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
