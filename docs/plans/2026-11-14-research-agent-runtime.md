# CPU 代码实验 Agent：实施与验收账本

日期：2026-11-14。用户已批准实现；工作分支 `codex/interview-harness-redesign`，起点 `a9521d3`。只在本机执行，WSL Docker 属于本机；不迁 L20、不恢复 DS/DSH、不重跑旧 HumanEval。

## 交付协议

把现有 CodingAgent、RSI 分支、预算、记忆与观测用于一个可复核的场景：固定数据 → 编辑 train.py → 受限容器预测 → 宿主重算 RMSE → 多轮选优 → 完成边界恢复 → 独立 final。借鉴来源和边界见 [选型记录](../research/2026-11-14-scenario-selection.md)。首版仅标准库 Python，不依赖 GPU 或 sklearn。

模型只能使用工作区中的五个文件工具和 train.py；不提供宿主 shell、MCP、skills 或标签。容器无网络、只读根、非 root、资源受限；训练数据、特征、脚本只读，唯一写挂载是预测 CSV。镜像固定 digest，评分器与数据在宿主核 hash。评分不接受 stdout 自报值，不加载不可信 pickle。

## 文件所有权

- SOL-1 (`/root/sol_runtime`, GPT-6.1 Sol / medium)：research_task.py、research_evaluator.py、rsi_branch.py、rsi_runtime.py 及直接测试；后续 dream_rsi_optimizer.py 及直接测试；独立实施报告。
- SOL-2 (`/root/sol_eval`, GPT-6.1 Sol / medium)：research_runtime.py、research_cli.py、research_integrations.py 及直接测试；必要的 CLI 打包配置；独立实施报告。
- 主代理：设计、外部镜像/数据预检、组合验收、统一实验日志、面试证据、精确暂存提交与 PR 更新。只有两位执行助手，不派生第三位。

多人共享目录，保留他人修改，不越权编辑；有依赖顺序交接。主代理不重复长篇源码调查。

## 验收顺序

1. **可信评分。** UCI 数据许可和下载 hash 留档；固定 train/dev/final。均值与线性脚本得到可区分的宿主 RMSE；伪造 stdout、非法值/行数、超时与取消不能成为有效结果。实际容器 smoke 后才能声明隔离链路通过。
2. **持久工作流。** hash-chain journal 在请求/评分前预留，结算真实用量；恢复从全 journal 计算总预算。已开始而未知的 API/评分不自动重做。仅支持完整 round 边界续接，不声称任意指令 exactly-once。
3. **真实模型闭环。** 主代理用真实 DeepSeek API 跑小规模实验，第一进程完成一轮后暂停，新进程恢复，最后独立 final。保存脚本、预测 hash、评分、用量、策略与错误；final 不回流选参和记忆。
4. **三项接线。** Mem0 只索引有证据的经验，状态/预算不由向量库决定；train 写入，holdout/test 用冻结快照。Langfuse 复用现有 observer，记录 task/round/branch/score/recovery；无服务凭据则明确仅本地 SDK 验证。RSI 消费真实连续评分树，优化只用显式策略 train/holdout 数据；不把单 CSV 的 dev/final 伪装成跨任务 holdout。
5. **受控策略。** 修候选覆盖、有效候选去重与成本目标；保留 baseline，可以无晋升。固定/随机与冻结 RSI 使用相同动作与预算。真实收益只有独立对照后才能写简历。
6. **交付。** 定向测试、组合回归、lint、CLI/wheel、秘密扫描；更新统一实验与面试记录；只暂存明确路径，推 origin PR #2，不合并。

## 数据与结果边界

每任务内 train/dev/final 与策略任务集合 train/holdout/test 分开冻结。首批数据 UCI Wine Quality red/white 与 Airfoil，CC BY 4.0，原始文件和下载清单在本机 `.git/codex-delivery/research-runtime-20261114/datasets`。当前是工程验证任务，不称官方长程 benchmark。

此前 HumanEval 的 47/48 final 与一次权限失败、974 次请求和无可识别 RSI 增益保持历史归档。新场景不得借用旧数字。真实 API、脚本模型、Docker smoke、本地 SDK、云上传各自报告。

## 进度（按证据更新）

- 起点干净；SOL-1 已落盘任务/评分器和受控文件工具，报告 58 项定向回归通过，主代理待独立验收。
- SOL-2 已落盘核心 ledger/runtime，CLI 与组合测试进行中。
- Windows 无 docker 命令；本机 WSL Ubuntu-20.04 有 Docker 26.1.3。daemon 直接 pull 超时；采用 Windows 从官方 Registry 验证 blob 后 Docker load，不改全局网络配置。首次下载中断，保留已校验 layer 缓存，进行有限重试。
- 三份 UCI CSV 已下载并记录 SHA；尚未形成新模型成绩。Langfuse 环境变量均未配置，待核全局设置 presence。

失败、变化与最终命令追加到 [统一实验日志](../experiments/HARNESS_EXPERIMENT_LOG.md)，不得将本页待办视为完成结论。

## 首版验收结果

已交付可信评分、受限容器、持久预算、完整round检查点、CLI与三项场景入口。真实red完成两轮、独立进程resume与一次final；含v1失败共25请求108078tokens/10评分，final保留均值baseline。研究源冻结后未因CI修复变化，功能head47eee1b四矩阵及SDK/wheel检查通过。

用户要求一天内可写简历，首版收敛为这条工程流程：[最终报告](../experiments/results/research-agent-20261114/LIVE_REPORT.md)、[简历稿](../interview/RESUME_READY.md)及事实账本已整理。两轮都含无效attempt，无完整策略训练树；Airfoil/white/正式三组未启动，RSI未晋升、Mem0无质量对照、Langfuse无云端回读。原计划的质量实验仍为后续工作，不影响本次工程首版交付，也不被记为已完成。
