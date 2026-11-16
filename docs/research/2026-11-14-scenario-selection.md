# 从通用Harness收敛到可验证代码实验场景

2026-11-14；目标岗位沿用Agent／大模型应用开发。本轮核验8个GitHub仓库，2个项目做有界源码审查，其余只核README与仓库元数据。固定版本、核验范围和许可信息见[来源清单](2026-11-14-scenario-source-manifest.json)。没有安装或运行这些项目，新增Agent/benchmark模型调用0，生产源码变更0。以下是待实施提案，不是新场景已经取得的成绩。

## 推荐定位与选择依据

推荐把应用场景收敛为：**面向多轮研发实验的可恢复、可观测、受控自优化Agent Harness**。首版做CPU表格数据上的代码实验：给定训练数据、任务目标和预算，Agent生成/修复训练脚本，宿主运行与评分，积累实验树和已验证经验，保存最佳产物，并从已完成检查点继续。

它保留长程方向，但把“长程”落实为依赖前序实验的多轮工作、跨Session状态和恢复，而不是以总token量或项目名称判断。原单函数校准中test已报告的provider输入峰值不足8k、首解饱和；新场景需要有区分度的连续验证指标，见[原无增益诊断](../experiments/results/rsi-live-20261114/NO_GAIN_ANALYSIS.md)。

选择这条路线的工程推断：现有CodingAgent已能改文件/运行命令、建立分支和评分树；从一个可编辑`train.py`和固定任务清单起步，比同时引入大型仓库构建、浏览器全栈与全量SWE更容易控制适配面。CPU sklearn类任务可能适合本机，实际资源占用仍需预检，不能据此声称8GB上已跑通。

备选是**跨会话的软件交付／仓库维护Agent**：沿用任务清单、进度和Git，以5–10项相互依赖的功能为一个任务，由宿主API/回归测试验收。它更贴近业务开发演示，但多文件任务绑定、服务启动和独立E2E还要补。两条路线先完成一条，不同时扩成两个产品。

## GitHub已有方案，具体借鉴什么

### AIDE：主线代码实验循环

