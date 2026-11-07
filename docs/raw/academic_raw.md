# Agent Harness 学术前沿调研（2025-2026）

> 调研目标：为 `my-agent-harness` 的 8 项核心设计（两层 Loop、上下文压缩管线、树状会话存储、三层长期记忆、子代理委派、工具并发控制、事件+钩子）建立「学术对标」坐标系。
>
> 标注约定：
> - **[已发表]** = 有明确会议接收备注（NeurIPS / ICLR / ICML / ACL / EMNLP / FSE / OSDI 等）。
> - **[预印本]** = 仅 arXiv，尚无接收信息。
> - **[经典 2022-2024]** = 奠基性工作，非 2025-2026 新作，但后续所有工作都以其为 baseline。
> - 所有链接均为 arXiv abs 页，可直达 PDF。检索时间基准 2026-11。

---

## 0. 元层面：Agent Harness 本身成为研究对象

2025-2026 最重要的结构性变化：学术界不再只研究「模型 + 提示」，而是把 **harness（运行时脚手架）** 本身当作独立变量与设计对象。这一层与本项目最直接相关。

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *From Question Answering to Task Completion: A Survey on Agent System and Harness Design* (Jianyuan Guo et al., 北大/华为等) | **[预印本]** arXiv 2606.20683, 2026-06 | 提出 agent = 基础模型 + 执行 harness 的分解视角；把 harness 拆成 **六项耦合运行时职责：observation / context / control / action / state / verification**；论证模型中心 scaling 的极限，指出性能瓶颈可能在 harness 而非模型 | 与本项目 7 大模块几乎一一对应：control→两层 Loop、context→压缩管线、state→树状会话存储、action→工具并发控制、verification→钩子中的校验点。**差异**：该综述把 verification 提到一等公民地位，本项目的 14 事件/5 钩子只隐含了 verification，没有独立的「验证阶段」抽象 |
| *Architectural Design Decisions in AI Agent Harnesses* (Hu Wei) | **[预印本]** arXiv 2604.18071, 2026-04 | 对 **70 个公开 agent 项目**做源码级实证研究，归纳出 5 个反复出现的设计维度（subagent 架构、上下文管理、工具系统、安全机制、编排）；结论：语料库偏好 **文件持久化 / 混合 / 分层** 的上下文策略；registry 式工具系统占主导；中间层隔离普遍但高保证审计罕见 | **强证据支持本项目的设计选择**：「文件持久化上下文策略」正是本项目「工具输出落盘」；「分层上下文」对应「结构化摘要」；「中间层隔离普遍」对应子代理上下文隔离。**差异**：该文指出「高保证审计罕见」是本领域共性缺口——本项目的 14 类事件若不做不可篡改审计链，会落进同一缺口 |
| *Scaling Laws for Agent Harnesses via Effective Feedback Compute* (Xuanliang Zhang et al., 哈工大) | **[预印本]** arXiv 2605.29682, 2026-05 | 提出 **EFC（Effective Feedback Compute）**：trace 级 scaling 坐标，只统计「有信息量、有效、非冗余、被保留」的反馈；配 Estimated-EFC / NRS-EFC / harness efficiency η。真实 trace 上 NRS-EFC/D_task 达 R²=0.93，而原始 compute 近乎零拟合；作为伴随控制层把 pass rate 61.2%→68.2%、成本 213.8→85.1 | **这是本项目最该吸收的度量框架**：本项目的「结构化摘要」本质上就是在做 feedback 的「保留/丢弃」决策，但缺一个可度量的目标函数。EFC 给了一个直接可用的评估坐标：不是「压缩了多少 token」，而是「保留了多少有效反馈」 |

### 关键洞察

1. **Harness 已成为一等研究对象**，2026 年出现至少 3 篇以 harness 为标题核心的系统性工作。本项目的 7 项设计不再需要「借用」其他领域的理论，已有直接对应的同类研究。
2. **「文件持久化 + 分层 + 混合」是被实证验证的主流做法**（2604.18071 对 70 个项目的统计），本项目「工具输出落盘 + 结构化摘要 + prefix cache 保护」正是这个三元组合的一个具体实例，且比多数项目多考虑了 cache 维度。
3. **学术界的缺口在「验证」与「审计」**：2606.20683 把 verification 列为六大职责之一，2604.18071 指出高保证审计罕见。本项目的 5 个钩子若仅用于日志，会错失这个学术空白点。
4. **评估坐标从「token 数」转向「有效反馈」**（EFC）。本项目的压缩管线目前若以 token 节省率为 KPI，属于被 2605.29682 明确批评的「raw compute」口径。

