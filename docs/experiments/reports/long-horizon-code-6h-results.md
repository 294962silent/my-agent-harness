# 长程代码 Harness 六小时证据包 v1

日期：2026-11-15

状态：完成一轮确定性故障矩阵与公开代码 pilot。该证据包足以回答“机制修复了什么故障”，但不足以宣称模块存在普遍性能收益。

## 结论

1. 7 类确定性故障各重复 20 次，最小 baseline 为 0/140，生产 Harness 接缝为 140/140。
2. Terminal-Bench 2.1 的 fix-git 完成三臂真实模型配对：baseline、enhanced、layered-without-artifact 均官方通过。
3. enhanced 在 fix-git 上使用 10 requests，baseline 为 17，layered-without-artifact 为 12；这是单题描述性结果，不作显著性结论。
4. large-scale-text-editing/enhanced 在 1,080 秒 Agent 上限被取消。官方 verifier 实际执行，5 项检查全部失败，因此该题没有有效 reward。
5. Langfuse 云侧读回到 4 个 trace、61 个 generation observation，与 v3 账本中 61 个真实模型请求逐一对应。
6. 证据支持“故障关闭、上下文证据恢复、状态校验与控制机制有效”；不支持“五层压缩或 artifact 普遍提高 Terminal-Bench 通过率”。

## Phase A：确定性故障注入

实验文件：

- experiments/long_horizon_code/fault_injection.py
- experiments/long_horizon_code/phase_a_report.json
- tests/experiments/test_long_horizon_fault_injection.py

| 场景 | Baseline | Enhanced | Baseline 根因 | Enhanced 通过机制 |
|---|---:|---:|---|---|
| context 证据回读 | 0/20 | 20/20 | 73,468 字符输出被头尾截断，关键第 2,501 行丢失 | ArtifactStore 保存全文，layered ContextManager 提供 handle，artifact search 命中证据 |
| memory revision/scope | 0/20 | 20/20 | 使用 stale 或跨 scope 候选，返回旧值 us-east-1 | MemoryStore 在异步返回后重建 revision，拒绝 mismatch，返回权威值 eu-west-1 |
| checkpoint/resume | 0/20 | 20/20 | 进程重建丢失内存状态，analysis 被执行两次 | ResearchCampaign hash-chain journal reload，只执行剩余 patch |
| 同文件并发修改 | 0/20 | 20/20 | 裸 read-modify-write 固定丢失一次更新 | FileMutationQueue 串行化同路径修改，lost update 为 0 |
| 受控策略晋升 | 0/20 | 20/20 | 只看 train，晋升 holdout 更差的候选 | train+holdout 严格门禁拒绝候选，registry 保持 incumbent，rollback 成功 |
| 宿主评分与沙箱 | 0/20 | 20/20 | 信任候选自报 score=1.0 | 固定镜像 Docker 执行，宿主重算 0.125；越界输出和额外文件被拒绝 |
| 连续仓库修复 | 0/20 | 20/20 | 证据截断、旧区域、恢复重放、并发丢失 update；最终测试失败 | 同轮串联 artifact、revision、journal、mutation queue，compile 和测试均通过 |
| **总计** | **0/140** | **140/140** | 7 类均为 deterministic | 无 blocked 场景 |

宿主评分场景每次运行两个容器，共 40 次固定镜像 Docker 执行。连续仓库场景每轮运行 4 个宿主子进程，共 80 次。

这些结果证明对应机制能修复预置故障。输入与模型动作是脚本化故障，因此不能替代真实模型的自然失败分布。

## Phase B：真实公开代码 pilot

冻结来源：

- Terminal-Bench 2.1 revision：12b7565679ce5006230b820666353673a133db93
- 模型：deepseek-v4.1-flash
- v3 plan SHA-256：f15d6ade822a7dcc582e15d4df9d63d382387395f0f4578c334fccf5e254008b
- 三题任务、镜像、oracle、TLS transport 与 schema 均冻结
- 同一 trial：64 requests、32,000 context、4,096 output、120 秒 shell timeout
- Langfuse required，stage=final；凭据值不进入 plan、日志或 Git

### fix-git 完整三臂

| Arm | compaction | artifact | reward | requests | input tokens | output tokens | cost USD |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline | baseline | off | 1 | 17 | 85,025 | 6,924 | 0.033823 |
| enhanced | layered | on | 1 | 10 | 32,826 | 2,417 | 0.012753 |
| loo_artifact | layered | off | 1 | 12 | 55,642 | 3,593 | 0.021009 |

三臂官方测试均通过。enhanced 相对 baseline 少 7 次请求、52,199 input tokens、4,507 output tokens，客户端计费少 0.021070 美元；相对 layered-without-artifact 少 2 次请求、22,816 input tokens、1,176 output tokens，计费少 0.008256 美元。

这是单一任务、单一 trial 的描述性差异，没有重复采样或显著性检验，不能归因为五层压缩或 artifact 的稳定收益。

### large-scale-text-editing/enhanced