[WecoAI/aideml 固定版](https://github.com/WecoAI/aideml/tree/ad73d227af481bea3de06f884f41b2993dd6453a)提供draft/debug/improve节点搜索、代码执行回调、父子Journal和指标排名。它直接对应我们缺少的有意义任务场景；可以借鉴任务循环和实验树，不替换现有Agent内核。主代理另核根LICENSE为MIT，复用代码保留声明。

所核源码中，指标由反馈模型读终端结果后提取；holdout主要由提示约束。三文件核验不足以确认宿主重算、密封测试、可靠落盘或崩溃恢复。因此我们的最小增量应包括固定宿主评分器、标签隔离与场景检查点，不能直接把模型自报指标作为实验结果。详见[SOL-1源码证据](2026-11-14-aide-source-review.md)。

### autoresearch：把实验边界缩小、固定预算

[karpathy/autoresearch 固定README](https://github.com/karpathy/autoresearch/blob/228791fb499afffb54b46200aca536f79142f117/README.md)把可变代码、固定数据准备/评价与人类实验协议分开，按固定训练时间比较验证指标并保留/放弃修改。这里可借鉴“一份可编辑脚本、一个可信指标、一个统一预算”的边界。

原版是NVIDIA单GPU nanochat训练、文档称在H100测试，不是本机8GB的已验证方案。本轮只核README和根目录；先将协议用于小型CPU代码实验，不部署原版深度训练。README声明MIT，未在所列根目录看到单独LICENSE文件，不进行源码移植。

### Anthropic autonomous-coding：跨会话任务协议

[官方示例固定目录](https://github.com/anthropics/claude-quickstarts/tree/4a4224bdf0e9cfb3332167b69c90457403813625/autonomous-coding)区分initializer/coding职责，以功能清单、进度与Git向新会话交接。适合备选的软件交付场景，也可给实验任务增加初始化/执行分工。

所核host按文件存在续接、每轮建新SDK上下文；flag与进度主要由Agent按prompt写，未见独立宿主验收/全通过停止门。使用Claude SDK，不能只改DeepSeek模型名就视为迁移。根LICENSE是MIT，子README另写Internal Anthropic use，许可范围未在本轮解释；先借鉴协议，不直接复制这部分实现。见[SOL-2源码证据](2026-11-14-long-running-source-review.md)。

### Ralph：轻量外部循环

[snarktank/ralph 固定README](https://github.com/snarktank/ralph/blob/6c53cb0b831ebe8739c6a003e22af14902d8b0b5/README.md)以新的Agent上下文执行一个任务项，通过prd.json、progress.txt和Git保留状态。可借鉴外置任务状态和逐项推进，首版可保持CLI，不先做复杂UI。本轮未核其shell/prompt执行源码，也不将README里的自动流程当作独立验收证明。

### Hermes与DeepAgents：补充机制，不迁移框架

[Hermes固定README](https://github.com/NousResearch/hermes-agent/blob/4b40ffb0ad6d7da5d3260f5e499e1691508dd4d5/README.md)描述经验沉淀、skills与历史检索，可借鉴“复用解决过程”，而不只存聊天摘要；本轮未核技能学习实现，也不引用其产品宣传作为我们已有收益。

[DeepAgents固定README](https://github.com/langchain-ai/deepagents/blob/859257b0f538cfa99b59bb9e5341e162d4ac8433/README.md)列出文件后端、上下文offload、持久化与委派。与我们已有组件重叠，可参考接口；当前不引入LangGraph/LangChain迁移成本。

### 数据和benchmark候选

[PMLB固定README](https://github.com/EpistasisLab/pmlb/blob/7c1f4bdc00136dc2e55c87fa6b8ba6e8af6d1a68/README.md)提供分类/回归表格数据与下载缓存接口，可作为CPU任务来源；具体数据的来源、许可、大小和split仍需逐项核验，本轮未下载数据。

[MLGym固定版](https://github.com/facebookresearch/MLGym/tree/9d40c1b5035202018cd7091fb4e83a9c68b377c0)可参考研究任务的环境/评价协议。当前仓库已归档，README标签为CC-BY-NC-4.0，且有容器/GPU配置；不选为这次快速实现的基础，也不冒称已运行官方benchmark。

## 三个既定项目如何成为同一个场景的能力

- **Mem0：检索经验证的实验经验（计划）。** 保存错误类型、环境/数据版本、有效修复、前后指标和证据hash；下次草稿/调试前检索。当前任务进度、预算和最终分数由宿主确定性存储负责，Mem0不是状态真值。保留none/词法/Mem0对照，不预设向量检索必胜。
- **Langfuse：可追踪的执行证据。** 关联task→run→branch→model/tool→score，记录tokens、耗时、错误、产物hash与恢复事件。现有SDK适配可复用；真实服务上传/回读仍要单独验，服务凭据与可用性未核。不能把离线SpanExporter当已接云端，也不把观测工具当质量优化算法。
- **Dream-RSI：预算内探索策略。** 用真实评分树选择受控探索/细化参数，先修有效候选覆盖和质量/成本目标，再以完整独立验证决定冻结策略。早停若加入，固定/随机对照同样具备该动作。首版保持受控参数范围，不声称训练DeepSeek权重或自改任意策略程序。

这样三者分别服务经验复用、过程测量和搜索决策。场景级checkpoint和可信评分属于Harness自身，不能期待某个SDK代替。

## 最小可运行场景

任务示例：给定一份公开表格训练数据、schema与预算，自动适配预处理/训练脚本，修复执行错误，比较候选，在独立最终评测上交付最佳脚本与可复现实验记录。

流程：任务清单与数据版本冻结 → 生成草稿 → 受限执行 → 宿主评分 → 形成父子节点 → 调试/改进/停止 → 已完成边界检查点 → 独立最终评测与导出。

首版只允许修改任务脚本和声明过的配置；数据划分、评分器、最终标签与预算不在Agent可修改范围内。评分器根据预测重算指标，不接受终端打印的“最好分数”，也不把候选对象以不可信pickle加载到宿主。操作系统隔离的实际程度需明确验收。

任务应包含数据schema适配、预处理和代码错误处理；仅调几个数字超参数时，固定脚本/随机或Optuna类搜索也应作为合理基线，不能把Agent称为新的AutoML算法。

### 现有代码的真实接缝

本轮两文件只读核验确认，当前Runtime[强制检查VisibleCommandEvaluator实例](../../src/my_coding_agent/rsi_runtime.py#L309)，[默认评分](../../src/my_coding_agent/rsi_branch.py#L112)只是`float(exit_code==0)`，不是通用数值评分协议。可复用预算、分支副本、评估调用、选优和导出；不能直接声称已有ML数值grader。

必须补三处：

1. 数值评分入口与feedback schema，宿主校验预测并重算utility；同时把当前`score==1`的通过计数与有效执行/质量指标分开。
2. 固定数据/split/seed的任务adapter及候选写入范围。当前单文件证据只核baseline，export导出整棵候选树，不能把它当作已经强制只改train.py；预测产物与可变代码也需分开管理。
3. 场景级checkpoint，绑定代码/数据/预测/评分器/预算并校验加载。当前Runtime[明确single-use](../../src/my_coding_agent/rsi_runtime.py#L371)，保存Session或Journal不等于已经恢复整个实验工作流。

这三处是拟新增范围，未在本轮实现。CPU/内存/线程限制与实际隔离程度也必须预检，不用“文件放在另一目录”替代权限隔离。

### 两层数据划分与经验范围

每个任务内部划分train/dev/final-test，用train拟合、dev比较候选、final-test只验选定产物；任务集合另外划分策略train/holdout/test，用于选择和验证Dream-RSI参数。两层划分独立冻结，不混用名称或反馈。

首轮正式对照只从策略train任务写入经验库；holdout/test仅读同一冻结知识快照，最终标签、final分数和test trace不得回流检索或选参。持续学习演示可以另定先评后学协议，分开报告，不能与独立泛化成绩混算。

## 三个交付批次

1. **任务与可信评分切片。** 选少量可下载、许可清楚、CPU资源可控的数据，固定hash/split/seed；先跑固定基线，确保不同合法方案有可测差异，再接一个真实Agent实验。交付脚本、预测、宿主分数及真实用量。尚未满足预检，不宣称本机已跑通。
2. **有状态实验工作流。** 接已完成检查点续接、Mem0经验读写和Langfuse场景元数据；用现有Agent/Session/Artifact/预算接缝，不重写循环。先验安全边界恢复和trace关联，再扩大轮数；在途API未知仍保留成本并停止，不承诺exactly-once。
3. **受控策略与场景对照。** 修正候选去重/覆盖、明确质量与真实成本目标，生成train评分树，完成独立holdout后冻结策略；对照贪心/固定/随机与冻结RSI。此前学习策略未产生新干预，本阶段不能复用旧HumanEval test调参后声称泛化。

这是按交付物排序的计划，不是工期承诺。首轮以一个完整可运行任务和少量数据为目标，之后扩到多个独立任务；长程收益必须由连续阶段、依赖、恢复和预算数据证明。

## 应采集的实验指标

- 任务质量：独立最终指标、有效实验/总实验、错误预测/伪完成次数；不同数据指标先规定归一化，测试反馈不回流选参。
- 搜索效率：best-score@相同请求/token/CPU预算、达到质量门槛的成本；不是谁用了更多请求就占优。
- 记忆价值：none/词法/Mem0的证据召回、重复错误、首次有效方案成本和最终质量，计算写入/检索附加成本。
- 恢复可靠性：已完成状态是否一致、重复执行、额外token、丢失/不确定费用；与现有Session续接比较，不虚构完全无持久化的弱对照。
- 观测可靠性：trace覆盖、score与artifact可追溯率、导出失败隔离；真实服务证据与本地记录分开。
- 上下文压力：单链长度与峰值、micro/fold的记录视图、摘要请求、回读调用和成功读取分别计量，累计tokens不等于上下文长度。

## 简历与面试应突出什么

现有可核验部分：真实CodingAgent分支执行、独立评分与冻结参数消费；共享预算、失败记账、产物/上下文和记忆/观测适配。48测试槽的原结果保持47通过/1权限失败，不包装成RSI或长程提升。

完成新场景后，项目介绍可围绕“可信评价、可恢复执行、可验证经验复用、同预算策略选择”组织。候选项目标题为“面向多轮研发实验的Agent Harness”；个人贡献按实际参与和源码掌握程度确认，不使用独立完成全部框架的说法。

新增场景、真实Langfuse服务链路、Mem0场景收益、RSI收益均处于待实现/待验证状态，当前不进入已证实简历指标。优先形成可演示工作流、版本化证据和能解释的实验结果，再精炼项目经历。
