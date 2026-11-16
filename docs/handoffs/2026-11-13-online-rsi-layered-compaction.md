# 真实RSI闭环、分层压缩与本地GPU记忆

> 执行方式：两名既有GPT-6.1 Sol / medium执行助手，主代理负责设计、集成、验收与统一实验日志。沿用interview-redesign worktree，不迁移L20。

用户已采纳真实RSI优先、三组等预算及随机控制方案，并要求参考所附Claude Code压缩截图、利用本机8GB GPU。起点为`0e9ef15e3368a5ddef64ce536b2d73373d550bf3`。改前Git归档保存在本机私有交付目录，原context-cache基线与既有实验不覆盖。

## 全局约束与验收层级

- 旧账本533请求中1条取消请求usage未知，预留45131微美元保留；本批不得新增付费模型请求、重置账本或重跑旧trial。
- 功能闭环必须经过生产Agent、真实文件工具/可见测试、真实隔离分支与成本计数。scripted模型的trace明确synthetic，不能生产晋升或宣称真实模型质量。
- 模型、工具schema、权限、父请求/输出/token/墙钟/评估预算在对照组中固定；所有分支与摘要/LLM评分共用父约束，子分支不能重发额度。取消/缺usage保留预留并停止。
- 原始canonical消息不被压缩投影改写；tool调用/结果完整，摘要有分支来源与覆盖校验；只在完整已完成前缀做阶段折叠。
- screenshot是设计建议，不是Claude官方实现证明。已核Anthropic官方context-editing文档：清理工具结果会使对应缓存前缀失效，不能把`cache_edits`当DeepSeek/OpenAI已支持接口或承诺KV收益。
- 新代码按路径暂存，保留既有用户修改；只推自己的origin/PR2，不自动合并。模型、私有配置、认证、向量库及环境不入Git。

## P0：生产RSI控制链（SOL-1）

文件所有权：my_coding_agent/agent.py、必要core/agent.py透传、新rsi_runtime.py/rsi_branch.py、专属tests与独立使用说明；不改context/embedding/依赖锁。

- [ ] 在CodingAgent.run增加显式RSI控制器，复用现有核心循环。冻结策略来源/版本/参数；max_width改变实际创建的branch数，max_refine改变同branch继续执行次数。首版串行、独立工作目录/Session/ArtifactStore，文件隔离不宣称OS安全沙箱。
- [ ] 用唯一父RunBudget和SharedLLM代理强制所有真实model请求、stream及摘要计数/截止/预留与usage结算；各child更紧RunLimits只能减少额度。未支持的调用入口明确拒绝，不泄露可绕过的裸provider callable。
- [ ] 每attempt由生产Agent与真实tools执行，可信可见评估器记录动作/评分来源、命令与产物hash、用量和失败，输出Dream兼容已评分树；隐藏最终官方测试在选定产物后独立评分，不回流选择。
- [ ] 选定产物以可审阅patch/文件导出，不污染原repo。候选score、Agent终态、预算/费用完整性和最终成功分开，超时但测试通过不当成功。
- [ ] 离线契约验证：真实工具修改/可见测试→synthetic trace→dry-run参数搜索→冻结候选参数实际控制下一次生产执行。合成数据不写生产active。无真实observed数据时不自动晋升，真实模型效果字段保持未测。
- [ ] 三组配置为A基础上下文固定策略、B分层上下文/产物增强固定策略、C B+冻结RSI；额外B+seeded-random等预算控制。先功能合同，后续有效真实小样本与独立测试集需费用状态可核。

## P1：分层上下文管线（SOL-2）

文件所有权：context.py、新压缩模块、专属tests与独立使用说明。core/agent.py与CodingAgent装配通过SOL-1接线，避免并发覆盖。

- [ ] 复用现有大输出ArtifactStore，只有保存成功且受限回读有效才返回引用。
- [ ] 摘要身份按原canonical先校验，再对旧的完整成功tool组做micro视图替换；保留近期/未闭合调用、错误状态，保存失败保原文，重复prepare稳定。
- [ ] 任意段落的模型语义删除首版不启用；明确unsupported，确定性处理只删可证明冗余或以可回读引用替代，不假装完整复刻五层。
- [ ] 阶段fold只接明确完成的完整前缀，复用现有覆盖指纹/Session摘要。动态视图不用于canonical缓存身份，fold后的下标不用于切原消息。
- [ ] 按实际窗口、输出、摘要与安全reserve计算兜底触发；结构化摘要保目标/约束/决定/已改文件/验证/待办/引用。超预算或不完整摘要不得覆盖有效状态。
- [ ] 每层记录前后规模、来源与前缀重写原因，不发送未支持cache_edits、不编造token/cache收益。

## P2：8GB GPU本地embedding（集成准备由主代理，适配实现另分批交执行助手）

本机实测RTX5060 Laptop，8151MiB，检查时2708MiB已用；项目默认venv尚未安装torch/transformers。使用独立GPU运行环境，避免把CUDA大依赖强加默认安装。

- [ ] 准备官方Qwen/Qwen3-Embedding-0.6B，固定revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`；逐Git blob/LFS核验，weights SHA256 `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`。匿名下载，不读HF token，不执行远端代码。
- [ ] 固定支持sm120的CUDA PyTorch环境；显式GPU0、小batch、长度上限与last-token pooling，查询指令按官方模型约定。运行期只读本地固定缓存，测实际设备、维度、范数、显存/延迟及资源关闭。
- [ ] 接入生产Mem0候选接口；模型变更使用新索引维度/命名空间，不复用旧BGE向量。不存在模型或CUDA不兼容时明确错误，不用CPU/BGE冒充GPU成功。
- [ ] 本地功能smoke与历史诊断集纯检索可0付费执行；与正式回答质量区分，不沿用旧上下文的答案作为新模型答案。新embedding质量不预填提升。

embedding不生成压缩摘要；本批摘要回归使用脚本模型，后续真实摘要调用仍受同一父预算。

## 主代理集成与交付

- [ ] 源/协议/模型准备证据留可复核manifest，检查真实runtime而不只配置。
- [ ] 两助手先行为失败测试再实现，完成后互相只读review；主代理执行关键离线回归、真实本地模型smoke及适当整套检查。
- [ ] 记录功能闭环、机制消融、真实模型效果的不同证据层级；补充实验日志、面试问题与CLAIMS，未测项保留未测。
- [ ] 明确路径提交PR2，核最终CI；保留工作区与失败记录，不自动合并。

来源：用户截图；[Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)、[Claude Code工作原理](https://code.claude.com/docs/en/how-claude-code-works)、[Qwen官方模型](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)。本项目设计不引用外部榜单数字作为自身收益。