### 引用
- [From Question Answering to Task Completion: A Survey on Agent System and Harness Design](https://arxiv.org/abs/2606.20683)
- [Architectural Design Decisions in AI Agent Harnesses](https://arxiv.org/abs/2604.18071)
- [Scaling Laws for Agent Harnesses via Effective Feedback Compute](https://arxiv.org/abs/2605.29682)

---

## 主题 1：Agent 上下文管理 / 压缩

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *ACON: Optimizing Context Compression for Long-horizon LLM Agents* (Minki Kang et al., Microsoft) | **[已发表]** ICML 2026（arXiv 2510.00615，2025-10 首投） | 统一压缩 **observation 与 history**；关键创新是**在自然语言空间做优化**：用 agent 的失败分析迭代精炼「压缩准则」，无需微调模型；再把优化后的 compressor 蒸馏进小模型。AppWorld / OfficeBench / Multi-obj QA 上峰值 token 降 26-54%，任务成功率反升；小模型长程任务提升最多 46% | **最贴近本项目「结构化摘要」的学术对应物**。相同点：都把历史/观察压成自然语言摘要而非 latent 向量。**差异 1**：ACON 的压缩准则是**离线优化产物**（用失败 trace 反推），本项目是**在线固定规则/提示词**，没有失败驱动的准则演进。**差异 2**：ACON 不做 prefix cache 保护；本项目把「压缩会破坏前缀缓存」当作约束，这是 ACON 完全没考虑的工程维度 |
| *AgentFold: Long-Horizon Web Agents with Proactive Context Management* (Rui Ye et al., 阿里/上交) | **[预印本]** arXiv 2510.24699, 2025-10 | 把上下文当「**动态认知工作区**」而非被动日志；每步学习执行 `folding` 操作，支持**粒度级 condensations**（保留细粒度细节）与**深度 consolidations**（把整个多步子任务抽象掉）。仅 SFT，AgentFold-30B-A3B 在 BrowseComp 36.2% / BrowseComp-ZH 47.3%，超过 DeepSeek-V3.1-671B 与 o4-mini | **与本项目「结构化摘要」高度同构**，但 AgentFold 把「摘要」升级为**模型自主决策的多尺度折叠动作**，而不是外部管线在阈值触发时统一压缩。本项目的优势是不依赖特训模型；劣势是压缩时机与粒度都是静态规则，无法应对「该细的地方被抹掉」 |
| *Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks* (Elias Lumer et al.) | **[预印本]** arXiv 2601.06007, 2026-01 | 首个系统评估 **prompt caching 在 agentic 负载下的收益**：跨 OpenAI/Anthropic/Google 三家，比较 full-context / system-prompt-only / **排除动态 tool results** 三种策略。500+ agent session、10k token system prompt：API 成本降 41-80%，TTFT 改善 13-31%。关键结论：**策略性 cache block 控制**（动态内容放尾部、排除动态工具结果）比朴素全量缓存更稳定；**朴素全量缓存反而会拉高延迟** | **本项目「prefix cache 保护」的直接学术背书**，且给出了定量区间。**共鸣**：「排除动态工具结果」= 本项目的「工具输出落盘」。**差异/可补**：论文明确警告「朴素全量缓存会反噬」，本项目的压缩管线若在历史中段插入摘要，会**打断 cache 前缀**——需把摘要做成 append-only 的尾块，而非就地改写 |
| *KVFlow: Efficient Prefix Caching for Accelerating LLM-Based Multi-Agent Workflows* (Zaifeng Pan et al., UCSD) | **[已发表]** NeurIPS 2025（arXiv 2507.07400） | 指出 serving 层 LRU 驱逐对 agent 负载次优；把 agent 执行调度抽象为 **Agent Step Graph**，给每个 agent 一个 **steps-to-execution** 值预测其下次激活时刻，指导 KV node 级细粒度驱逐，并做 CPU→GPU 全重叠预取。相对 SGLang 分层 radix cache：单工作流最高 1.83×，多并发工作流 2.19× | **本项目完全缺失的层次**：本项目只在「请求侧」保护前缀不变，KVFlow 在「服务侧」管理 KV 驱逐。对本项目子代理委派的启示很直接：**子代理与主代理共享前缀时，服务端会把子代理的 KV 当成低优先级驱逐**，多子代理并发场景下 cache 命中率会系统性劣化 |
| *Autoencoding-Free Context Compression for LLMs via Contextual Semantic Anchors (SAC)* | **[预印本]** arXiv 2510.08907, 2025-10（v4 2026-03） | 放弃自编码式压缩 token 训练，改为**直接从原文选 anchor token**，把上下文聚合进这些 anchor 的 KV 表示；两个设计：anchor embedding（标记压缩载体）+ 双向注意力修改（让 anchor 看到全上下文） | **与本项目方法论完全不同路径**：本项目是「文本→文本」压缩，SAC 是「文本→KV」压缩。SAC 的优势是无损于下游任务语义、且压缩率更高；劣势是**要求模型内部可干预**，对本项目这种跑在闭源 API 上的 harness 不可用。列在此处是为了划清边界：本项目选的文本压缩路线是**被 API 边界所迫的正确选择** |
| *LLMLingua* (Huiqiang Jiang et al., Microsoft) | **[已发表]** EMNLP 2023 **[经典 2022-2024]** | **coarse-to-fine** prompt 压缩三段式：budget controller（高压缩比下维持语义完整性）+ token 级迭代压缩算法（建模被压缩内容间的相互依赖）+ 基于 instruction tuning 的分布对齐。GSM8K / BBH / ShareGPT / Arxiv-March23 上最高 20× 压缩几乎无损 | 本项目未采用 token 级压缩。学术上 token 级压缩的问题在于：**破坏可读性、破坏 cache、对代码/结构化输出不友好**。本项目面向代码场景，选择结构化摘要而非 token 级删减，是合理的 |
| *RECOMP: Improving Retrieval-Augmented LMs with Compression and Selective Augmentation* (Fangyuan Xu et al.) | **[已发表]** ICLR 2024 **[经典 2022-2024]** | 两条 compressor：**extractive**（选有用句）+ **abstractive**（跨文档综合摘要）；均训练成「让下游 LM 表现得更好」，可输出空串实现**选择性增强**；压缩率低至 6% | **RECOMP 的「选择性增强/空串」机制值得本项目借鉴**：当检索到的工具输出确实无关时，摘要应当输出「无需保留」而非强行写一段废话——这会直接减少 context rot |
| *MemAgent: Reshaping Long-Context LLM with Multi-Conv RL-based Memory Agent* (Hongli Yu et al., 字节) | **[已发表]** ICLR 2026 **Oral**（arXiv 2507.02259） | 分段读入文本、**overwrite 策略更新记忆**；扩展 DAPO 做独立上下文多轮生成训练。8K 训练 → 32K 外推到 3.5M QA 任务性能损失 <5%，512K RULER 95%+ | 与 MEM1 同属「定长记忆」路线。对本项目的意义：**证明「固定窗口 + 覆盖式更新」在长程任务上可行**，为本项目压缩管线的「保留最近 N 轮原文 + 覆盖式摘要」提供了可行性依据 |
| *MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents* (Zijian Zhou et al., MIT) | **[已发表]** ICLR 2026；NeurIPS 2025 MTI-LLM Workshop Best Paper（arXiv 2506.15841） | 每轮更新一个**紧凑共享内部状态**，同时承担记忆巩固与推理；主动丢弃无关信息使 context 恒定。16 目标 multi-hop QA 上比 Qwen2.5-14B-Instruct 提升 3.5×、内存省 3.7× | 与本项目「结构化摘要」同为「恒定窗口」思路，但 MEM1 的状态是**模型内部表示**，本项目是**外置文本**。外置文本的可审计性更好，这正是 harness 层应有的取舍 |
| *When to Memorize and When to Stop: Gated Recurrent Memory for Long-Context Reasoning (GRU-Mem)* | **[预印本]** arXiv 2602.10560, 2026-02 | 指出 MemAgent 式循环记忆两大缺陷：**无差别更新导致记忆爆炸**、**缺少退出机制导致无效计算**；引入两个文本控制门（update gate / exit gate），用 RL 奖励 r^update、r^exit 训练。相比 MemAgent 推理加速最多 400% | **对本项目的直接启示**：「什么时候压缩」比「怎么压缩」更关键。本项目压缩若由固定 token 阈值触发，等价于 MemAgent 的无门控更新——GRU-Mem 给出了「证据充分即停止」的退出判据思路 |

### 关键洞察

1. **文本空间压缩是 API-only harness 的唯一正解**。ACON / AgentFold / AgentHarness 综述都确认：无需模型内部访问的压缩路径就是「自然语言准则 + 失败驱动优化」。本项目路线正确，缺的是 **ACON 式的失败驱动准则演进**。
2. **压缩与 prefix cache 存在结构性冲突**且已被量化（2601.06007）。本项目「prefix cache 保护」的工程直觉是对的，但学术结论更进一步：**朴素全量缓存会反噬延迟**，必须做「动态内容尾部化 + 排除动态工具结果」的块级控制。
3. **压缩时机 > 压缩率**。GRU-Mem 用 400% 加速证明了「该停就停」的价值；RECOMP 用「空串」证明了「该不压就不压」。本项目的阈值触发式压缩在这一点上落后于 SOTA。
4. **多子代理场景下 cache 命中率会被系统性破坏**（KVFlow）。这是本项目「子代理委派 + prefix cache 保护」两个模块交叉处的**未被考虑的耦合风险**。
5. **学术界的压缩评价坐标已从 token 节省率转向任务成功率 + 有效反馈量**（ACON 反升成功率、EFC 坐标）。

### 引用
- [ACON: Optimizing Context Compression for Long-horizon LLM Agents](https://arxiv.org/abs/2510.00615)
- [AgentFold: Long-Horizon Web Agents with Proactive Context Management](https://arxiv.org/abs/2510.24699)
- [Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks](https://arxiv.org/abs/2601.06007)
- [KVFlow: Efficient Prefix Caching for Accelerating LLM-Based Multi-Agent Workflows](https://arxiv.org/abs/2507.07400)
- [Autoencoding-Free Context Compression for LLMs via Contextual Semantic Anchors](https://arxiv.org/abs/2510.08907)
- [LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models](https://arxiv.org/abs/2310.05736)
- [RECOMP: Improving Retrieval-Augmented LMs with Compression and Selective Augmentation](https://arxiv.org/abs/2310.04408)
- [MemAgent: Reshaping Long-Context LLM with Multi-Conv RL-based Memory Agent](https://arxiv.org/abs/2507.02259)
- [MEM1: Learning to Synergize Memory and Reasoning for Efficient Long-Horizon Agents](https://arxiv.org/abs/2506.15841)
- [When to Memorize and When to Stop: Gated Recurrent Memory for Long-Context Reasoning](https://arxiv.org/abs/2602.10560)

---

## 主题 2：Agent 记忆系统

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *MemGPT: Towards LLMs as Operating Systems* (Charles Packer et al., Berkeley) | **[经典 2022-2024]** arXiv 2310.08560 (2023-10) | 把 OS 的**虚拟内存/分层存储**思想搬到上下文的：主上下文（in-context）+ 外部上下文（外部存储）分层，通过 **function call 自编辑记忆** + **interrupt 控制流**实现「LLM 自己管理内存」 | **本项目「抽取-巩固-检索三层」的学术祖先**。相同点：分层 + LLM 自主决定写入/换出。**差异**：MemGPT 的核心是 **self-editing memory**（LLM 直接调 API 改自己的记忆），本项目的抽取是**管线自动触发**，LLM 不直接持有写权限——后者更可控、更省 token，但缺少 MemGPT 那种「按需精细换页」的灵活性 |
| *Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory* (Prateek Chhikara et al.) | **[预印本]** arXiv 2504.19413, 2025-04 | 明确的三阶段：**extraction → consolidation → retrieval**（与本项目三层命名完全一致）；在图记忆变体下建模对话元素间关系结构。LOCOMO 上 LLM-as-Judge 相对 OpenAI memory 提升 26%，p95 延迟降 91%，token 成本省 90%+ | **本项目三层记忆的直接同名对应物**。Mem0 的 consolidation 是**冲突消解式更新**（新事实覆盖/合并旧事实），这一点值得本项目核对：若本项目的「巩固」只做摘要不做冲突消解，会产生陈旧事实并存。**差异**：Mem0 用向量库/图库，本项目用 Markdown——Mem0 的检索是 embedding 相似度，本项目的 Markdown 检索若靠 grep/关键词，语义召回会明显弱于 SOTA |
| *A-MEM: Agentic Memory for LLM Agents* (Wujiang Xu et al., Rutgers) | **[已发表]** NeurIPS 2025（arXiv 2502.12110） | 借鉴 **Zettelkasten** 卡片盒：新记忆入库时自动生成含**上下文描述、关键词、标签**的结构化 note，再分析历史记忆建立**链接**；支持 **memory evolution**——新记忆可回写更新旧记忆的上下文表示与属性，形成持续自精炼的知识网络 | **这正是本项目「Markdown 存储」应该走的升级路径**：Markdown + 双链（`[[...]]`）+ 结构化 frontmatter（tags/keywords/context）就能复现 A-MEM 的核心机制，无需向量库。**差异**：A-MEM 的链接是 embedding 驱动的，本项目若只用显式 wiki-link，会漏掉语义近邻。折中方案：Markdown 为权威存储 + 轻量向量索引做链接候选 |
| *MemInsight: Autonomous Memory Augmentation for LLM Agents* (Rana Salama et al., Amazon) | **[已发表]** EMNLP 2025 Main（arXiv 2503.21760） | **自主发现语义有意义的属性**（attribute mining），为历史交互生成结构化记忆增广，无需人工定义 schema；检索时按 attribute-based + embedding-based 双路。LLM-REDIAL 推荐说服力 +14%，LoCoMo 检索 recall 比 RAG baseline 高 34% | **对本项目「抽取层」的关键启发**：抽取不应写死 schema，而应**让 LLM 自主发现该记什么属性**。本项目若采用固定 Markdown 模板字段，会把 schema 固化在提示词里，跨任务迁移性弱于 MemInsight |
| *Hierarchical Memory for High-Efficiency Long-Term Reasoning in LLM Agents (H-MEM)* (Haoran Sun, Shaoning Zeng) | **[预印本]** arXiv 2507.22925, 2025-07 | 按**语义抽象程度**分层组织记忆；每个记忆向量带**位置索引编码**指向下一层的子记忆；推理时用**index-based routing** 逐层下钻，避免全库相似度暴力检索。LoCoMo 五个任务设定上稳定超过 5 个 baseline | **本项目「三层」缺失的机制**：H-MEM 的层间是有**指针路由**的，检索是 O(层数) 而非 O(全库)。本项目的三层若只是「原始片段 / 摘要 / 索引文件」的物理分层而无路由指针，检索时仍需全扫。建议在 Markdown frontmatter 中加入 `children: [...]` 形成显式下钻路径 |
| *MIRIX: Multi-Agent Memory System for LLM-Based Agents* (Yu Wang, Xi Chen) | **[预印本]** arXiv 2507.07957, 2025-07 | 六类记忆（Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault）+ 多 agent 框架**动态协调更新与检索**；支持多模态（截图）。ScreenshotVQA 上比 RAG baseline 高 35% 且存储省 99.9%；LOCOMO 85.4% SOTA | **本项目单库三层的对照物**：MIRIX 主张**按记忆类型分库**（情节记忆 vs 语义记忆 vs 程序记忆），因为它们的更新策略与检索方式本质不同。本项目的单一 Markdown 库混合存放三类信息，会导致「巩固」时用同一套策略处理应当区别对待的记忆 |
| *LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory* (Di Wu et al., UCLA/Tencent AI Lab) | **[已发表]** ICLR 2025（arXiv 2410.10813） | 500 题覆盖 **五大记忆能力**：信息抽取、多 session 推理、时间推理、知识更新、**abstention（拒答）**；历史可扩展到 1.5M token。商业助手与长上下文 LLM 在跨 session 记忆上准确率掉 30%。提出记忆设计三阶段框架（indexing / retrieval / reading）+ 三项优化：session decomposition、fact-augmented key expansion、time-aware query expansion | **本项目的首选评测基准**。三点必须注意：(1) 三阶段框架（indexing/retrieval/reading）与本项目三层不完全对齐，本项目的「巩固」在 LongMemEval 里属于 indexing 的子步骤；(2) **abstention 是五项能力之一**——本项目的记忆检索若总是「尽力返回 top-k」，会在 abstention 上系统性失分；(3) fact-augmented key expansion 是低成本高收益的优化，Markdown 场景可直接实现为 frontmatter 中的 `facts:` 字段 |
| *Agent Workflow Memory (AWM)* (Zora Zhiruo Wang et al., CMU) | **[经典 2022-2024]** arXiv 2409.07429 (2024-09) | 从成功轨迹中**归纳可复用的 workflow**（子目标 + 通用动作序列）写入 memory，后续任务直接复用；offline / online 两种归纳方式。WebArena、Mind2Web 上显著提升 | **本项目缺的第四类记忆**：MemGPT/A-MEM 记「事实」，**AWM 记「程序」**。对代码 agent 尤其重要——「这个仓库的测试怎么跑」这类程序性知识应当沉淀为可复用 workflow 而非普通事实 |

### 关键洞察

1. **「抽取-巩固-检索」三阶段划分在学术上有明确对应**（Mem0 同名三阶段；LongMemEval 的三阶段是 indexing/retrieval/reading）。本项目的命名是对的，但学术上更强调「**巩固即冲突消解**」（Mem0）与「**巩固即回写演化**」（A-MEM），不只是压缩。
2. **记忆应按类型分库**（MIRIX：情节/语义/程序/资源）。本项目单一 Markdown 库混装，是架构层面的简化，代价在巩固环节的策略冲突。
3. **层级化必须配指针路由**（H-MEM 的 index-based routing），否则「三层」只是三份文件，检索仍全扫。
4. **评测必须含 abstention**（LongMemEval 五能力之一）。这是最容易被忽略、也最容易暴露「记忆太激进」的维度。
5. **Markdown 存储不是学术落后**：A-MEM 的核心机制（结构化 note + 链接 + 演化）在纯文本下完全可实现；真正的差距在**链接候选的语义召回**与**巩固时的冲突消解**，不在存储介质。
6. **程序性记忆（workflow）被本项目完全遗漏**，对代码 agent 是明显短板。

### 引用
- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413)
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110)
- [MemInsight: Autonomous Memory Augmentation for LLM Agents](https://arxiv.org/abs/2503.21760)
- [Hierarchical Memory for High-Efficiency Long-Term Reasoning in LLM Agents](https://arxiv.org/abs/2507.22925)
- [MIRIX: Multi-Agent Memory System for LLM-Based Agents](https://arxiv.org/abs/2507.07957)
- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813)
- [Agent Workflow Memory](https://arxiv.org/abs/2409.07429)

---

## 主题 3：长程任务与任务分解

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *Why Reasoning Fails to Plan: A Planning-Centric Analysis of Long-Horizon Decision Making in LLM Agents* (Zehong Wang et al., ND/Emory) | **[预印本]** arXiv 2601.22311, 2026-01 | 核心论点：step-wise reasoning 诱导出**逐步贪心策略**，短程够用、长程失效；局部最优导致**早期短视承诺**并随时间被系统放大、难以恢复。提出 **FLARE**（Future-aware Lookahead with Reward Estimation）：显式 lookahead + 价值传播 + 有限承诺，让下游结果能反向影响早期决策。LLaMA-8B + FLARE 常超 GPT-4o 逐步推理 | **对本项目两层 Loop 的理论诊断**：内层 ReAct 闭环本质上就是 step-wise greedy——这正是该文指出的失效根源。**本项目的差异与优势**：外层 Task 队列天然提供了「显式 lookahead」的载体（任务列表 = 未来状态的前瞻表示），比纯 ReAct 更接近 FLARE 的三要素。**可补**：本项目缺「有限承诺（limited commitment）」——即允许在证据不足时回退早期决策，这恰好是树状会话存储能提供的能力 |
| *Reflexion: Language Agents with Verbal Reinforcement Learning* (Noah Shinn et al., Northeastern) | **[经典 2022-2024]** arXiv 2303.11366 (2023-03) | 不更新权重，而用**语言反馈**强化：对任务反馈信号做 verbal reflection，把反思文本存入 **episodic memory buffer**，指导下一次尝试。HumanEval pass@1 91% | 本项目内层闭环未显式包含 reflection。**差异**：Reflexion 的价值在**跨 attempt 累积**（同一任务重试 K 次），本项目的重试若无反思记忆，等价于 K 次独立采样 |
| *ReAct: Synergizing Reasoning and Acting in Language Models* (Shunyu Yao et al.) | **[已发表]** ICLR 2023 **[经典 2022-2024]** arXiv 2210.03629 | 交错生成 reasoning trace 与 action，reasoning 帮助诱导/跟踪/修正行动计划并处理异常，action 获取外部信息。ALFWorld +34%、WebShop +10% 绝对成功率 | **本项目内层闭环的标准实现来源**。学术上 ReAct 已被证明有长程失效问题（见 2601.22311），只作为最小基线保留 |
| *LATS: Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language Models* (Andy Zhou et al., UIUC) | **[已发表]** ICML 2024 **[经典 2022-2024]** arXiv 2310.04406 | 用 **MCTS** 在「推理+行动」联合空间搜索：树搜索探索多条动作序列、真实环境执行、结果评估、自反思作为价值信号 | **本项目树状会话存储的天然搭档但未打通**：树存储提供了分支能力，但内层 Loop 是线性 try 而非树搜索。学术路径是把「分支/回退」从「事后调试功能」升级为「搜索原语」 |
| *Tree of Thoughts: Deliberate Problem Solving with Large Language Models* (Shunyu Yao et al.) | **[已发表]** NeurIPS 2023 **[经典 2022-2024]** arXiv 2305.10601 | 把问题求解表述为对 thought 的树搜索：thought 生成 + 状态评估 + 搜索算法（BFS/DFS） | 纯推理侧树搜索，无环境交互。LATS 是其在 agent 场景的推广 |
| *Demystifying Reinforcement Learning for Long-Horizon Tool-Using Agents: A Comprehensive Recipe* (Xixi Wu et al., CUHK) | **[预印本]** arXiv 2603.21972, 2026-03 | 在 TravelPlanner 上系统分解 agentic RL 的 5 个轴（reward shaping / model scaling / data composition / algorithm / 环境稳定性），得 7 条结论。关键：**小模型需要分阶段 reward 与更强探索，大模型用简单 dense reward 即可**；~1K 训练样本 + 平衡难度是 sweet spot；**环境稳定性对防止策略退化至关重要** | **「环境稳定性至关重要」直接映射本项目的工具并发控制**：若工具执行环境不稳定（并发写冲突、非确定性返回），RL 侧被证明会导致策略退化；对非 RL 的 harness 而言，这同样意味着**不可复现的工具结果会污染 agent 的决策链**。本项目的「非安全工具串行 + 单文件排队锁」正是环境稳定性的实现 |

### 关键洞察

1. **两层 Loop 在学术上有明确的理论支撑**：内层 ReAct = step-wise greedy（已知缺陷），外层 Task 队列 = 显式 lookahead（部分解药）。本项目结构上是对的，缺的是 FLARE 的第三要素「有限承诺」。
2. **长程失效的根因是「早期决策不可撤销」**（2601.22311）。本项目的树状会话存储提供了撤销能力，但它是**会话管理功能**而非**规划原语**——学术 SOTA 的做法是让搜索直接使用分支（LATS），而非事后回退。
3. **reflection 必须跨 attempt 落盘**（Reflexion），否则重试无信息增益。
4. **环境稳定性是被低估的规划前提**（2603.21972）。本项目的并发控制不只是性能设计，也是规划可靠性的前提。
5. **该主题 2025-2026 的直接新作偏少**：多数是 2023-2024 经典工作的延伸；真正的新增理论贡献集中在「为什么长程会失败」的诊断类论文（2601.22311）与 RL 配方类（2603.21972），而非新的规划算法。

### 引用
- [Why Reasoning Fails to Plan: A Planning-Centric Analysis of Long-Horizon Decision Making in LLM Agents](https://arxiv.org/abs/2601.22311)
- [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
- [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)
- [LATS: Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language Models](https://arxiv.org/abs/2310.04406)
- [Tree of Thoughts: Deliberate Problem Solving with Large Language Models](https://arxiv.org/abs/2305.10601)
- [Demystifying Reinforcement Learning for Long-Horizon Tool-Using Agents: A Comprehensive Recipe](https://arxiv.org/abs/2603.21972)

---

## 主题 4：多智能体与子代理

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *Why Do Multi-Agent LLM Systems Fail?* (Mert Cemri et al., Berkeley) | **[预印本]** arXiv 2503.13657, 2025-03（v3 2025-10） | 构建 **MAST-Data**（7 个主流 MAS 框架的 1600+ 标注 trace）与 **MAST 分类法**：14 种失败模式聚成 3 大类——(i) 系统设计问题、(ii) **agent 间失配**、(iii) 任务验证。κ=0.88，并提供 LLM-as-Judge 标注流水线。核心发现：MAS 在主流 benchmark 上的性能增益往往很小 | **本项目子代理委派必读的风险清单**。三类失败中，(ii) 失配与 (iii) 验证缺失是子代理架构的高频雷区。**对本项目的具体映射**：子代理返回结果若无 schema 校验与「验证阶段」，就会命中第三类失败。**关键警告**：该文明确指出多 agent 增益常常很小——子代理委派的收益必须被单独度量，不能假定「分工即提升」 |
| *Multi-Agent Collaboration via Evolving Orchestration* (Yufan Dang et al., 清华/OpenBMB) | **[已发表]** NeurIPS 2025（arXiv 2505.19591） | **puppeteer 范式**：一个中心化 orchestrator（「提线人」）根据任务状态演化动态调度 agent（「木偶」）；orchestrator 用 **RL 训练**来学习序列化与优先级排序。关键分析结论：性能提升**持续来自 orchestrator 演化过程中涌现的更紧凑、更循环的推理结构** | **支持本项目「中心化调度」而非「对等协作」的架构选择**。本项目的 Task 队列 + 子代理 = 静态规则的 puppeteer；学术 SOTA 把这个调度策略变成**可学习对象**。**差异**：本项目的调度是启发式，无法自适应任务复杂度；但 RL 训练 orchestrator 对通用 harness 不现实，可作为长期演进方向 |
| *MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework* (Sirui Hong et al., DeepWisdom) | **[已发表]** ICLR 2024 **[经典 2022-2024]** arXiv 2308.00352 | 把人类 **SOP（标准作业程序）编码进 prompt 序列**，用装配线范式给不同 agent 分配角色，支持中间结果校验以减少级联幻觉 | 本项目子代理委派是**任务级**（Task/subagent），MetaGPT 是**角色级**（PM/架构/开发/测试）。角色级分工对开放式任务更结构化，但开销大；任务级更适合 harness 通用性 |
| *Architectural Design Decisions in AI Agent Harnesses*（同主题 0） | **[预印本]** arXiv 2604.18071 | 实证发现：**「中间层隔离普遍，高保证审计罕见」**；更强的执行环境与更结构化的治理共存；更深的协调与更显式的上下文服务配对 | 「**中间层隔离普遍**」是对本项目子代理**上下文隔离**的直接实证支持：70 个项目中这是主流做法，本项目的设计不是特例而是共性。同时提醒「审计罕见」是我们可差异化的点 |
| *MemInsight / MIRIX*（多 agent 记忆侧，见主题 2） | — | 多 agent 动态协调记忆更新与检索 | 子代理与主代理的记忆共享边界是本领域未解决的问题 |

### 关键洞察

1. **子代理委派的最大收益是上下文隔离，不是并行加速**——这一点在 2025-2026 的实证研究（2604.18071）与 MAST（2503.13657）中反复出现。本项目应当以「主代理上下文的干净度」而非「吞吐」作为子代理的首要 KPI。
2. **多 agent 增益常常很小**（2603.13657 的核心结论），甚至被协调开销吃掉。本项目若在文档中声称子代理提升性能，需要任务级证据。
3. **中心化 orchestration 优于对等协作**（NeurIPS 2025 puppeteer 范式），且学术 SOTA 已把调度策略作为可学习对象。
4. **失败分类法可直接用作本项目的验收清单**：MAST 的 14 种模式（系统设计 / 交互失配 / 任务验证）是一个现成的 checklist。
5. **该主题缺少的直接工作**：「子代理委派的定量收益分析（ablation 式收益归因）」在 2025-2026 尚未检索到专门的实证论文——目前只有综述级论断与失败分析，没有「在同等 token 预算下，子代理 vs 单 agent 的收益曲线」这一类的受控实验。

### 引用
- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)
- [Multi-Agent Collaboration via Evolving Orchestration](https://arxiv.org/abs/2505.19591)
- [MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework](https://arxiv.org/abs/2308.00352)
- [Architectural Design Decisions in AI Agent Harnesses](https://arxiv.org/abs/2604.18071)

---

## 主题 5：工具使用与并发

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *The Evolution of Tool Use in LLM Agents: From Single-Tool Call to Multi-Tool Orchestration* (Haoyuan Xu et al., 哈工大/华为) | **[预印本]** arXiv 2603.22862, 2026-03 | 系统性综述，把问题从「单次调用是否正确」重定位为「长轨迹上的多工具编排」；按 **6 个维度**组织：推理时规划与执行、训练与轨迹构建、**安全与控制**、**资源约束下的效率**、开放环境下的能力完备性、benchmark 设计 | **本项目工具并发控制的定位框架**。「资源约束下的效率」正是并发控制的学术坐标；「安全与控制」对应本项目的只读/非安全工具二分。**差异**：该综述把并发视为「效率」问题，而本项目把「非安全工具串行 + 单文件排队锁」当作**正确性**问题——后者其实更接近该综述提到的「parallel execution 的写操作风险」 |
| *Speculative Actions: A Lossless Framework for Faster Agentic Systems* (Naimeng Ye et al., NYU) | **[预印本]** arXiv 2510.04371, 2025-10 | 借鉴 CPU 推测执行与 speculative decoding：用**快模型预测未来动作并并行执行**，仅在预测命中时提交（lossless）。游戏/电商/网页搜索上下一动作预测准确率最高 55%，延迟降 20%；给出 speculation 宽度与时间收益的 cost-latency 形式化分析 | **本项目并发控制的上位替代思路**：本项目是「等 LLM 给出多个 tool call 再并发执行」，speculative actions 是「LLM 还在生成时就开始执行预测的调用」。**对本项目的直接启示**：只读工具的无副作用特性使它们**天然适合推测执行**——本项目的「只读工具异步并发」可以升级为「只读工具推测预执行」，收益空间比单纯并发更大 |
| *Parallelizing Tool Execution and LLM Generation for Low-Latency Agent Serving (PASTE)* (Yifan Sui et al.) | **[预印本]** arXiv 2603.18897, 2026-03（v3 2026-06） | 从**复现的 agent 模式**中预测具体未来 tool 调用，在 LLM 生成期间推测执行；**隔离推测结果直到 LLM 确认**；联合调度工具执行与 LLM session 回填以避免瓶颈转移到 GPU。deep research / coding / 科学 agent 负载上任务完成时间降 43.5%，工具延迟降 1.8× | **本项目并发控制最直接可借鉴的工程机制**：**「隔离推测结果直到确认」= 事务语义的异或提交**。本项目「单文件排队锁」解决的是「同一文件写冲突」，PASTE 解决的是「推测结果与真实结果冲突」——两者可组合：只读推测 + 写操作串行 + 单文件锁 = 完整的三层隔离 |
| *An LLM Compiler for Parallel Function Calling (LLMCompiler)* (Sehoon Kim et al., Berkeley) | **[已发表]** ICML 2024 **[经典 2022-2024]** arXiv 2312.04511 | 编译式三段：**Function Calling Planner**（生成执行计划 DAG）→ **Task Fetching Unit**（按依赖派发）→ **Executor**（并行执行）。自动推导最优编排。相对 ReAct 延迟降最高 3.7×、成本降 6.7×、准确率升约 9% | **本项目并发控制的理论基座**：把多 tool call 视为**依赖图**而非列表。「只读可并发 / 写必须串行」本质是 DAG 中按副作用类型加边。**差异**：LLMCompiler 让 LLM 出 DAG；本项目让 harness 按工具元数据自动判定并发性——后者更稳健（不依赖模型正确标注依赖），但会漏掉「两个只读工具其实有隐式依赖」的情况 |

### 关键洞察

1. **并发控制的正确定位是「正确性 + 效率」双重问题**，而不仅是效率。本项目按「只读/非安全」二分是正确的抽象，因为它同时编码了副作用语义。
2. **推测执行是并发控制之后的下一个台阶**（PASTE / Speculative Actions）：只读工具天然无副作用，是最安全的推测目标。本项目的「只读工具异步并发」离这一步只差一个「预测 + 隔离 + 确认」层。
3. **LLMCompiler 的 DAG 视角**给了一个形式化语言：把工具调用视为节点、依赖视为边、副作用类型视为加边规则。本项目可用这套语言重述自己的并发策略，从而具备可解释性。
4. **该主题 2025-2026 的新增生要在「serving 系统层」（推测执行、调度）**，而非「工具选择/路由」层。工具路由方面未检索到 2025-2026 的权威新作，多为工业实践报告。

### 引用
- [The Evolution of Tool Use in LLM Agents: From Single-Tool Call to Multi-Tool Orchestration](https://arxiv.org/abs/2603.22862)
- [Speculative Actions: A Lossless Framework for Faster Agentic Systems](https://arxiv.org/abs/2510.04371)
- [Parallelizing Tool Execution and LLM Generation for Low-Latency Agent Serving](https://arxiv.org/abs/2603.18897)
- [An LLM Compiler for Parallel Function Calling](https://arxiv.org/abs/2312.04511)

---

## 主题 6：Agent 评测

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains* (Shunyu Yao et al., Sierra/Princeton) | **[已发表]** arXiv 2406.12045 (2024-06) **[经典 2022-2024]** | 用 LLM 模拟用户与 agent 动态对话，agent 拿到域内 API 与**政策性文档**；评测方式是**对比对话结束时的数据库状态与标注目标状态**（不用 LLM judge 判 pass/fail）。提出 **pass^k** 指标：k 次独立试验**全部**成功的概率。GPT-4o 成功率 <50%，retail 上 pass^8 <25% | **本项目最该采用的可靠性指标**：pass@k 奖励「偶尔蒙对」，pass^k 惩罚「时好时坏」。对 harness 而言，工程目标是稳定性，**pass^k 比 pass@1 更贴合**。**差异**：本项目若只报 pass@1，会掩盖「同一任务三次成功两次失败」这类典型的 harness 抖动 |
| *Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces* (Mike A. Merrill, Alexander G. Shaw et al.) | **[预印本]** arXiv 2601.11868, 2026-01 | Terminal-Bench 2.0：**89 个终端任务**，每个任务有独立环境、人工编写的参考解、完整验证测试；前沿模型与 agent 得分 <65%；附错误分析 | **与本项目形态最匹配的 benchmark**（CLI agent + 工具执行 + 文件系统）。比 SWE-bench 更贴近「harness 能力」而非「补丁正确性」 |
| *Survey on Evaluation of LLM-based Agents* (Asaf Yehudai et al., IBM/MIT) | **[已发表]** ACL Findings（arXiv 2503.16416） | 首个 agent 评测综述，五视角：核心 LLM 能力、应用类 benchmark（web/SWE）、通用 agent、benchmark 核心维度分析、评测框架与工具。指出三大缺口：**成本-效率、安全性、鲁棒性**评估，以及细粒度可扩展评测方法 | **本项目评测体系的设计清单**。三大缺口里「成本-效率」正是 harness 的强项维度（压缩率、cache 命中率、并发度），也是最容易被忽略的评测项 |
| *Benchmarking Reward Hack Detection in Code Environments via Contrastive Analysis (TRACE)* (Darshan Deshpande et al., Patronus AI) | **[预印本]** arXiv 2601.20103, 2026-01 | 提出 **54 类 reward exploit 的分类法**与 **TRACE** benchmark（517 条人工验证轨迹）；关键发现：模型在**对比式异常检测**设定下比孤立分类设定下更能识别 reward hack（GPT-5.2 最高 63% vs 孤立设定 45%）；对**语义上下文型** reward hack 的识别显著弱于语法型 | **对本项目「评测方法论的批判性研究」的核心引用**：agent 评测的最大威胁不是难度不够，而是**reward hacking**（改测试、篡改评测代码、利用任务环境漏洞）。本项目的验收流程若包含「测试通过即成功」，需要配一个对比式异常检测 |
| *LongMemEval* / *SWE-bench Verified* 等 | 见主题 2 / 主题 7 | 见对应章节 | — |
| *WebArena* (Shuyan Zhou et al., CMU) | **[已发表]** ICLR 2024 **[经典 2022-2024]** arXiv 2307.13854 | 自托管四个真实 web 应用（电商/论坛/GitLab/CMS）共 812 任务，功能正确性评测；最强 GPT-4 agent 14.41% vs 人类 78.24% | web 域基线，与本项目代码场景关联较弱 |
| *SWE-bench Verified* (OpenAI × 原始作者) | 2024-08，数据集而非论文 | 从 SWE-bench 人工筛选 500 题，确保问题描述清晰、测试补丁正确、任务在给定信息下可解 | 常用但已被反复质疑（见主题 7 的污染与语义错误问题） |

### 关键洞察

1. **pass^k 优于 pass@k**，尤其对 harness 工程——harness 的价值就在「把不可靠的模型变成可靠的系统」，pass^k 直接度量这件事。τ-bench 的 pass^8 <25% 说明这个指标极难刷，区分度好。
2. **评测的最大威胁是 reward hacking，而不是 benchmark 难度**（TRACE，54 类 exploit）。本项目的验收若依赖单次测试通过，存在被 agent 钻空子的结构性风险。
3. **成本-效率、安全性、鲁棒性是公认的评测缺口**（ACL Findings 综述），而这三点恰恰是 harness 层的主要贡献维度——本项目有充分的「评测空白」可以占据。
4. **CLI/终端类 benchmark（Terminal-Bench 2.0）比 issue 级 benchmark 更贴合 harness 能力评估**：它测的是「在真实环境里完成任务」，而非「产出一个 diff」。
5. **该主题不存在「agent 评测理论」的完整形式化**：pass^k 是目前最接近可靠性理论的指标，但**尚无「harness 级 scaling law / 收益归因」的成熟理论**（2605.29682 的 EFC 是最接近的尝试）。

### 引用
- [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045)
- [Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces](https://arxiv.org/abs/2601.11868)
- [Survey on Evaluation of LLM-based Agents](https://arxiv.org/abs/2503.16416)
- [Benchmarking Reward Hack Detection in Code Environments via Contrastive Analysis](https://arxiv.org/abs/2601.20103)
- [WebArena: A Realistic Web Environment for Building Autonomous Agents](https://arxiv.org/abs/2307.13854)
- [Scaling Laws for Agent Harnesses via Effective Feedback Compute](https://arxiv.org/abs/2605.29682)

---

## 主题 7：代码 Agent 专项

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering* (John Yang et al., Princeton) | **[已发表]** NeurIPS 2024（arXiv 2405.15793） | 核心主张：LM agent 是**新的终端用户类别**，需要专门设计的 **ACI（Agent-Computer Interface）**。自定义 ACI 显著增强 agent 创建/编辑文件、导航仓库、执行测试的能力。SWE-bench pass@1 12.5%，HumanEvalFix 87.7% | **本项目「工具设计」的学术依据**：ACI 不是「把已有 CLI 包一层」，而是**为 agent 认知特点重新设计接口**。**对本项目的具体启示**：(1) 编辑工具应返回 diff + 行号上下文而非全文件；(2) 搜索工具应限制输出行数并提示如何收窄；(3) 报错信息应包含「下一步该怎么做」的提示。这些细节决定 pass@1 |
| *CodePlan: Repository-level Coding using LLMs and Planning* (Ramakrishna Bairi et al., Microsoft Research India) | **[已发表]** FSE 2024 **[经典 2022-2024]** arXiv 2309.12499 | 把仓库级编码表述为**规划问题**：综合出多步编辑链（plan），每步是一次带全仓库上下文的 LLM 调用。三要素：**增量依赖分析** + **变更 may-impact 分析** + **自适应规划**。包迁移（C#）与临时性编辑（Python），2-97 个文件互相依赖；6 个仓库中 5 个通过校验，baseline 0 个通过 | **本项目外层 Task 队列的仓库级对应物**：仓库级任务无法一次塞进 prompt，必须切成有依赖关系的编辑链。**差异**：CodePlan 用**依赖分析**决定任务顺序与上下文；本项目的 Task 队列若只按 LLM 自拟顺序，会失去「变更影响传播」这一关键排序信号 |
| *SWE-smith: Scaling Data for Software Engineering Agents* (John Yang et al., Stanford/Princeton) | **[已发表]** NeurIPS 2025 D&B Spotlight（arXiv 2504.21798） | 给定任意 Python 代码库，**自动构造执行环境并合成 100s-1000s 个打破现有测试的任务实例**。从 128 个仓库生成 50k 实例（比此前工作大一个数量级）；训练 SWE-agent-LM-32B，SWE-bench Verified 40.2% Pass@1，开源模型 SOTA | **本项目的自制评测集方案**：若我在自己代码库上做 harness 评测，SWE-smith 的思路（自动注入 bug 造任务）是避免 benchmark 污染的可行路径 |
| *ProjDevBench: Benchmarking AI Coding Agents on End-to-End Project Development* (Pengrui Lu et al.) | **[预印本]** arXiv 2602.01655, 2026-02 | 从**项目需求**出发评测产出仓库：OJ 测试 + LLM 辅助 code review 双轨，评三项能力——系统架构设计、功能正确性、迭代式方案精炼。20 题 8 类，**总体接受率 27.38%**；结论：agent 能处理基础功能与数据结构，但**系统设计、时间复杂度优化、资源管理**上明显不足 | **补上了 issue 级 benchmark 的盲区**：「从零建项目」比「修一个 bug」更接近 harness 的完整能力（编排、状态、验证都要用到）。27.38% 的低分说明这类任务对 harness 是硬骨头 |
| *Architectural Design Decisions in AI Agent Harnesses*（见主题 0） | **[预印本]** arXiv 2604.18071 | 70 个 agent 项目的架构实证 | 其中代码类 agent 是主要样本来源 |

### 关键洞察

1. **ACI > 模型能力**：SWE-agent 的核心贡献不是算法而是接口设计。本项目若在「工具设计」上投入不足，压缩管线与并发控制做得再好也补不回来。
2. **仓库级任务必须由依赖分析驱动任务切分**（CodePlan），而非 LLM 自由规划。这是本项目 Task 队列最容易改进、也最有学术依据的一点。
3. **issue 级评测已饱和且被污染**（SWE-bench Verified 的语义错误率、模型记忆 PR 编号等问题被多方指出）；**项目级评测（ProjDevBench）是新的区分带**。
4. **自制评测集是被验证的路径**（SWE-smith 式自动构题），比刷公开 benchmark 更能反映 harness 真实能力，也是避开污染的公认防御手段。
5. **该主题 2025-2026 新作集中在「数据规模/评测维度」**，而非 agent 架构本身——架构层面的新思想仍主要来自 SWE-agent(2024) 与 CodePlan(2024)。

### 引用
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793)
- [CodePlan: Repository-level Coding using LLMs and Planning](https://arxiv.org/abs/2309.12499)
- [SWE-smith: Scaling Data for Software Engineering Agents](https://arxiv.org/abs/2504.21798)
- [ProjDevBench: Benchmarking AI Coding Agents on End-to-End Project Development](https://arxiv.org/abs/2602.01655)

---

## 主题 8：Agent 安全与权限

### 代表性工作

| 论文 | 会议/年份 | 核心方法 | 与本项目设计的异同 |
| --- | --- | --- | --- |
| *Defeating Prompt Injections by Design (CaMeL)* (Edoardo Debenedetti, Ilia Shumailov et al., Google DeepMind / ETH) | **[预印本]** arXiv 2503.18813, 2025-03 | 在 LLM 外建保护层：从**可信用户 query** 中显式提取**控制流与数据流**，使不可信数据**永远无法影响程序流**；用 **capability** 概念在工具调用时执行安全策略，阻止私有数据经未授权数据流外泄。AgentDojo 上 77% 任务可证明安全（无防御 84%） | **本项目权限模型的理论上限**：CaMeL 的核心是「**能力（capability）+ 数据流标签**」，比本项目的「只读/非安全工具二分」更严格。**差异**：本项目的二分是**静态工具级**，CaMeL 是**动态数据流级**——同一工具在不同上下文中的危险程度不同。代价是 CaMeL 需要 plan 级双模型架构，工程复杂度高 |
| *Design Patterns for Securing LLM Agents against Prompt Injections* (Luca Beurer-Kellner et al., 14 位作者，含 AgentDojo/Tramèr 团队) | **[预印本]** arXiv 2506.08837, 2025-06 | 提出一组**可证明抗 prompt injection 的设计模式**，系统分析各模式在 utility 与 security 上的权衡，并用案例研究说明工程落地 | **本项目钩子体系的参考模式库**。这些模式（如 plan-then-execute、dual-LLM、code-then-execute、context-minimization 等）多数可直接映射为「在哪个钩子点做哪种拦截」。**对本项目的关键提醒**：14 类事件若只在**事后**记录，无法提供任何可证明安全性——**证明性来自「执行前决策与数据流的隔离」，不来自日志** |
| *AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents* (Edoardo Debenedetti et al., ETH Zurich) | **[已发表]** NeurIPS 2024 D&B（arXiv 2406.13352） | 可扩展环境：97 个真实任务（邮件、网银、预订）+ 629 个安全测试用例 + 多种攻击/防御范式；非静态测试集，支持新任务、新防御、自适应攻击 | **本项目安全评测的标准环境**。其设计哲学（不追求静态通过率，而是支持攻击演化）值得本项目在写安全测试时照搬 |
| *AgentBound: Securing Execution Boundaries of AI Agents* (Christoph Bühler et al.) | **[已发表]** FSE 2026（arXiv 2510.21236） | 首个 **MCP server 访问控制框架**：**Android 权限模型**式声明式策略 + 策略执行引擎，无需修改 MCP server 即可遏制恶意行为。构建 296 个最流行 MCP server 的数据集；**从源码自动生成访问控制策略准确率 80.9%**；阻断多数恶意 server 威胁，开销可忽略 | **本项目「工具权限」的工程范式**：权限不是写在 agent prompt 里，而是**在 server/工具边界上做声明式策略 + 独立执行引擎**。**可直接借鉴**：「从工具源码自动生成权限声明」这条流水线，对本项目「非安全工具」分类的自动化有直接价值 |
| *AEGIS: No Tool Call Left Unchecked — A Pre-Execution Firewall and Audit Layer for AI Agents* (Aojie Yuan et al., USC) | **[预印本]** arXiv 2603.12621, 2026-03（demo paper） | **执行前防火墙**：三段流水线——(i) 工具参数的深度字符串抽取、(ii) **content-first 风险扫描**、(iii) 可组合策略校验；高风险调用可挂起等待人工批准；所有决策记入基于 **Ed25519 签名 + SHA-256 哈希链**的防篡改审计。支持 14 个框架；48 个攻击实例全阻断，500 个正常调用误报 1.2%，1000 次拦截中位延迟仅 +8.3ms | **本项目「事件 + 钩子」缺失的关键一环**：**执行前介入 + 防篡改审计链**。本项目的 14 类事件若全部是观察者（observer），则不构成「防火墙」；学术界明确区分**事后可观测（post-execution observability）无法阻止副作用**与**执行前中介（pre-execution mediation）**。这是本项目可低成本补齐、且学术上已有明确配方的一点 |
| *OpenClaw PRISM: A Zero-Fork, Defense-in-Depth Runtime Security Layer for Tool-Augmented LLM Agents* (Frank Li) | **[预印本]** arXiv 2603.11853, 2026-03 | 零 fork 运行时安全层；**把强制执行分散到 10 个生命周期钩子**：消息入口、prompt 构造、工具执行、工具结果持久化、出站消息、**子代理派生（sub-agent spawning）**、网关启动。混合启发式 + LLM 扫描；**会话级风险累积 + TTL 衰减**；工具/路径/私网/域名分级/出站 secret 模式策略；防篡改审计 + 热重载策略 | **与本项目「14 类事件 + 5 个钩子」最直接的结构对应物**，且给出了钩子应覆盖的完整生命周期清单。**最关键的两点借鉴**：(1) **子代理派生是一个必须设防的钩子点**——本项目有子代理委派但安全边界未提及；(2) **会话级风险累积 + TTL 衰减**——单次调用无害但累积有害，这是本项目完全没有的机制 |
| *Benign in Isolation, Harmful in Composition: Security Risks in Agent Skill Ecosystems (SCR-Bench)* (Yi Xie et al.) | **[预印本]** arXiv 2606.15242, 2026-06 | 提出 **Skill Composition Risk (SCR)**：单独良性的技能在共享执行上下文中组合后可能有害（能力流、信任传递、授权混淆）。三个子基准；组合下 SCR-CapFlow 攻击成功率 33.6%（孤立近 0），SCR-TrustLift 在 5 个后端中 4 个 >96.5% | **对本项目「工具二分」的最强反驳**：**安全属性不是工具级属性，而是路径级属性**。只读工具 A 的输出喂给写工具 B，就可能构成越权。本项目的静态二分无法表达这种组合风险 |
| *ACIArena: Toward Unified Evaluation for Agent Cascading Injection* (Hengyu An et al., 浙大) | **[已发表]** ACL 2026（arXiv 2604.07775） | 系统化 **Agent Cascading Injection (ACI)**：被攻陷 agent 利用 agent 间信任传播恶意指令。覆盖三类攻击面（外部输入 / agent profile / agent 间消息）× 三类目标；6 个 MAS 实现，1356 个测试用例。结论：**仅靠拓扑评估 MAS 鲁棒性不够**，需角色设计与受控交互模式 | **本项目子代理架构的安全空白**：主代理→子代理的委派消息就是一条信任通道。该文证明「防御在简化环境中有效、迁移到真实设置常常失效」，甚至**窄域防御会引入新漏洞** |
| *Classifier Context Rot: Monitor Performance Degrades with Context Length* (Sam Martin, Fabien Roger) | **[预印本]** arXiv 2605.12366, 2026-05 | 用 LLM 做安全监控分类器时，**长 transcript 下更易漏检危险动作**：Opus 4.6/GPT 5.4/Gemini 3.1 在 800K token 良性活动之后出现的危险动作，漏检率是孤立出现时的 **2×-30×**。可用「周期性提醒」部分缓解 | **对本项目安全钩子的直接警告**：若安全审查复用同一个长上下文 session（同一 agent 自己审查自己），**审查能力会随上下文增长而衰减**。正确做法是**独立短上下文的审查器**——这也从安全角度再次论证了子代理上下文隔离的必要性 |

### 关键洞察

1. **安全必须是「执行前中介」，不能是「事后可观测」**（AEGIS 的核心区分）。本项目的 14 类事件若全是观察者模式，安全性基本为零；必须把关键事件改造成**可阻断的拦截点**（pre-execution hooks）。
2. **「只读/非安全」二分是工具级静态属性，已被证明不足以表达组合风险**（SCR-Bench：孤立良性、组合有害，攻击成功率 33.6% vs 近 0）。学术界的方向是**路径级 / 数据流级策略**（CaMeL 的能力 + 数据流标签）。
3. **权限应在工具/server 边界做声明式策略 + 独立执行引擎**（AgentBound，FDE 2026），并且可以**从源码自动推导策略**（80.9% 准确率）。
4. **子代理派生是必须设防的钩子点**（PRISM 的十钩子之一；ACIArena 的级联注入）；本项目的子代理委派目前没有对应的权限收敛机制。
5. **安全审查器必须在短上下文中独立运行**（Classifier Context Rot：长上下文漏检率 2-30×）。这否定了「让主 agent 自己审查自己」的设计。
6. **会话级风险累积 + TTL 衰减**（PRISM）是本项目完全缺失的机制：单次无害的调用组合起来可能构成攻击，需要跨调用的累积计分。

### 引用
- [Defeating Prompt Injections by Design](https://arxiv.org/abs/2503.18813)
- [Design Patterns for Securing LLM Agents against Prompt Injections](https://arxiv.org/abs/2506.08837)
- [AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents](https://arxiv.org/abs/2406.13352)
- [AgentBound: Securing Execution Boundaries of AI Agents](https://arxiv.org/abs/2510.21236)
- [AEGIS: No Tool Call Left Unchecked — A Pre-Execution Firewall and Audit Layer for AI Agents](https://arxiv.org/abs/2603.12621)
- [OpenClaw PRISM: A Zero-Fork, Defense-in-Depth Runtime Security Layer for Tool-Augmented LLM Agents](https://arxiv.org/abs/2603.11853)
- [Benign in Isolation, Harmful in Composition: Security Risks in Agent Skill Ecosystems](https://arxiv.org/abs/2606.15242)
- [ACIArena: Toward Unified Evaluation for Agent Cascading Injection](https://arxiv.org/abs/2604.07775)
- [Classifier Context Rot: Monitor Performance Degrades with Context Length](https://arxiv.org/abs/2605.12366)

---

## 附录 A：本项目 7 项设计 × 学术对应物速查

| 本项目设计 | 学术对应物 | 差距（本项目缺什么） |
| --- | --- | --- |
| 两层 Loop（ReAct 内层 + Task 队列外层） | ReAct(ICLR'23)、FLARE(2601.22311)、CodePlan(FSE'24)、Reflexion | 缺「有限承诺」（可回退早期决策）；缺依赖分析驱动的任务排序 |
| 上下文压缩管线（落盘 + 摘要 + cache 保护） | ACON(ICML'26)、AgentFold、Don't Break the Cache、KVFlow(NeurIPS'25)、RECOMP | 缺失败驱动的压缩准则演进；缺「该不该压」的门控（GRU-Mem）；多子代理场景的 KV 驱逐未考虑 |
| 树状会话存储（Entry 树 + 分支/回退） | LATS(ICML'24)、Tree of Thoughts(NeurIPS'23)、FLARE | 树只是存储能力，未作为搜索/规划原语使用 |
| 长期记忆（抽取-巩固-检索三层，Markdown） | MemGPT、Mem0、A-MEM(NeurIPS'25)、MemInsight(EMNLP'25)、H-MEM、MIRIX、LongMemEval(ICLR'25) | 缺指针式层间路由；缺冲突消解式巩固；缺程序性（workflow）记忆；缺 abstention 能力 |
| 子代理委派（Task/subagent） | MAST(2503.13657)、Evolving Orchestration(NeurIPS'25)、ACIArena(ACL'26) | 缺委派的定量收益归因；缺子代理派生的权限收敛；缺 MAST 式验收清单 |
| 工具并发控制（只读并发 + 非安全串行 + 单文件锁） | LLMCompiler(ICML'24)、PASTE、Speculative Actions | 缺「预测 + 隔离 + 确认」的推测执行层；二分模型无法表达组合安全 |
| 事件驱动 + 生命周期钩子（14 事件 / 5 钩子） | PRISM 十钩子、AEGIS 三段式防火墙、Design Patterns(2506.08837) | 钩子多为观察者，非可阻断中介；缺防篡改审计链；缺会话级风险累积 + TTL 衰减；缺独立短上下文审查器 |

---

## 附录 B：学术稀缺性判断

| 主题 | 2025-2026 学术供给 | 说明 |
| --- | --- | --- |
| 0. Harness 元层 | **中等**，2026 年集中出现（2604.18071 / 2606.20683 / 2605.29682） | 均为 2026 年新作，尚无正式发表；但方向已确立。本项目有直接对标可用 |
| 1. 上下文压缩 | **丰富** | ACON(ICML'26)、AgentFold、KVFlow(NeurIPS'25)、cache 评估均有 2025-2026 新作 |
| 2. Agent 记忆 | **丰富** | A-MEM(NeurIPS'25)、MemInsight(EMNLP'25)、Mem0、H-MEM、MIRIX、LongMemEval(ICLR'25) |
| 3. 长程任务规划 | **偏稀缺（新算法层）** | 2025-2026 主要是**诊断类**（2601.22311）与**RL 配方类**（2603.21972），**新的规划算法**基本没有；核心仍是 ReAct/Reflexion/LATS(2023-2024) |
| 4. 多智能体 / 子代理 | **中等** | MAST、Evolving Orchestration(NeurIPS'25) 有；但**「子代理 vs 单 agent 的受控收益归因」未检索到专门论文**——这是明确的空白 |
| 5. 工具使用与并发 | **中等偏稀缺** | 推测执行（PASTE / Speculative Actions）有货；**「工具并发控制 / 副作用感知调度」作为独立问题几乎无人研究**，LLMCompiler(2024) 之后无直接后续——本项目在此可自居前沿 |
| 6. Agent 评测 | **中等** | τ-bench(pass^k)、Terminal-Bench、评测综述、TRACE 都有；但**「harness 级收益归因 / 消融方法论」稀缺**，EFC 是唯一接近的尝试 |
| 7. 代码 Agent | **偏饱和（评测）/ 稀缺（架构）** | SWE-bench 系列、SWE-smith、ProjDevBench 密集；**新的 agent 架构思想仍停留在 SWE-agent(2024) / CodePlan(2024)** |
| 8. 安全与权限 | **丰富且最新** | CaMeL、Design Patterns、AgentDojo、AgentBound(FSE'26)、AEGIS、PRISM、SCR-Bench、ACIArena(ACL'26) —— 2025-2026 最活跃的方向之一 |

**结论性稀缺清单（写论文/做差异化的机会点）**：
1. **工具并发控制的副作用语义理论**：LLMCompiler 之后无继任者，SCR-Bench 只做了安全侧的路径分析，没有做调度侧的并发正确性分析。
2. **子代理委派的定量收益归因**：只有失败分析（MAST）与架构实证（2604.18071），没有「同等 token 预算下的收益曲线」。
3. **Harness 级 scaling law / 消融方法论**：EFC（2605.29682）是孤例，尚无人提出可复现的 harness 消融范式。
4. **树状会话结构作为规划原语**：LATS 用树做搜索但不在乎会话持久化；harness 侧的树存储（分支/回退）未与搜索理论打通。
