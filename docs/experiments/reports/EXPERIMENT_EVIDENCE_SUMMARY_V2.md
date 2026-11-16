---
schema_version: 2
evidence_cutoff: 2026-11-15
repository_branch: codex/interview-harness-redesign
repository_head_at_documentation_start: a2d72e14f0241c1016dffe27f11886d05cbebe59
primary_scope: long-horizon coding-agent runtime, context evidence, memory, host scoring, controlled strategy search, observability
evidence_result: mixed_positive_negative
paid_requests_started_by_this_summary: 0
---

# my-agent-harness 实验证据汇总 V2

本文把仓库内分散的真实运行、离线消融、公开任务 pilot、负结果与失败边界整理成一份可扫描、可追踪的证据账本。它不替代原始报告或冻结结果；数字冲突时，以对应运行目录中的 manifest、ledger、summary 和终态记录为准。

本文没有发起模型请求、Docker 评测、embedding 或 benchmark，也没有修改源码、冻结实验产物、历史账本或预算文件。

## 阅读规则

| 证据等级 | 含义 | 可以支持 | 不能单独支持 |
|---|---|---|---|
| E0 代码实现 | 模块、接口和回归测试已存在 | 某机制已接入产品路径 | 机制带来质量收益 |
| E1 本地/确定性验证 | 使用生产接缝、临时文件、固定镜像或脚本动作 | 指定故障可被稳定关闭 | 真实模型自然分布上的成功率 |
| E2 真实链路 | 付费模型、真实数据、journal、usage 与终态可核验 | 指定模型/样本/版本下完成了什么 | 跨模型、跨数据或生产 SLA |
| E3 公开任务 | 固定公开任务、官方 verifier 或 benchmark | 指定任务与 trial 的终态 | 总体 benchmark 排名 |
| E4 科学比较 | 冻结协议、配对样本或独立 final | 明确样本内的差异或不可识别性 | 不能把小样本不显著差异写成总体优劣 |

本项目的证据设计优先级是：

1. 先证明故障与机制对应，即 baseline 失败、生产 Harness 接缝通过。
2. 再证明真实链路能完成，即真实 provider、工具、journal、评分与清理都有终态。
3. 最后才看真实任务结果；单题、单 trial、单 seed 只写描述性差异。
4. 未知 usage、超时、截断、基础设施故障和负结果保留在原批次，不重试成“成功样本”。

## 主张登记表

| ID | 可陈述事实 | 证据类型 | 结论状态 | 禁止外推 |
|---|---|---|---|---|
| C01 | 7 类确定性故障各重复 20 次，minimal baseline 为 0/140，生产 Harness 接缝为 140/140 | E1 | 机制故障修复成立 | 不等于真实模型任务通过率提高 |
| C02 | deadline/progress guard 在脚本化“只做局部验证、不交付最终产物”消融中为 baseline 0/20、guarded 20/20 | E1 | 机制对预置故障有效 | 未证明真实模型会遵守 steering |
| C03 | Wine Quality red 真实链路完成两轮、跨进程恢复和一次独立 final | E2 | 工程闭环成立 | 两个 dev checkpoint 都保留均值 baseline，不能写质量提升 |
| C04 | LongMemEval 96 题中查询注入上下文 105,316→11,983 tokens，准确率 71.9%→69.8%；计入抽取后总 token 105,316→17,705 | E2/E4 | token 大幅减少，准确率未提高 | 不能写精度提升或统计等价即普遍无损 |
| C05 | LoCoMo 原 30 题、独立确认集和 MMR/会话去重实验均未显示稳定检索优势 | E2/E4 | 小样本负结果/不可识别 | 不能写词法、语义、RRF、MMR 或去重的显著准确率优势 |
| C06 | 受控策略搜索完成 7/8 个 test final 臂，具备晋升、回滚和 usage unknown fail-closed | E2/E4 | 控制机制成立，训练收益不可识别 | 不能写 RSI 已产生学习收益或实现自我进化 |
| C07 | Terminal-Bench 2.1 的 fix-git 三臂均 reward 1，requests 为 17/10/12 | E2/E3 | 单题单 trial 三臂通过 | 不能归因为五层压缩/artifact 的稳定收益 |
| C08 | large-scale-text-editing enhanced 在 1,080 秒取消，22 requests，verifier 0/5 | E2/E3 | 有效负结果 | 不能计作通过或隐藏超时 |
| C09 | custom-memory-heap-crash v4 三臂未得分；artifact-off 在 24 requests 后 ContextBudgetExceeded，baseline/enhanced 均 5/6 | E2/E3 | artifact 推动到 verifier，但正确性未提升 | 不能写 artifact 修复 SIGSEGV 或使任务通过 |
| C10 | Langfuse v3+v4 共 7 traces / 121 generation observations，与 ledger 请求数一致 | E2 | 元数据观测链路成立 | 不证明上下文或模型质量提升 |
| C11 | SWE v2 小样本 on 2/5、off 1/5；on 产生 11 个 artifact、0 次模型回读 | E2/E4 | 小样本方向与使用缺口 | 不能写稳定质量、费用或 KV 收益 |

