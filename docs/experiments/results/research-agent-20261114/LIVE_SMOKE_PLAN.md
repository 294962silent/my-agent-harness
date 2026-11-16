# 真实链路 smoke：事前协议

2026-11-14，首次本场景模型调用前登记。用途是验证工程闭环，不是质量消融或官方 benchmark。模型固定 `deepseek-v4.1-flash`，真实凭据只在宿主内存，重试为0；本机 CPU Docker、固定镜像见 image-source.json。

## 阶段

1. Wine Quality red：策略 train，seed23，任务内部60%/20%/20% train/dev/final。两轮，第一轮提交后暂停，另一进程 load/resume。每轮最多3个候选、每候选4个 Agent turns；固定收集参数width2/refine1。
2. Airfoil：策略 val（即holdout），seed23，内部同样划分，一轮、最多3候选。仅读阶段1冻结经验；不写经验、不用final调参。
3. 用前两阶段已完成的真实 dev trace 导出显式生产 dataset。只有满足 SDK/优化器的独立任务与完整性检查时才搜索和验证；缺覆盖则 blocked，不补造数据。baseline可以胜出，不强制学到新策略。
4. Wine Quality white：策略 test，seed23，内部同样划分，一轮。只消费第三阶段独立验证后冻结的参数；若无合法可消费策略则不启动“训练策略”条件，可另外明确固定参数smoke，但不能冒称学习贡献。
5. 各campaign完成后可执行独立final，final反馈不参与策略选择、记忆与候选搜索。策略优化必须在final_started之前完成；工程恢复演示以步骤1为主。

每个campaign冻结独立累计预算：train最多32请求/200000tokens/900活跃秒/20评分，val和test各16请求/120000tokens/600活跃秒/10评分；每请求最多4096输出tokens。评分CPU1核、512MiB、每次60秒，0 GPU。余额多不取消任务预算。预算不足、未知usage、在途round或pin变化均停止，失败保留。

## 经验与观测

train可以写入已验证dev候选的经验；val/test使用同一封印的train快照。默认先以明确指定模式跑链路，Mem0 CPU真实SDK另验，不能把词法结果计为Mem0效果。模型请求/预算/阶段观测为本地证据；Langfuse环境和全局配置均无可用凭据，不能声称云上传。

## 结果解释

red/white同属Wine Quality，任务相关；只有3个数据集、小样本、单种子，不能推断泛化或统计提升。不同任务RMSE量纲不同，不平均原始RMSE；utility仅供本任务排序，跨任务优化使用优化器声明的归一化目标。实际参数、调用与异常由账本决定，不据计划填结果。正式三组和同预算固定/随机对照另建计划，不从这次smoke推断优势。

## 首次失败后的运维修订（v2首请求前登记）

v1 red 完成1次付费请求1252 tokens、1次baseline评分，活跃21.2123297秒，随后第一Agent分支MemoryError，round未提交。用量全部结算；旧round永久保留阻断，不修改其journal或重置。Mem0初始化和工具结果已完成；并行整库测试时另观察到Windows进程启动STATUS_COMMITMENT_LIMIT，具体异常行与因果未证实。同一已结算response的工具调用离线重放未复现，不能声称修复了确定的代码根因。

允许一次独立v2 red campaign（新ID，相同数据/种子/实现/策略），串行运行，不同时跑整库或本地模型测试。新上限从原预算扣减：31请求、198748 tokens、19评分、878.78活跃秒；连同v1不超过原train总ceiling。原失败计入场景覆盖与全部成本，不能仅发布v2成功。若再次出现未知用量/资源失败则停止并保留，不自动循环重启。此为工程smoke的事后运维修订，不进入未修订的科学比较。
