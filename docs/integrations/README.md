# Mem0、Langfuse、Dream-RSI

2026-11-13。三项适配均有用户入口。本轮接通串行 RSI 执行链：真实 CodingAgent 分支与本地评分 → trace → 参数搜索 → 冻结策略 → 下一轮真实分支。演练使用脚本模型，质量未测、未生产晋升。新增本地 Qwen3 GPU embedding，已验证功能，尚无召回提升结论。原 Memory30题负结果与 Terminal 停止记录保留。

## 安装与配置位置

```sh
uv sync --frozen --extra mem0-local --extra langfuse --extra dream-rsi
uv run --all-extras python -c "from my_coding_agent.paths import AgentPaths; print(AgentPaths().settings_path)"
```

可只安装需要的extra。`mem0`只安装SDK，`mem0-local`另安装FastEmbed0.9.1及CPU ONNX Runtime；SDK已安装与运行时索引开关是两件事。Mem0固定2.2.1/git `f1791daa86d469249c9b002f62644d58a1eb96a0`；Langfuse固定5.2.0；Dream固定0.1.0/git `8ec35bd2538220517ce1bfca4282db21b8c518db`，均由uv.lock绑定。

把[配置示例](settings.example.json)中需要的块合并到上述命令输出的**全局settings.json**，保留已有其他配置。新用户通常为`~/.my-agent-harness/settings.json`；MY_AGENT_HOME优先，也兼容已有`~/.my-pi-agent`。本机兼容路径为`C:/Users/MLL/.my-pi-agent/settings.json`，现有进程需重启以读新配置。

memory_index、observation、dream_optimization、context_compaction 属于全局专属配置，项目 settings 不能启用或覆盖它们；普通 theme 等项目覆盖保持。Python 显式参数仍可注入，适合受信任调用方和测试。

分发示例默认 enabled=false。本机本轮改为 local_qwen_gpu 与 layered 压缩，原 CPU profile 和配置备份保留。新配置需要本分支代码，请从 `interview-redesign` 工作区启动；旧 checkout 尚不识别 GPU profile，切回旧版本应恢复 CPU 配置。Dream 全局入口原值保留，不自动启用 RSI；首版 RSI 是显式批任务 API，TUI 流式入口暂不支持。其他用户配置后可用 `uv run --all-extras my-pi-agent` 启动；缺 SDK、模型或无效配置会报错。

## Mem0：语义候选索引

### 本机配置：本地CPU中文模型

```sh
uv run --extra mem0-local python -c "from pathlib import Path; from my_agent_core.memory_local import prepare_model_cache; prepare_model_cache(Path.home()/'.cache/my-agent-harness/models/bge-small-zh-v1.5/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59')"
```

将[mem0-local.example.json](mem0-local.example.json)放到可信全局目录，令memory_index.config_file指向它，再设enabled=true。此四字段profile由Harness解释，不能直接当成Mem0原生SDK配置；也可用`local_cpu_config(model_dir=..., storage_dir=..., threads=2)`生成绝对路径配置。

