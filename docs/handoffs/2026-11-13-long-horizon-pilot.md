# 长程任务与记忆检索小样本实验计划

> 执行方式：两名 GPT-6.1 Sol / medium 分别负责 benchmark_live 与 longmemeval；主代理负责协议、数据准备、账本核对、集成、结果与面试材料。用户已明确要求设计并开始，无需再等待先前暂停指令。

日期：2026-11-13。代码起点 e15a14e82fea36fc5578ec37b0d87301cd75ce3d。目标是检验真实任务执行与记忆检索接缝，保留失败和成本，建立后续长程消融的可复查起点。单次长工具链不自动证明跨会话记忆或持久恢复。

## 设计取舍

采用固定小样本与单变量对照。全量 SWE/Terminal 成本与环境准备量过大；纯合成数据不能验证真实工程任务。当前选择先验收公开任务环境，再执行有限真实模型调用。SWE-Milestone 留作后续连续开发阶段，本批不启动；Dream 仍是回放参数研究层，不用普通单链日志伪造评分树。

## 任务与来源

- Terminal-Bench 2.1：官方 harbor-framework/terminal-bench-2-1，revision 12b7565679ce5006230b820666353673a133db93。固定八题：cancel-async-tasks、large-scale-text-editing、log-summary-date-ranges、git-multibranch、fix-git、regex-log、db-wal-recovery、custom-memory-heap-crash。81 个文件、154081B，逐文件对照官方 Git blob SHA 校验。只选 CPU/1核/2GB/gpus=0 的任务；不依据本批模型输赢换题。
- SWE-EVO：官方 SWE-EVO/SWE-EVO revision 9b83d5af943ba7a17567336f5b18239f73960219 的 hf_out/hf_dataset/test/data-00000-of-00001.arrow，12815976B，SHA256 74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520，已核 Git blob。按 instance_id 排序、每仓库最多一题取前两题，已冻结 conan-io__conan_2.0.14_2.0.15 与 dask__dask_2022.9.2_2022.10.0；共48条原数据。运行前另存原始安全元数据和官方 gold/empty 验收绑定。不通过官方环境预检的条目保留 blocked，不换题、不自制简化评分器。
- LongMemEval：xiaowu0162/longmemeval-cleaned 的 longmemeval_s_cleaned.json，277383467B，官方 LFS SHA256 d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442，与本地既有文件一致。按六种 question_type 各取5题，seed23；类内存在 _abs 时先固定一题，其余无放回抽样，实际30个ID在付费前冻结。不使用 oracle 历史代替长历史。

## 对照与指标

代码任务每题 artifact_projection on/off 各一次，seed23 平衡先后。两臂使用相同生产 Agent/ContextManager/Session、shell 与产物回读 schema、初始镜像和模型。它是产物投影单因素实验，不是全部 Harness 模块同时开关。工具栈是评测适配器的 shell+artifact，不能冒称普通 CodingAgent 全工具栈。

每任务最多64次模型请求（含摘要），output4096/context32k；总时限为 min(1800s, 官方 agent.timeout_sec)，shell单次至多120s且不超过剩余运行时间。原官方 task.toml 不修改。每题分别保存官方分数、实际请求/停止原因、token/费用/缺失项、工具与回读次数、摘要事件、耗时、产物字节及镜像ID。环境异常、模型未完成、已评分失败分开。

记忆三臂为 none / lexical / mem0。将完整历史确定性切成带时间戳、session_id 的 turn chunks，生产 Fact/MemoryStore 承载同一语料；禁止 gold answer、has_answer、answer_session_ids 进入索引或模型输入。无 LLM 抽取，因此只评固定历史索引，不评价生产事实蒸馏质量。两检索臂统一 top8、6000字符发送上限并记录截断。记录 session Recall@k、MRR、检索耗时、semantic错误及实际回退；无证据的拒答题单列。Mem0故障不能悄悄算成词法成功，合法空候选保留零召回。

回答与judge分别经同一预算保护，每题每臂各最多一次，无隐式重试；各阶段固定run_id、1请求/180s，避免答案完成后的排队时间使judge预算失效。共享题目/日期/回答模板和输出上限；先首题三臂回答+judge，再运行其余题目。DeepSeek替代官方默认judge时明确标为派生评估，绝对分数不与官方榜单直接比较。BGE-small-zh-v1.5面对英文历史是跨语配置，翻译或换多语模型应另开冻结实验。

Langfuse 本批不上传云服务；保留本地元数据观测。观测覆盖、丢失与开销应另做专项，不把开关当必然提高代码成功率。Dream不做在线消融或真实策略晋升。

## 预算与安全边界

唯一累计账本为 WSL /home/mll/.cache/my-agent-bench/campaign-20261112/ledger.json。启动前300条请求全部ok，已结算888355微美元（$0.888355），未知预留0；根上限30000000微美元、默认16请求/600s不重置。新增run级冻结限制向后兼容，原记录不迁移。

先执行 memory campaign_id=longhorizon-20261113-memory，绝对累计ceiling=$3.888355；随后 code campaign_id=longhorizon-20261113-code，绝对累计ceiling=$15.888355。本批新增最多$15，外层原$30仍有效；不是重新获得30美元。全部调用在发出前预留，缺usage/费用超界/API失败保留预留并停止对应阶段，主代理停止其他付费阶段；不换run_id或campaign_id重试。

