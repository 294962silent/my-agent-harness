# 长程代码 Harness 确定性故障注入实验 Phase A v1

日期：2026-11-15。

状态：**完成**。7 类故障各重复 20 次；最小 baseline 为 0/140，完整 Harness 为 140/140。这里证明的是具体故障修复能力，不冒充真实模型长任务通过率。

## 实验目的

回答四个问题：

1. 每个机制具体解决什么失败。
2. baseline 在什么可观测条件下失败。
3. 增强版为什么能够通过。
4. 结果能否稳定复现，而不是依赖一次模型采样。

实验使用真实生产组件、真实文件系统、真实 ResearchCampaign journal、真实文件锁，以及固定镜像 Docker 评分器。故障输入和模型动作由脚本确定性生成，目的是隔离 Harness 行为。

## Baseline 与 Enhanced

- Baseline：省略目标机制的最小实现。例如直接截断大输出、信任未经范围/版本校验的候选、进程重建后只保留内存状态、并行裸写文件、仅按 train 分数晋升、信任候选自报分数。
- Enhanced：使用对应的生产接缝，包括 ArtifactStore、layered ContextManager、MemoryStore、ResearchCampaign、FileMutationQueue、PolicyRegistry、ResearchTask 和 NumericPredictionEvaluator。

两类实现使用相同输入、相同临时工作区约束和相同成功判据。

## 总体结果

| 场景 | Baseline | Enhanced | 关键结果 |
|---|---:|---:|---|
| 大输出与 artifact 回读 | 0/20 | 20/20 | 73,468 字符输出中的关键证据在 baseline 被裁掉；enhanced 保存 artifact 并检索到第 2,501 行 |
| 记忆 revision 与 scope | 0/20 | 20/20 | baseline 使用旧值 us-east-1；enhanced 拒绝 stale 与跨 scope 候选并回到权威值 eu-west-1 |
| checkpoint/resume | 0/20 | 20/20 | baseline 进程重建后重复 analysis；enhanced 从真实 campaign journal 只执行剩余 patch |
| 同文件并发修改 | 0/20 | 20/20 | baseline 固定丢失 1 次更新；enhanced 的 FileMutationQueue 将 lost update 降为 0 |
| 受控策略晋升 | 0/20 | 20/20 | 候选 train 提升 0.26，但 holdout 比 incumbent 低 0.07；enhanced 拒绝晋升并成功回滚 |
| 宿主评分与 Docker 边界 | 0/20 | 20/20 | 候选自报 1.0，宿主重算 0.125；越界写和额外工作区文件被拒绝 |
| 连续仓库修复 | 0/20 | 20/20 | 同一任务串联证据回读、记忆校验、恢复续跑、并发写和最终测试；每轮运行 4 个宿主子进程 |
| **总计** | **0/140** | **140/140** | 7 个场景均为 deterministic，且无残留评测容器 |

## 各场景细节

### 1. 五层压缩与 artifact 回读

构造 73,468 字符的工具输出，关键修复指令位于中段。Baseline 仅保留头尾 900 字符，因此模型视图不包含关键证据。Enhanced 使用真实 ArtifactStore 与 layered ContextManager，将完整正文保存为不可变 artifact，向发送视图提供 handle，并通过 artifact search 命中关键行。

通过判据不是“出现了 handle”，而是最终上下文重新包含关键修复证据，同时 canonical Session 正文仍保持原文。

### 2. 长期记忆 revision 与 scope

同一 identity 先写入 us-east-1，后更新为 eu-west-1。语义索引被故意注入一个 stale revision 候选和一个跨 workspace scope 候选。

Baseline 直接采用候选文本，得到旧区域。Enhanced 调用真实 MemoryStore.aretrieve，在异步返回后重建当前 revision，并拒绝 scope、identity 或 revision 不匹配的候选，最终返回权威事实 eu-west-1。

该实验证明的是记忆候选不能覆盖本地权威事实；不证明向量检索比词法检索有质量优势。

### 3. checkpoint 与进程恢复

使用真实 ResearchTask 和 ResearchCampaign 创建两阶段工作单元：analysis、patch。analysis 完成后模拟进程重建。

Baseline 仅保留内存完成集合，重建后丢失状态，工作日志变成 analysis、analysis、patch。Enhanced 重新加载 hash-chain journal，只执行尚未提交的 patch，重复执行单元为 0。

