# 真实模型官方 SWE 单例验证

日期：2026-11-12。requested model：`deepseek-v4.1-flash`，通过已有 OpenCode Go 兼容端点。官方任务 `sympy__sympy-20590`，官方 SWE-bench 5.0.2，CPU 单并行，原评分脚本；任务代码 BSD-3-Clause，许可证同目录保存。

## 实际运行设计

只有 **1 个独立任务、2 次模型 trial**，顺序 on→off。目录 r2 表示修复启动错误后的运行命名，每组实际上是第一次发生模型请求的运行，不是第二个模型重复。r1 的零请求准备失败单列。seed23只用于活动顺序，不是provider采样seed。温度0.7，模型采样与服务器缓存状态未控制。

两个trial均使用新建禁网容器，无宿主测试/金标挂载，先重置到固定base commit；只给问题文本与可见仓库。官方参考解预检先通过，任务/metadata/镜像/报告hash绑定后才允许收费调用。

## 官方结果

- **on**：官方 resolved=True，completed=1，errors=0；FAIL_TO_PASS 1/1、PASS_TO_PASS 21/21。solver 在 16 次请求上限停止，真实终态 `budget_exhausted`。官方评分整体 21.85 秒（测试阶段 4.23 秒）。
- **off**：官方 resolved=True，completed=1，errors=0；FAIL_TO_PASS 1/1、PASS_TO_PASS 21/21。solver 在 16 次请求上限停止，真实终态 `budget_exhausted`。官方评分整体 21.49 秒（测试阶段 3.75 秒）。

预算截停与补丁解决问题是不同维度，不能把budget_exhausted改写为正常end_turn。官方aggregate统计相同所以两文件hash可相同，独立run_id、目录与测试stdout各自保留。

## 用量与费用口径

- on：按provider total−completion得到输入合计 80706 token，输出 4533 token；客户端保守全价结算 $0.029658。请求窗口 94.40 秒包含请求间工具操作，不等同完整任务端到端耗时。
- off：按provider total−completion得到输入合计 110165 token，输出 4902 token；客户端保守全价结算 $0.038939。请求窗口 142.18 秒包含请求间工具操作，不等同完整任务端到端耗时。

总计 **32 次请求、保守账本 $0.068597**，授权上限 $30。按输入 $0.30/M、输出 $1.20/M 且不给缓存折扣计算；不是已核对的服务端账单或实际订阅余额。全部请求usage已结算；cache_read字段各仅15/16覆盖，因此不报告完整缓存命中率。

## 为什么不能宣传技术提升

本次 on 目录没有生成外置产物，Artifact机制**没有激活**。因此这是生产Agent / Runtime / 预算 / 官方评分链路的真实smoke，不是有效的Artifact因果消融。80,706与110,165输入token的差异伴随不同模型轨迹，不能归因为产物外置。两个组都通过，也没有成功率优势证据。

复核还发现协议v1没有为CoreAgent装配artifact_read/search。当前修正协议v2已在两组注册相同受限工具schema，off仅关闭投影，并用长输出往返回归验证；不把v1真实结果冒充v2真实链路验证。协议v1源码保存在protocol-v1/，以便解释历史结果与修复，未篡改原始记录。

## 原始证据

- `ledger.json`：逐请求预留、已归一化usage、purpose与保守结算。
- `on/`、`off/`：原solver run.json、模型生成预测、官方aggregate / instance report、官方测试输出。
- `gold-evidence.json`：实际参考解通过证据；`excluded-pre-agent-run.json`保留零请求失败。
- `manifest.json`：文件SHA256、源码hash和版本边界。源码快照为事后采集并明确标注，不冒充调用前签名。
- 参考解 / 模型评分的完整provenance另见 `../../benchmark-preflight/swe-model-paired.json`。

Terminal-Bench 的原官方fix-git参考解因verifier下载uv失败未通过环境预检，未给它发送模型请求；requests1963的官方gold超时也不计作模型失败。不能把这些环境过滤隐藏成完整benchmark成绩。