- enhanced 在 1,080 秒 Agent 上限被取消。
- 22 requests 全部完整结算。
- input 88,968、output 41,638、cost 0.076664 美元。
- 官方 verifier 实际执行，5 项测试全部失败。
- 失败直接原因：最终 /app/apply_macros.vim 未形成，后续输入输出等价检查失败。
- 因此 official reward 为 null，不能计作通过，也不能把 verifier 失败误报为基础设施故障。

该负结果说明：长输出任务仍然超出当前预算内 Agent 的交付能力。五层压缩和 artifact 尚未在该任务上形成通过证据。

## Langfuse 远端核验

使用 v2 observations 查询接口读取 session lhpp-20261115-v3：

- trace 数：4
- generation observation 数：61
- 与 v3 ledger 的 61 个模型请求一致
- 每个 trace 的 observation 数：17、10、12、22
- 每 trace input/output/total 与 ledger 完全一致
- 查询只返回 metadata/usage，不含 prompt、回答正文或密钥

证据文件：.git/codex-delivery/long-horizon-code-20261115-v3/campaign/langfuse-readback.json

## 失败与保留

| 批次 | 结果 | 保留原因 |
|---|---|---|
| v2 fix-git/baseline | 1 request、662 input、64 output、0.000276 美元；无 verifier | Langfuse scope ContextVar 跨 asyncio Task reset，导致 shell 被中断 |
| v3 fix-git 三臂 | 全部官方通过 | 完整有效 triplet |
| v3 large-scale/enhanced | 1,080 秒超时；22 requests 完整；verifier 5/5 失败 | 合法 Agent timeout 与真实未交付，不重试、不换题 |
| v3 custom-memory 补跑 | 0 requests；sidecar host 未安装 LIVE_BENCH_API_KEY | 独立 sidecar 配线错误；run ID 标为 consumed，不重试 |

v2 和 v3 的失败记录、usage 与费用均保留。没有将 timeout、criterion failure 或 launcher error 伪装成模型质量结论。

## 成本与规模

- v3 真实模型请求：61
- v3 input tokens：262,461
- v3 output tokens：54,572
- v3 客户端保守计费：0.144249 美元
- v2 失败请求：1；计费 0.000276 美元
- v2 + v3 合计：62 requests；客户端保守计费 0.144525 美元
- Phase A：0 API 请求；40 次评分容器、80 次连续仓库宿主子进程

客户端价格是固定全冷价估算，不是供应商发票。

## 可验证的工程结论

- 大输出不再通过直接截断丢失关键证据；完整内容可持久化并按 handle 回读。
- 语义候选不能越过本地权威事实；stale、scope、revision 不匹配会被拒绝。
- campaign journal 可以跨对象重建恢复阶段状态，且不会将已完成单元再次执行。
- 同文件并发写可以串行化，避免经典 lost update。
- 策略晋升需要独立 holdout 同时严格优于 reference 和 incumbent；失败时保留旧版本并可回滚。
- 宿主评分不信任候选自报分数；固定镜像、受限输出和额外文件检查均生效。
- Langfuse required 观测在真实 v3 请求中完成 flush/close，云侧 observations 与账本对齐。

## 当前不能写

- 不能写“五层压缩使长程代码通过率显著提高”。
- 不能写“artifact projection 使 Terminal-Bench 2.1 从失败变为通过”。
- 不能写“所有长程任务都在 1,080 秒内完成”。
- 不能写“Terminal-Bench 71/89 由某个单一模块带来”。
- 不能写“custom-memory 三臂已完成”。
- 不能写“Langfuse 回读证明上下文质量提升”；它只证明观测与 usage 对齐。
- 不能写“记忆或 RSI 在真实长程代码任务上产生学习收益”。

## 推荐面试表述

“我没有用 Terminal-Bench 总分反推每个模块有效。确定性故障矩阵证明：在证据被截断、旧记忆误召回、进程重建、同文件竞争、train-only 过拟合和不可信自报分数这七类故障下，最小 baseline 为 0/140，生产接缝为 140/140。真实 fix-git 三臂都通过，enhanced 使用 10 次请求而 baseline 使用 17 次，但这是单题描述性结果。large-scale-text-editing 在 1,080 秒超时且 verifier 5 项失败，所以我没有把它写成收益。这个项目重点证明的是长程执行中的可恢复性、证据保真和失败关闭，而不是每个模块都提高了榜单分数。”

## 复现入口

确定性故障：

uv run python -m experiments.long_horizon_code.fault_injection --repeat 20 --output experiments/long_horizon_code/phase_a_report.json

Windows/WSL 测试：

uv run pytest tests/experiments/test_long_horizon_fault_injection.py -q

wsl -d Ubuntu-20.04 -- env PYTHONPATH=src:. /home/mll/.cache/my-agent-bench/venv/bin/python -m pytest -q tests/experiments/test_long_horizon_public_pilot.py tests/experiments/test_benchmark_live_artifacts.py

Langfuse 只读核验入口：

uv run python -m experiments.long_horizon_code.langfuse_readback --session-id lhpp-20261115-v3 --from-start 2026-11-15T00:00:00Z --to-start 2026-11-16T00:00:00Z