该实验验证了 journal 接缝与恢复算法，不声称外部副作用 exactly-once。

### 4. 并发同文件修改

两个协程同时读取同一文件旧快照，然后分别写入 left 和 right。Baseline 没有路径锁，固定产生一次 lost update。Enhanced 使用生产 FileMutationQueue.acquire，使同一路径串行，最终同时保留 left 和 right。

该场景只串行冲突路径，不牺牲不同文件之间的并发能力；不同文件并发由既有定向测试覆盖。

### 5. 受控策略晋升与回滚

候选在 train 上比 incumbent 高 0.26，但在独立 holdout 上比 baseline 低 0.02、比 incumbent 低 0.07。

Baseline 只看 train，错误晋升。Enhanced 要求同时满足 train 与 holdout 严格增益，拒绝候选，保持原 incumbent 指针不变，并通过真实 PolicyRegistry 回滚到 previous version。

该实验证明晋升门禁和版本回滚有效，不证明策略搜索本身能产生学习收益。历史真实策略实验仍没有观察到可识别收益。

### 6. 宿主评分与 Docker 沙箱

使用固定镜像 ID：

sha256:896edfbcbf0c29ad15681617a8886cb9871b7f24c5fde4606114c8ee6bfe4c70

每次重复运行两个容器，共 40 次容器执行：

- 合法候选只能写 predictions.csv，容器退出后被宿主接受。
- 候选自报 score=1.0，宿主从私有标签重算为 0.125，自报值未成为最终分数。
- 候选尝试写 /output/escaped.txt，容器内写入失败，输出目录仍只有 predictions.csv。
- 宿主工作区预先放入额外文件时，evaluator 在执行前拒绝。

该实验使用固定网络、只读、资源限制和非 root 的生产 evaluator 边界；没有放宽生产安全策略。

### 7. 连续仓库修复

将前六类机制放入同一临时 Python 仓库的连续四阶段任务：

1. 从 73,000 级字符诊断输出中定位并修改 retry_limit=7。
2. 根据新 revision 的区域约束修改配置。
3. 模拟进程重建，从 ResearchCampaign journal 继续剩余阶段。
4. 两个协程并发修改同一文件，最终运行宿主 compile 和固定测试。

Baseline 在首个阶段丢失关键证据，同时使用旧区域、恢复时重放已完成工作、并发写入丢失更新。它的 compile 虽然返回 0，但固定测试返回 1。

Enhanced 通过 artifact 回读恢复证据、MemoryStore 校验 revision/scope、journal 跳过已完成单元、FileMutationQueue 串行冲突写入，最终 compile 和固定测试都返回 0。

每轮调用 4 个宿主子进程，20 轮共 80 次。该场景证明多种机制在同一脚本化代码任务中能够组合工作，仍不代表真实模型成功率。

## 复现

仓库根执行：

uv run python -m experiments.long_horizon_code.fault_injection --repeat 20 --output experiments/long_horizon_code/phase_a_report.json

定向验证：

uv run pytest tests/experiments/test_long_horizon_fault_injection.py -q

uv run ruff check experiments/long_horizon_code tests/experiments/test_long_horizon_fault_injection.py

实际结果：8 项测试通过，Ruff 通过，完整 CLI 退出码 0。

## 可以写进简历的结论

“针对 context 证据丢失、stale/cross-scope memory、进程恢复重做、并发 lost update、train-only 错误晋升、候选自报分数和连续仓库修复七类长程代码故障，构建 20 次重复的确定性故障注入矩阵；最小 baseline 为 0/140，生产 Harness 接缝为 140/140。宿主评分场景真实运行 40 次固定镜像 Docker 容器，连续仓库场景运行 80 次宿主子进程。”

## 不能写进简历的结论

- 不能写成真实模型在长程代码 benchmark 上从 0/120 提升到 120/120。
- 不能写成五层压缩使 Terminal-Bench 通过率提升。
- 不能写成记忆检索具有普遍质量优势。
- 不能写成策略搜索获得了可识别的学习收益。
- 不能写成进程恢复保证外部副作用 exactly-once。

## 面试解释

“这些数字是故障注入，不是模型题分。它回答的是：当系统真的遇到大输出、旧记忆、进程重启或并发冲突时，机制是否可靠。Terminal-Bench 71/89 回答端到端任务能力；真实模型的逐模块因果消融另行报告。两者不能混用。”