冻结同一 OpenCode Go 兼容端点与 deepseek-v4.1-flash，价格预留沿用账本输入$0.30/M、输出$1.20/M的保守口径，不当供应商实际账单。凭据仅由主机内存加载传给模型进程，不进CLI字面量、容器、文档、日志或Git。SDK/Harbor重试均为0。源或价格变动须停止并另存新计划，不能覆盖已开始的原始运行。

## 执行环境和阶段

- [x] 原工作区保持，复用 interview-redesign worktree；两助手责任范围隔离。
- [x] 核验L20可连但无Docker/uv、拷贝非Git；历史官方评测实际在本机WSL Ubuntu-20.04，Docker26.1.3可用。当前使用WSL CPU；所有新子进程仍设置CUDA_VISIBLE_DEVICES=2，未运行GPU任务。
- [x] 固定官方任务文件与记忆LFS、SWE-EVO Arrow来源；建立独立私有工作目录和初始账本快照。
- [x] 原样运行八个TB oracle。首轮均因Docker registry连接失败而environment-blocked，模型请求为0，原始记录保留。
- [x] 修复任务专属网络路径并保存新一次oracle证据：TB七题通过、一题blocked；EVO Dask通过gold/empty，Conan gold仍有九项P2P失败。未改官方task/test/solution、不把基础设施重试当模型重复试验，见[预检归档](../experiments/results/long-horizon-20261113/preflight/REPORT.md)。
- [ ] 完成两条驱动的离线回归、交叉审查、源提交与选择清单冻结。
- [ ] 运行纯检索、首个完整付费对照、核对账本后续跑冻结小样本。
- [ ] 归档原始证据与可复算汇总，明确本批有效样本、无效/阻塞和费用；更新实验日志、问题深挖和GitHub PR，不自动合并。

每阶段以真实产物和退出状态为准。所有计划指标在测量前保持未测，不能预填收益。

## 运行约束补充（2026-11-13，付款前/启动阶段已核）

用户后续明确暂不迁移，继续本机；所有执行器、Docker任务和CPU向量索引运行于本机WSL，生成/判分仍用既有远端DeepSeek兼容API，不在L20跑任务。Memory索引离线建库等待1800秒、关闭60秒；这与产品全局10秒工作等待不同，不按在线SLA解释。正式协议已升级为v2匿名SID，旧0API单题探针排除。

首题正式6调用已结算1485微美元，1独立题none0/1、lexical1/1、mem01/1；仅确认流程。30题批量resume已启动，TB串行排队并在首个科学配对检查通过后续跑；两进程遇未知用量/源漂移/预算上限会停止，仍复用原30美元账本。启动报告见[首题记录](../experiments/results/long-horizon-20261113/smoke-v2/REPORT.md)。

### 后续状态更正：保持本机并等待EVO源码稳定

30题检索已全部完成，回答/判分继续运行。原Terminal等待器在仍处于waiting_for_memory、无子进程、正式freeze尚未发生时停止，保留terminal-queued-v1及停前状态；付费trial为0。停因是新增EVO接缝仍在修复，而Terminal源码冻结会枚举benchmark_live文件。待修复、复核和源码稳定后，另建调度阶段接续，不覆盖旧目录，不重跑任何已消费模型任务。

这次停止不影响Memory运行，未改其123份冻结源码或原累计账本。用户明确暂不迁移L20，后续实验仍在本机WSL/Docker/CPU执行；调用DeepSeek的远端API不等于在L20运行。

### Memory完成与EVO接缝验收

Memory已完成30题三臂、180调用；none4/30、mem015/30、lexical22/29有效（另1judge空正文/length，不重试）。完整三臂29题中22/15/4，语义未优于词法；4弃答单列，召回分母26。客户端新增$0.049191，全账本480请求/$0.937546、unknown0。[正式报告](../experiments/results/long-horizon-20261113/memory-v2/REPORT.md)另存，不覆盖首题记录。

EVO报告身份/额外可执行文件两个阻断已修并独立复核；源码稳定，公开adapter与73文件清单及[复现说明](../experiments/SWE_EVO_ADAPTER.md)可用。真实clean-Dask-empty零LLM契约通过，F2P0/44、P2P2861/2861，报告原hash与旧empty相同，容器已清理。尚无本批EVO模型成绩。

源码455915743ee687006698eaeefe3a98d43b55609e已推origin且四平台CI run43422249290全通过。Terminal已另开terminal-queued-v2，12:05 UTC首cancel任务off/on开始；计划SHA8cb76c1479c97638eed024e627e13397a1fe63078b4209105886ac4442e16216，16槽中七题可用、一题blocked。见[启动快照](../experiments/results/long-horizon-20261113/terminal-start/REPORT.md)，完整成绩待运行产物，不把启动当完成。

### 本批最终付费状态：已按保护停止

首Terminal两trial已结束：off raw0且验证器uv下载失败未执行测试；on raw1但AgentTimeoutError/取消，最后请求缺usage。有效科学配对0、delta不可用；剩余14槽未启动，EVO模型未运行。53请求已知结算$0.281062，另$0.045131未知预留保持；全账本533请求/已知$1.218608/unknown1，不能当完整费用。

队列在核到账本unknown后停止，没有绕过或重试。当前fresh运输探针可通，不能改写原网络失败；任务进程、自有容器和临时转发器已清理。[停止报告](../experiments/results/long-horizon-20261113/terminal-first-pair/REPORT.md)含原reward、执行终态与预算边界。Memory完整结果保留；后续仍以本机为执行地点，先做离线工作，缺失用量保持不可用。
