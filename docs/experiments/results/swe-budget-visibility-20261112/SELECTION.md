# 静态预算可见性：诊断面板

日期：2026-11-12。本轮任何模型请求前固定。官方任务源仍为 `SWE-bench/swe-bench-tasks@3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，复用已通过gold的原输入/镜像绑定。

- `pytest-dev__pytest-5103`：上一批两臂empty，作为症状案例。
- `sympy__sympy-11400`：上一批两臂empty，作为另一个项目的症状案例。
- `pytest-dev__pytest-5227`：上一批两臂resolved，作为既有成功控制。

这是按已观察症状选择的工程诊断面板，不是未见题或代表性随机样本；旧结果不替代本轮同期对照。不得因新模型结果换题。

每题static budget guidance on/off各一次，两个条件artifact_projection均为on；16请求、4096输出、600秒、温度0.7不变。唯一提示差异是初始system是否包含真实API调用上限信息，整个run不动态改写前缀；不加入编辑策略或强制回读指令。模型前冻结源码、seed23顺序和费用界限，结果仍受采样/缓存随机性影响。