固定BGE-small-zh-v1.5：中文、512维、512-token窗口，模型MIT；官方repo Qdrant/bge-small-zh-v1.5、revision `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。7文件合计95332206B，ONNX SHA256为`1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38`；全部文件在准备及运行期均重验官方独立hash，坏缓存不覆盖、不自动换模型。

显式CPUExecutionProvider、cuda=False、2线程、只用本地路径；NeverLLM禁止二次抽取，不构造OpenAI客户端、不读取Harness模型认证。模型只读共享，SQLite/Qdrant为每adapter独占可重建缓存，关闭只清理自己目录并释放模型强引用。冷构造会同步加载模型，不在reconcile/search超时内；profile示例10秒只约束后续工作等待，不能杀SDK线程。

原本机 CPU 配置及模型保留；该版本的真实链路功能检查曾临时禁止词法回退并拒绝 socket 连接。这是功能检查，不能据此给正确率或性能提升。

### 本轮：本地 Qwen3 GPU 模型

官方 `Qwen/Qwen3-Embedding-0.6B` 固定 revision，独立 Torch/CUDA Python，主应用无需 Torch。RTX 5060 Laptop 的真实中英文检索、1024维归一化、Settings → RPC → CodingAgent 装配和进程回收已验证；无付费调用。配置见[本地 Qwen](../local-qwen-memory.md)，证据见[两事实功能检查](../experiments/results/context-rsi-20261113/qwen-gpu/REPORT.md)。

新 profile 显式禁用 owned SDK 的 BM25/NLP 懒加载，属于 dense-only 派生配置。追加更正：原 BGE profile 含可选 BM25/NLP 路径，旧30题没有记录是否实际触发；不能无条件称旧臂纯稠密，未来 Qwen/BGE 差异也不能全归于 embedding。旧数值不改，单因素对照须先固定这些行为。

### 其他可选embedding服务

如另选外部embedding，将[mem0.example.json](mem0.example.json)放在自己控制的全局目录并修改缓存绝对路径，再令config_file指向该文件。config_file先展开`~`；相对路径按CodingAgent.workspace解析，不按settings文件位置解析。原生SDK内部路径不另重写，建议用绝对路径；非空config与config_file互斥。本机现采用本地profile，不需要此外部配置。

该示例使用OpenAI embedding，需要独立有效的`OPENAI_API_KEY`及可访问的对应embedding服务；`OPENAI_BASE_URL`影响SDK的OpenAI路径。现有DeepSeek/OpenCode模型凭据不代表支持embedding，不自动继承Harness认证文件。也可以按Mem0官方配置另选依赖已安装的本地embedding provider。建议启动前设置`MEM0_TELEMETRY=false`。

工作流程是：

1. remember仍先写本地facts，精确read/delete/tombstone语义保持。
2. 后续非琐碎检索把已脱敏的当前事实增量送入Mem0，纯文本infer=False，不做第二次事实抽取；查询也经过既有脱敏器并限长。
3. Mem0只提供scope、identity、revision候选。本地再次检查active、删除状态、revision和scope，正文从本地全文取，重复候选去重。
4. 错误、陈旧或无效候选降级为原词法检索。远端删除失败不能复活本地事实。

同步SDK在每实例单daemon worker执行，等待超时后永久熔断，不每轮新增挂起调用。SDK自己的重试/请求可能继续；外层超时不能杀线程。`coding.agent.memory_index.status`提供inflight、circuit_open、closed、sdk_owned/sdk_closed等状态；关闭未完成返回False。SQLite与向量库均是可重建候选缓存，本地facts/tombstone才是权威。

配置工厂创建的index由Agent关闭；显式注入默认caller-owned。`memory_dir=False`优先于全局开关，不创建工厂或远端检索。适配初版未测质量；后续固定英文历史30题实验已经完成，Mem015/30、词法22/29有效（另1判分不可用），详见[误差分解](../experiments/results/long-horizon-20261113/memory-error-analysis/REPORT.md)。这不是自动事实抽取/巩固的质量评估，也不证明中文任务效果。既有脱敏规则仍是best-effort，不是所有秘密的完备检测器。

## Langfuse：元数据观测

在本机环境设置自己的`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，settings中只保存环境变量名称；base_url可选为自己的Langfuse部署地址。开启observation后，正常CodingAgent装配官方SDK、有界导出队列并订阅LLM五入口/辅助请求。

原`llm.observer`保持，新增订阅按调用开始时快照；某个observer失败不影响其他observer或模型结果。Runtime仅移除自身订阅、关闭自身client/exporter，不关闭调用方LLM或原observer。切换会话先结束旧Agent在途工作，再关闭旧订阅并重建；旧资源未关完时不会假装切换成功。

默认不发送prompt、工具正文或异常消息，只发送调用关系、模型、状态、数值用量等元数据。完整一致时input=total_tokens-completion_tokens，output=completion_tokens；可能仅表示未缓存部分的prompt_tokens及cache细项保留为数字metadata，不重复计入input。缺失字段不补零。