## 长程代码 Harness：确定性故障矩阵

来源：[六小时证据包](long-horizon-code-6h-results.md)、[Phase A 报告](long-horizon-code-phase-a-v1.md)、[机器汇总](../results/long-horizon-code-6h-20261115/summary.json)。

实验使用生产 ArtifactStore、layered ContextManager、MemoryStore、ResearchCampaign journal、FileMutationQueue、PolicyRegistry、ResearchTask、固定镜像 Docker evaluator 与宿主测试。输入和模型动作是脚本化的，因此该矩阵证明的是 Harness 接缝，不是模型能力。

| 场景 | baseline | 生产接缝 | baseline 根因 | enhanced 通过机制 |
|---|---:|---:|---|---|
| 大输出证据回读 | 0/20 | 20/20 | 73,468 字符输出被头尾截断，第 2,501 行证据丢失 | 全文 artifact 持久化、handle 注入、artifact search 命中 |
| 记忆 revision/scope | 0/20 | 20/20 | stale 或跨 scope 候选返回旧值 | 异步返回后重建 revision，并校验 scope/identity/revision |
| checkpoint/resume | 0/20 | 20/20 | 新进程丢失内存完成集合，analysis 重放 | hash-chain journal 恢复，只执行剩余 patch |
| 同文件并发修改 | 0/20 | 20/20 | 裸 read-modify-write 固定丢失一次更新 | FileMutationQueue 对同一规范化路径串行 |
| 受控策略晋升 | 0/20 | 20/20 | 只看 train，晋升 holdout 更差候选 | train+holdout 严格门禁、保留 incumbent、回滚 |
| 宿主评分与沙箱 | 0/20 | 20/20 | 信任候选自报 1.0 | 固定镜像执行、宿主重算 0.125、检查越界与额外文件 |
| 连续仓库修复 | 0/20 | 20/20 | 证据、旧 revision、恢复重放与并发更新共同失败 | 串联 artifact、记忆校验、journal、路径锁，最终 compile/test 通过 |
| **总计** | **0/140** | **140/140** | 7 类确定性故障 | 140 次均通过 |

附带运行规模：

- 宿主评分场景每次运行两个容器，共 40 次固定镜像 Docker 执行。
- 连续仓库场景每轮运行 4 个宿主子进程，共 80 次。
- 本矩阵的付费模型请求为 0。

可支持的说法是“对应机制关闭了预置故障”；不能写成“真实模型在长程任务中的成功率从 0 提升到 100%”。

## deadline 与 progress guard

来源：[progress guard v1](long-horizon-progress-guard-v1.md)。

该机制接收工具完成事件；当时间比例达到预警阈值或连续无进展计数超限时，最多注入一次结构化 steering：

1. 先持久化请求的最终交付物。
2. 再对精确产物执行全仓库验证。

| 条件 | 通过 | 失败类型 |
|---|---:|---|
| baseline | 0/20 | 全部为 timeout_before_final_delivery |
| guarded | 20/20 | 无 |

记录运行中，baseline 执行 7 次局部检查后超时；guarded 在约 76% 时间比例注入一次，produce final artifact 后执行宿主 py_compile 与 unittest。20 次结果签名唯一。

边界：时钟、仓库动作、进展信号和最终交付均是脚本化；宿主编译与测试只验证产物，不验证真实模型是否服从 steering。真实 Harbor 集成时机、token 开销和策略质量仍未测。

## 五层上下文与 artifact 证据

