# RSI闭环实验协议：机制已验，真实模型效果待测

2026-11-13。目标岗位为Agent/大模型应用开发。先回答“控制器是否执行并受预算约束”，再回答“在未见任务上是否提高质量/效率”。本文件是下一批真实实验的设计，尚未冻结真实任务manifest，也未启动付费调用。

2026-11-14状态增补：用户新授权继续真实模型，声明约20亿tokens。当前先执行官方HumanEval短代码校准，11题已预检、53槽已冻结；详细[当前协议与v1→v2校准](../handoffs/2026-11-14-live-rsi-evaluation.md)。正式v2每attempt12轮，父64请求/1Mtokens/1800秒/6评估，四臂一致。旧费用停止与原16请求方案保留为历史，不再限制本轮新授权；原账本unknown不补零。HumanEval不提供长程能力或仓库外泛化证据，长程正式任务仍需容器接缝。

## 已跑的零provider闭环

实际 CodingAgent/core loop → 文件工具 → 本地可见测试 → scored trace → Dream SDK 参数搜索dry-run → 冻结策略 → 下一轮 A/B/C/random。完整入口为 `experiments/context_rsi/run_contract.py`，报告与原始计数见[RSI契约](results/context-rsi-20261113/rsi-contract/REPORT.md)。

A/B/random各4次有效尝试、12次脚本模型请求/6次本地评估；C为2次尝试、6次请求/4次评估。四臂共享相同上限：12请求、300000 tokens、90秒、6评估。SDK做21次离线replay评估，选width1/refine1；未晋升。四臂各自的后置独立final检查通过，原源文件未变。

这些只证明接口、实际执行和预算/数据隔离。质量为unmeasured、final_success=null；不把C少调用称为真实节费或学到泛化策略。独立final是合成本地契约，并非官方隐藏测试。

## 后续真实实验的四个条件

1. A：基础上下文，固定width2/refine1，产物投影关闭。
2. B：layered上下文和产物增强，策略同A。
3. C：B，使用train选出、holdout验收后冻结的策略。
4. random：B，同固定策略合法边界内的seeded-random调度。

A→B测工程增强组合；B→C测学习策略的独立增量；C与random比较排除仅换搜索顺序的影响。A/B并非只切一个compaction mode，因此不称单一压缩算法消融。必要时在后续独立批次增加仅mode开关，不能事后混写本批解释。

模型精确版本、采样配置、工具schema/权限、最大输出、起始快照、可见评分器、final评分器、机器和预算上限全部冻结。运行顺序按配对任务交错，保存随机seed；相同机会不要求用无效请求填满所有额度。

## 任务与数据边界

小规模候选规模为train3、holdout2、test6个任务，test每题2个seed，四臂共48个测试episode，训练/验证采集另计。重复seed不是新增任务。这个规模只能做pilot，不能宣称总体显著领先。

候选从固定版本的开源代码/终端任务选取。优先能在本机完成empty/gold检查、同时有可见测试与host独立final评分的任务。现有Terminal基础设施故障、SWE-EVO只有Dask前gate通过，不足以填充此计划；不编造任务ID、不把合成夹具当官方benchmark。新任务清单需另存source/test/solution的commit及hash，并在看模型答案前冻结。

train/holdout/test按仓库或任务族隔离；只改ID不能防相同内容泄漏。test题从未用于选参、阈值或prompt调整。final测试不放进branch workspace/模型上下文，原始final反馈不进入trace或优化。执行器与评分器由host固定，候选只有width/refine参数，不能修改评分代码。

## 预算、停止与指标

候选单episode上限：16请求、300000 reservation tokens、900秒、6评估；确切输出上限和工具动作上限在manifest冻结。这个配置只用于pilot，不代表足够所有长程任务。评分与摘要也占父额度，每branch不能领取新额度；同步快照/copy的磁盘IO不保证硬墙钟抢占。

必须汇总train/holdout采集、离线搜索、final评分和test总成本。报成功率、可判定覆盖率、达到成功的实际请求/token/时间、预算耗尽率、错误/清理失败数；只在完整同schema配对上算差值，同时列全部失败与缺失，禁止幸存者均值。KV读token字段缺失则保持unavailable，不用前缀字节数替代。小样本给逐题配对与不确定性，不挑有利seed。

当前旧账本533请求，已知$1.218608，未知预留$0.045131；原$30上限不重置。真实批次启动前须处理旧未知用量/停止状态并单独冻结费用计划；本轮没有授权绕过该停止条件。最大请求数不是费用估算，未按未核价格预写预算收益。

## 简历证据如何升级

目前可写“实现受控探索与冻结策略执行闭环，建立共享预算和四臂对照，保留可复查trace与失败记录”。真实实验完成后才补“在固定N题、模型X、预算Y下，B相对A、C相对B的具体指标”。若C没有改善，仍记录负结果与原因，不能用合成6/12请求代替真实数值。