`coding.close_status`分别记录memory_index、observation、MCP的关闭结果；False表示未完成/失败，None表示未拥有或尚未关闭。Runtime另保留dropped、export_errors、close_errors、is_alive。关闭超时不代表SDK线程已经退出。

本轮真实Langfuse5.2 SDK验证使用内存SpanExporter，已经过生产sink及flush；未向实际Langfuse服务上传，不能据此宣称云端项目已接通。

## Dream-RSI：受控参数自动优化

自动化闭环为“冻结候选→train选参→holdout验收→原子晋升→回放消费active→可回滚”。候选只有max_width(1..64)、max_refine(0..64)两个参数，不生成或执行任意策略Python，也不调用额外LLM。执行器和评分器保持固定；不是上游完整任意代码RSI流程或模型权重训练。

历史“消费 active”仅指离线 replay。本轮新增 [RSI runtime](../rsi-runtime.md)：`CodingAgent(..., rsi_runtime=runtime).run(goal)` 经现有 core loop 执行分支、运行 host 可见测试并导出评分树；`FrozenPolicy.from_registry` 在下一 episode 开始时读取 active，宽度/继续尝试次数实际控制执行。无 active 明确失败，不在运行中热切换。

请求、摘要、评分共享父预算；独立 final 在选定产物后执行，反馈不参与搜索/训练。分支独立目录、Session、ArtifactStore，目录隔离非 OS 沙箱。当前串行、显式启用、不自动应用文件或晋升，不包含任意代码自改或权重训练。脚本模型四臂演练见[证据](../experiments/results/context-rsi-20261113/rsi-contract/REPORT.md)，真实模型收益待验证。

```sh
uv run --extra dream-rsi my-agent-dream status
uv run --extra dream-rsi my-agent-dream optimize \
  --dataset-manifest docs/integrations/dream-demo/dataset.json \
  --output .my-agent-harness/dream-demo-new \
  --dry-run --budgets 2 --max-candidates 2 --max-seconds 60
```

[演练数据](dream-demo/dataset.json)明确kind=synthetic，含独立train/holdout完整小树；只验证功能，不能生产晋升。初版没有配置真实数据集时返回needs_dataset，不创建优化目录或假造改进。后续研究campaign已产生真实树，但没有自动配置全局数据集或发布active；研究输出与`my-agent-dream status`的全局配置状态分别查看。

真实manifest用kind=observed、source、entries；每条entry具有id/task_id/split/file/sha256/provenance，相对路径受限。至少各一完整train/holdout树，拒绝同任务跨split（改文件名、时间戳、分支编号也不能绕过）。带可重算公开输入内容证据时，不同输入允许出现相同分数曲线；legacy/mixed数据仍用保守规则，见[任务内容身份](../rsi-task-evidence.md)。observed不是评分真实性的密码学证明；Session和SWE单链不能补造成真实树。

候选网格在结果前固定，只用train选择一个。holdout只验该候选、完整shape baseline和冻结incumbent，不能拿它再选其他候选。指标是有限步数下best/已记录正数上界的归一化均值，不是论文reward、任务成功率或线上收益。可信动作/成本日志重算，TraceEnv初始best为0，不把baseline得分伪造为已经达到。

晋升要求完整、observed且候选严格胜过baseline和当前active，并满足min_gain。指针锁内重验完整性和CAS token，版本/参数/registry目标不匹配、期间其他晋升或rollback、ABA、超时或失败都不能覆盖旧active。不可变版本及previous支持`rollback`；合法active由正常replay实际消费，坏版本明确退回baseline。

默认最多16候选、32树、4个步数预算、300秒总执行时间；单worker不超过剩余时间。budgets表示回放探索步数，不是API费。Settings.dream_optimization提供全局默认，显式CLI优先；enabled控制默认配置入口，不阻止受信任显式单次命令，也不会自动撤销已晋升active。没有后台定时收费或未知模型提案。

### 正常回放与回滚