产品设置默认启用 layered 模式，对应 `src/my_coding_agent/settings.py` 中 `CompactionSettings.enabled=True` 与 `ContextCompactionSettings.mode="layered"`。实现按需组合五种处理：

1. canonical 历史与摘要来源身份校验，避免把不同分支或不同覆盖来源的摘要复用。
2. 大工具输出全文写入 content-addressed artifact，发送视图只保留有界预览和 `artifact:<owner>:<id>` handle。
3. 对已闭合、成功且不在最近窗口内的工具组执行微压缩。
4. caller 明确登记已完成闭包前缀后，折叠该段并保留结构化摘要。
5. 仍不满足预算时执行有界结构化语义摘要；没有安全投影则抛 `ContextBudgetExceeded`，不静默超预算发送。

artifact 回读是字节分页且 Unicode-safe；搜索结果数与每行长度有界；跨 workspace owner、符号链接逃逸、损坏 hash 和二次 artifact 化均被拒绝。原始 Session 正文保持 canonical，发送视图不覆盖原始历史。

离线回归夹具记录：

- 120,019B 大输出的发送视图从 120,019B 降至 7,041B。
- 两路证据可由 artifact 回读恢复。
- 3 个独立只读工具的峰值并发从 1 提升到 3。
- 写后读依赖违例从 1 降至 0。

这些是生产接缝上的工程夹具结果，不是真实模型质量或 KV-cache 收益排名。

## LongMemEval 96：省 token，准确率未提高

来源：[LongMemEval RESULTS](../../../experiments/longmemeval/RESULTS.md)。

| 方案 | 正确 / 96 | 准确率 | 查询注入 token | 计入抽取后总 token |
|---|---:|---:|---:|---:|
| 全量上下文 | 69 | 71.9% | 105,316 | 105,316 |
| 三层记忆 v2p | 67 | 69.8% | 11,983（−88.6%） | 17,705（−83.2%） |

这是本项目最明确的“效率正结果、准确率负方向”组合：

- 查询时注入上下文减少 88.6%。
- 计入逐题历史抽取后，总 token 仍减少 83.2%。
- 正确数从 69 降到 67，不能写准确率提升。
- 两臂 95% 区间重叠不能抵消少答对 2 题这一事实；在小样本上也不支持“完全无损”。

## LoCoMo：检索与上下文多样性没有稳定优势

主结果来源：[LoCoMo v3 summary](../results/locomo-memory-v3-20261115/summary.json)。

### 原 30 题四臂

| 臂 | 正确 / 30 | 主结果准确率 | valid judge | session recall@8 | session MRR |
|---|---:|---:|---:|---:|---:|
| raw lexical | 16 | 53.33% | 30/30 | 0.8299 | 0.7837 |
| raw semantic candidate | 17 | 56.67% | 28/30 | 0.8125 | 0.7722 |
| extracted facts + lexical | 12 | 40.00% | 25/30 | 0.8913 | 0.7678 |
| extracted facts + semantic candidate | 15 | 50.00% | 25/30 | 0.9140 | 0.8689 |

12 个 judge 因 512-token 上限按冻结规则计错。另设的 scoring-only 敏感性分析统一把输出上限提高到 4,096 后，正确数变为 16/18/13/17；关键配对仍不显著。

### 独立 24 题确认

来源：[raw confirmation summary](../results/locomo-raw-confirmation-v1-20261115/summary.json)。

| 臂 | 正确 / 24 | session recall@8 | session MRR | evidence-turn recall@8 |
|---|---:|---:|---:|---:|
| lexical | 10 | 0.8264 | 0.8194 | 0.6500 |
| semantic | 12 | 0.8542 | 0.7431 | 0.7403 |
| hybrid RRF | 10 | 0.8819 | 0.7951 | 0.7611 |

semantic 比 lexical 多 2 题，discordant 为 0:2，exact McNemar p=0.500。3 个 judge length 全量补评后，lexical/semantic/hybrid 为 10/12/12，配对仍不显著。三种检索指标给出的方向不一致。

### MMR 与会话去重

来源：[context diversity summary](../results/locomo-context-diversity-v1-20261115/summary.json)。

| 臂 | 正确 / 24 | session recall@8 | session MRR | evidence-turn recall@8 |
|---|---:|---:|---:|---:|
| semantic top-8 | 9 | 0.7210 | 0.6244 | 0.6322 |
| semantic MMR λ=0.7 | 9 | 0.8244 | 0.6476 | 0.6066 |
| semantic session cap 2 | 10 | 0.7210 | 0.6244 | 0.6114 |

