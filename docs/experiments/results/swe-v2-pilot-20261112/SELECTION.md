# SWE v2 固定小样本清单

日期：2026-11-12。在本轮任何真实模型请求之前冻结；官方任务仓库 `SWE-bench/swe-bench-tasks`，revision `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`。

选择规则：保留已跑过v1的 `sympy__sympy-20590` 作为已知回归任务；另取固定快照中 SymPy 的两个最小数字编号 Lite 任务，以及 pytest 的三个最小数字编号 Lite 任务。没有读取本批模型结果来筛题。这个清单偏向两个CPU项目，且包含已知回归题，不是随机代表性样本。

任务：

- `sympy__sympy-20590`
- `sympy__sympy-11400`
- `sympy__sympy-11870`
- `pytest-dev__pytest-5103`
- `pytest-dev__pytest-5221`
- `pytest-dev__pytest-5227`

每题 on/off 各一次，最多12个trial；环境/gold失败不替换。真实 arm 顺序在代码接线完成、环境预检之后由 seed23冻结，首轮先后平衡；seed不控制供应商采样。只要开始真实模型试验，既有清单与顺序不可覆盖。