```sh
uv run --extra dream-rsi my-agent-dream replay \
  --trace docs/integrations/dream-trace.example.json \
  --output .my-agent-harness/dream-replay-new \
  --budget 2 --max-workers 2 --timeout-seconds 30
```

输出目录必须不存在。示例是[合成三节点契约输入](dream-trace.example.json)，只验证schema和SDK调用，不是真实任务或策略效果数据。实际输入必须来自外部已评分的v1发现树，包括baseline、parent_id、branch/attempt及evaluated/combined_score等；普通Session记录和未评分的SWE单链不能冒充这类数据。

没有active时调用真实上游TraceEnv.replay及完整baseline，标记fixed_reference；有合法active时将其受控参数送入同一官方策略类，标记controlled_parameters并记录版本/hash、requested/effective参数。核固定SDK来源，不加载任意候选Python；上游OptimalPolicy空模板未使用。旧无子命令的--trace用法保持兼容。

`my-agent-dream rollback` 回退到已验证 previous；没有 previous 明确失败而不修改状态。CLI 可显式 --registry 选择可信目录。回放与真实 runtime 都可在开始时冻结受控参数，已开始的 episode 保持原版本。尚无真实模型策略晋升成绩。

Windows使用Job Object，Unix使用进程组；worker归属建立后再运行，限时并清理自己完整进程树。工作进程不继承模型/API凭据且禁网络；进程隔离不宣称为任意代码安全沙箱。

主执行超时后仍有独立的退出确认窗口。Unix发送kill后会有界确认组内没有非zombie成员，5秒无法确认时明确报错，不把“信号已排队”当成“整组已退出”。

离线选参、验证、晋升/回滚与显式在线分支执行链已实现。[真实HumanEval短代码校准](../experiments/results/rsi-live-20261114/REPORT.md)48测试槽已到终态：47正常交付且final通过，1权限失败、final未执行。训练树可见分数饱和，SDK选择baseline；holdout覆盖不完整，未晋升，C为与B同参数的reference fallback，不能作为学习干预。原失败的一个schema指纹缺失，完整比较未生成排名。既有Memory/Terminal不算Dream消融；四臂方案与实际限制见[实验协议](../experiments/RSI_CLOSED_LOOP_EVALUATION.md)和[持续记录](../experiments/HARNESS_EXPERIMENT_LOG.md)。

## 验证与来源

`tests/integrations/`经过生产适配器：早期Mem0为本地Qdrant/SQLite+假embedding，新增local测试包括真实BGE CPU功能（设MY_AGENT_LOCAL_MODEL_DIR后运行，测试不下载）；Langfuse为内存SpanExporter；Dream为真实固定SDK的参数搜索/active消费与损坏回退。它们不提供召回消融、真实策略收益或云端成绩。依赖缺失的Linux环境已单独验证默认模块导入与Settings，分发不强制下载模型。

GitHub CI保留四组默认依赖矩阵；Ubuntu/Python3.13另安装全部extras跑SDK契约，wheel检查三个模块和Dream入口。持续记录见[实验日志](../experiments/HARNESS_EXPERIMENT_LOG.md)与[设计/实施计划](../handoffs/2026-11-12-three-integrations-plan.md)。

本轮实施范围另见[本地记忆/受控优化计划](../handoffs/2026-11-13-local-memory-controlled-optimization.md)。本机全局配置已merge-only更新并保留原文件备份，模型/向量缓存和配置备份不进Git；真实数据缺失时没有生产策略晋升。

- Mem0：[官方固定源码](https://github.com/mem0ai/mem0/tree/f1791daa86d469249c9b002f62644d58a1eb96a0)，Apache-2.0。
- Langfuse：[SDK固定源码](https://github.com/langfuse/langfuse-python/tree/00e5098bd6511a2f7bc7b6813ac125a3af559697)，已安装发行元数据为MIT。
- Dream-RSI：[官方固定源码](https://github.com/google-research/dream-rsi/tree/8ec35bd2538220517ce1bfca4282db21b8c518db)，Apache-2.0；作者仓库README指向此代码发布仓库。未运行其第三方任务后端，其他依赖边界以原仓库README为准。