三组 exact McNemar 均为 p=1.000。MMR 提高了 session 指标，却降低了 evidence-turn recall，答案数没有增加。不能写 MMR 或会话去重显著提高准确率。

## 受控策略搜索：控制可用，学习收益不可识别

来源：[策略探索面试结果](../results/research-effect-20261114/rsi-interview-results.md)、[机器汇总](../results/research-effect-20261114/rsi-compat7-summary.json)。

已实现并执行的控制面包括：

- 候选只修改白名单探索参数，固定 runner、镜像和评分器。
- train 选择、独立 holdout 晋升、冻结 final 执行。
- 唯一候选生成/折叠、轨迹回放、成本预留与结算。
- active/previous 原子指针与 rollback。
- provider usage unknown 时停止后续派发并 fail-closed。

| 数据集 | 完成 final 臂 | 关键结果 |
|---|---:|---|
| QSAR | 3/4 | fixed、random、greedy 的 final RMSE 均为 1.432272，ratio=1.0；frozen 在 1,200 秒边界 usage unknown，无 final |
| AutoMPG | 4/4 | fixed/random/frozen-fallback/greedy final RMSE 为 2.515738/2.437353/2.495291/7.154405 |

合计完成 7/8 个 test final 臂。AutoMPG 的 random 在本次数值最低，但只有单 seed、每臂一次 final，没有推断统计；trained-frozen 实际回退到 fixed-reference 参数，不能解释为训练收益。

最终总账为 151 requests、1,798,894 accounting units、1,655,475 个 known settled tokens、56 evaluations、12,776.24 active seconds；其中 3 个请求 usage unknown，实际总 token 与 USD 保持 unavailable。

## 真实代码实验运行时

来源：[Wine Quality red LIVE_REPORT](../results/research-agent-20261114/LIVE_REPORT.md)、[live-summary](../results/research-agent-20261114/live-summary.json)。

已核验的工程路径：

- candidate workspace 只允许 `train.py`。
- 固定 Docker 镜像、无网络、资源配额与受限输出目录运行候选。
- 容器只写 `predictions.csv`；宿主检查 schema/ID/有限数值，再用候选不可见标签重算 RMSE。
- journal 在锁内预留请求、token、活跃时间和评分额度。
- round 只有请求结算、工具配对、清理与 checkpoint 核验后提交。
- final 不产生模型/记忆/控制器反馈路径。

v2 运行完成 24 requests、106,826 reported tokens、8 次 dev 评分和 1 次独立 final。新进程恢复后，round 0 的 12 requests / 48,915 tokens / 4 evaluations 与 root_id 保持一致。两个 dev round 的候选都与均值 baseline 同分，checkpoint 保留 baseline；因此只能写流程与恢复完成，不能写模型改进。独立 final 的 RMSE 0.755412 与前两项不是同一 split，不能当成训练收益。

全部 v1+v2 口径为 25 requests、108,078 tokens、10 次评分；v1 的 MemoryError 原样保留，供应商 USD 未核验。

## Terminal-Bench 2.1 v3/v4

来源：[长程代码六小时证据包](long-horizon-code-6h-results.md)、[公开 pilot 报告](long-horizon-public-pilot-v1.md)、[冻结机器汇总](../results/long-horizon-code-6h-20261115/summary.json)。

固定来源 revision 为 `12b7565679ce5006230b820666353673a133db93`，模型为 `deepseek-v4.1-flash`。v3 plan SHA-256 为 `f15d6ade822a7dcc582e15d4df9d63d382387395f0f4578c334fccf5e254008b`；v4 plan SHA-256 为 `90971e8b39c8b9660de2249bae8fd03d6ad285a8c1d0ef126e4b2ed030ebc288`。

### fix-git：完整三臂

| 臂 | compaction | artifact | reward | requests | input | output | conservative cost |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline | baseline | off | 1 | 17 | 85,025 | 6,924 | $0.033823 |
| enhanced | layered | on | 1 | 10 | 32,826 | 2,417 | $0.012753 |
| layered without artifact | layered | off | 1 | 12 | 55,642 | 3,593 | $0.021009 |

三臂均通过官方验证。enhanced 的 10 requests 少于 baseline 的 17 和 layered-without-artifact 的 12；这是单题、单 trial 的描述性差异，不构成统计显著性或稳定收益。

### large-scale-text-editing：超时负结果

- enhanced 在冻结的 1,080 秒 Agent 上限被取消。
- 22 requests 均完整结算；input 88,968、output 41,638、保守成本 $0.076664。
- 官方 verifier 实际执行，5 项检查全部失败。
- 最终 `/app/apply_macros.vim` 未形成，后续输入输出等价检查失败。

official reward 为 null。不能把 verifier 失败写成基础设施故障，也不能计作通过。

### custom-memory-heap-crash v4

| 臂 | compaction | artifact | 官方终态 | verifier | requests | input | output | conservative cost |
|---|---|---:|---|---:|---:|---:|---:|---:|
| layered without artifact | layered | off | ContextBudgetExceeded | 未执行 | 24 | 26,429 | 49,037 | $0.171078 |
| baseline | baseline | off | reward 0 | 5/6 | 15 | 13,397 | 35,522 | $0.079754 |
| enhanced | layered | on | reward 0 | 5/6 | 21 | 28,610 | 58,180 | $0.118269 |

baseline 与 enhanced 的唯一共同失败项都是 `test_outputs.py::test_release_build_runs_without_crash`；release 程序返回 `-11`（SIGSEGV）。artifact 使 layered 路径从上下文预算失败推进到 verifier，但没有修复正确性。

### 成本与观测对齐

- v3：61 requests，input 262,461，output 54,572，保守成本 $0.144249。
- v4：60 requests，input 68,436，output 142,739，保守成本 $0.369101。
- v2 失败请求：1，$0.000276。
- v2+v3+v4：122 requests，$0.513626。
- v3+v4 Langfuse：7 traces / 121 generation observations，与每臂请求数 17/10/12/22/24/15/21 和 usage 对齐。

费用是固定价格下的客户端保守估算，不是供应商发票。

## Langfuse 元数据观测

来源：[真实服务 verification](../results/langfuse-live-20261114/verification.json)。

早期双探针使用真实 provider 请求验证两种接入：

- 既有 runtime 直接 attachment。
- 可复用 experiment owner CLI。

两次探针均为 HTTP 200；远端 observation type 为 `GENERATION`，model、session、metadata 与 usage 匹配 ledger；关闭后 export/close errors 为 0、dropped 为 0、客户端关闭。v3/v4 的 7 trace / 121 generation 回读进一步验证了 campaign 级对齐。

边界：

- 观测是 metadata-only，不保存 prompt、回答正文或 gold。
- 远端可见性存在时序，使用有界指数轮询。
- 观测回读不等于模型质量评测。
- 单个 generation 的 session 已验证；多轮与多 trace 的分组在早期探针中未覆盖，后续 campaign 按 session 匹配。

## SWE v2 小样本

来源：[SWE v2 报告](../results/swe-v2-pilot-20261112/REPORT.md)。

- 选定 6 题，5 题通过环境/gold 预检。
- 5 个可用题各跑 on/off 一次，共 10 次真实模型 trial。
- on 组通过 2/5，off 组通过 1/5。
- 7 次为空补丁；160 requests 全部结算，保守成本 $0.503462。
- on 组产生 11 个外置 artifact，但两组 `artifact_read/artifact_search` 响应均为 0。

这组结果只能说明小样本方向与“artifact 可调用尚未转化为模型使用”的产品缺口，不能充当消融、总体成绩或成本收益证明。

## 全局失败与边界清单

| 失败/边界 | 保留的结论 |
|---|---|
| 脚本化模型动作与真实模型自然失败分布不同 | 确定性矩阵只证明机制，不证明模型能力 |
| progress guard 未进入真实 Harbor | 不能写模型会遵守 steering |
| LongMemEval 96 准确率下降且 CI 重叠 | 能写 token 降低，不能写精度提升或普遍无损 |
| LoCoMo 30 题、24 题确认与 MMR 均不显著 | 不能写语义、RRF、MMR 或去重稳定更好 |
| QSAR frozen、Wine holdout 均出现 usage unknown | 不得把 reservation 当实际 token/费用，不得重放未知请求 |
| fix-git 只有一个 task/trial | 不得写五层压缩/artifact 普遍提高公开 benchmark |
| custom-memory 三臂未得分，failure SIGSEGV | artifact 只解决上下文预算阶段，不解决正确性 |
| journal/checkpoint 不具备外部副作用 exactly-once 证明 | 恢复只从可核验安全边界继续 |
| 同步第三方代码可能不协作取消 | 不宣称强制抢占任意进程或代码 |
| Langfuse metadata-only | 不支持内容回放、云端答案评估或质量提升结论 |
| 客户端全冷价估算 | 不能冒充供应商账单或订阅余额 |

## 证据文件指纹

以下 SHA-256 在 2026-11-15 文档整合前从工作树原始字节计算，用于发现源文件漂移。本文自身不列入自身 hash。

| 证据文件 | bytes | SHA-256 |
|---|---:|---|
| `experiments/longmemeval/RESULTS.md` | 6,046 | `0a8068380f8276f5a2151356cf3959853c1f917bf08eec0cdf2a9585a441e17b` |
| `docs/experiments/reports/interview-resume-v1.md` | 46,684 | `4d51d23dc34f26aff6cbec5035d3ebe0d82c9b0019453426bec458c2e8e5f4c2` |
| `docs/experiments/reports/long-horizon-code-6h-results.md` | 12,915 | `0a576dcc22683e80d0d75ff2c5eb4a4baffe41bbc72f892fbbe223196f9d48fc` |
| `docs/experiments/reports/long-horizon-code-phase-a-v1.md` | 8,280 | `ec8583ae3bc853c6d6b8d47cd99965ac63ef1d263f64aa37bf0a59a880234618` |
| `docs/experiments/reports/long-horizon-progress-guard-v1.md` | 3,723 | `bb8f010e8607aafd2c419bb13557b455abce565d256db379b57b3c46fbfa1646` |
| `docs/experiments/reports/long-horizon-public-pilot-v1.md` | 12,431 | `f12d1afbca28ea5e793d509e434d4ad7f8f555cd5cdcff99175b23cca1dcae48` |
| `docs/experiments/results/long-horizon-code-6h-20261115/summary.json` | 5,501 | `009bf23b6d873b1f950fa2d520164ef350946d22e93a1cc6f8e21b532e64748f` |
| `docs/experiments/results/locomo-memory-v3-20261115/summary.json` | 24,101 | `33a77e10319303c5d2bbb6be6a47e907a736a064c7fce9ace0716a772f2bd4d1` |
| `docs/experiments/results/locomo-raw-confirmation-v1-20261115/summary.json` | 8,595 | `4705e048e26cdab83a89bf0922648bad62b5edd773ed2855b01032965a2785c0` |
| `docs/experiments/results/locomo-context-diversity-v1-20261115/summary.json` | 10,404 | `1cad21a113d8e1e716c55335c85b6f3aaef05cfa4d2e227de388928b1f37a7b3` |
| `docs/experiments/results/research-effect-20261114/rsi-compat7-summary.json` | 5,936 | `f5d808789af0050086abc416104e0f3fcb4c6f14ae3be999c3f246fe4a39e61e` |
| `docs/experiments/results/research-agent-20261114/live-summary.json` | 16,289 | `f29ae8ee488955922e2503155d31ae88537c4b6cf265f32ccab667b7146de4d3` |
| `docs/experiments/results/langfuse-live-20261114/verification.json` | 5,628 | `6c76fc6cde2edfa9993450fa0c31a46ee8b9225d53a4999a5cba743cbbd45fde` |
| `docs/experiments/results/swe-v2-pilot-20261112/REPORT.md` | 8,286 | `f90c5b9492068cdc2ff0096108a5f2e83333c69aea62316fff97bc90d8f8587a` |
| `docs/experiments/results/redesign/20261112T095857-2381042b458f/report.md` | 6,595 | `47d8970d7150320f65d67fcec0b88c7561abac4ce64a0390408716bca632bc32` |

## 维护约定

- 新增运行必须保留冻结 plan、manifest、ledger、原始终态和失败分类。
- 新结果不能覆盖本文件登记的历史运行；结论变化以新增 claim ID 或 amendment 记录。
- 若同一机制出现跨任务、重复 seed 或正式配对结果，再提高证据等级。
- README 只引用本文中状态明确、边界完整的主张。
