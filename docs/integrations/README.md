# Mem0、Langfuse、Dream-RSI

2026-11-13。三项适配均有用户入口，默认关闭外部服务；本轮只验证代码和离线SDK契约，按用户要求暂停后续SWE及效果benchmark。

## 安装与配置位置

```sh
uv sync --frozen --extra mem0 --extra langfuse --extra dream-rsi
uv run --all-extras python -c "from my_coding_agent.paths import AgentPaths; print(AgentPaths().settings_path)"
```

可只安装需要的extra。Mem0 OSS固定2.2.1/git `f1791daa86d469249c9b002f62644d58a1eb96a0`；Langfuse固定5.2.0；Dream固定0.1.0/git `8ec35bd2538220517ce1bfca4282db21b8c518db`，由uv.lock绑定。

把[配置示例](settings.example.json)的两个块合并到上述命令输出的**全局settings.json**，保留已有其他配置。新用户通常为`~/.my-agent-harness/settings.json`；MY_AGENT_HOME优先，也兼容已有`~/.my-pi-agent`。本机兼容路径为`C:/Users/MLL/.my-pi-agent/settings.json`。

两个块属于全局专属配置，项目`.my-agent-harness/settings.json`不能启用或覆盖它们，包括外发地址和凭据环境变量引用。普通theme等项目覆盖保持。Python显式构造参数仍可注入，适合受信任调用方和测试。

示例默认enabled=false。按需开启对应块后，以`uv run --all-extras my-pi-agent`正常启动；RPC新建、恢复、切换、fork、clone均透传配置。显式启用却缺凭据/SDK或配置无效会给出错误，不能把“配置了enabled”当成服务已接通。

## Mem0：语义候选索引

将[mem0.example.json](mem0.example.json)放在自己控制的全局目录，修改缓存绝对路径；settings的config_file指向该可信文件。示例采用`~/.my-agent-harness/mem0.json`，旧兼容目录用户应改为实际路径。config_file先展开`~`；相对路径按CodingAgent.workspace解析，不按settings文件位置解析。SDK内部路径不另重写，建议使用绝对路径。config与非空config_file互斥。

该示例使用OpenAI embedding，需要独立有效的`OPENAI_API_KEY`及可访问的对应embedding服务；`OPENAI_BASE_URL`影响SDK的OpenAI路径。现有DeepSeek/OpenCode模型凭据不代表支持embedding，不自动继承Harness认证文件。也可以按Mem0官方配置另选依赖已安装的本地embedding provider。建议启动前设置`MEM0_TELEMETRY=false`。

工作流程是：

1. remember仍先写本地facts，精确read/delete/tombstone语义保持。
2. 后续非琐碎检索把已脱敏的当前事实增量送入Mem0，纯文本infer=False，不做第二次事实抽取；查询也经过既有脱敏器并限长。
3. Mem0只提供scope、identity、revision候选。本地再次检查active、删除状态、revision和scope，正文从本地全文取，重复候选去重。
4. 错误、陈旧或无效候选降级为原词法检索。远端删除失败不能复活本地事实。

同步SDK在每实例单daemon worker执行，等待超时后永久熔断，不每轮新增挂起调用。SDK自己的重试/请求可能继续；外层超时不能杀线程。`coding.agent.memory_index.status`提供inflight、circuit_open、closed、sdk_owned/sdk_closed等状态；关闭未完成返回False。SQLite与向量库均是可重建候选缓存，本地facts/tombstone才是权威。

配置工厂创建的index由Agent关闭；显式注入默认caller-owned。`memory_dir=False`优先于全局开关，不创建工厂或远端检索。本轮没有测真实语义召回；既有脱敏规则仍是best-effort，不是所有秘密的完备检测器。

## Langfuse：元数据观测

在本机环境设置自己的`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，settings中只保存环境变量名称；base_url可选为自己的Langfuse部署地址。开启observation后，正常CodingAgent装配官方SDK、有界导出队列并订阅LLM五入口/辅助请求。

原`llm.observer`保持，新增订阅按调用开始时快照；某个observer失败不影响其他observer或模型结果。Runtime仅移除自身订阅、关闭自身client/exporter，不关闭调用方LLM或原observer。切换会话先结束旧Agent在途工作，再关闭旧订阅并重建；旧资源未关完时不会假装切换成功。

默认不发送prompt、工具正文或异常消息，只发送调用关系、模型、状态、数值用量等元数据。完整一致时input=total_tokens-completion_tokens，output=completion_tokens；可能仅表示未缓存部分的prompt_tokens及cache细项保留为数字metadata，不重复计入input。缺失字段不补零。

`coding.close_status`分别记录memory_index、observation、MCP的关闭结果；False表示未完成/失败，None表示未拥有或尚未关闭。Runtime另保留dropped、export_errors、close_errors、is_alive。关闭超时不代表SDK线程已经退出。

本轮真实Langfuse5.2 SDK验证使用内存SpanExporter，已经过生产sink及flush；未向实际Langfuse服务上传，不能据此宣称云端项目已接通。

## Dream-RSI：已评分树的离线回放

```sh
uv run --extra dream-rsi my-agent-dream \
  --trace docs/integrations/dream-trace.example.json \
  --output .my-agent-harness/dream-replay-new \
  --budget 2 --max-workers 2 --timeout-seconds 30
```

输出目录必须不存在。示例是[合成三节点契约输入](dream-trace.example.json)，只验证schema和SDK调用，不是真实任务或策略效果数据。实际输入必须来自外部已评分的v1发现树，包括baseline、parent_id、branch/attempt及evaluated/combined_score等；普通Session记录和未评分的SWE单链不能冒充这类数据。

适配器调用真实上游TraceEnv.replay及官方baseline ParallelRefineMethod，标记fixed_reference；返回summary、decisions及文件hash manifest。核已安装发行版本和固定Git来源，不加载任意候选Python文件。上游OptimalPolicy模板为空实现，本版不用它冒充算法。

Windows使用Job Object，Unix使用进程组；worker归属建立后再运行，限时并清理自己完整进程树。工作进程不继承模型/API凭据且禁网络；进程隔离不宣称为任意代码安全沙箱。

本版接通的是回放与参考策略入口，尚未启动在线探索、自改代码或自动RSI学习；后续策略优化需要真实评分树、单独预算及效果实验。本轮不执行这些benchmark。

## 验证与来源

`tests/integrations/`的三项SDK契约经过生产适配器：Mem0→本地Qdrant/SQLite（假embedding、禁止LLM和网络），Langfuse→内存SpanExporter，Dream CLI→隔离worker→真实固定SDK。它们验证调用、生命周期及数据格式，不能提供语义质量、策略收益或远端服务成绩。

GitHub CI保留四组默认依赖矩阵；Ubuntu/Python3.13另安装全部extras跑SDK契约，wheel检查三个模块和Dream入口。持续记录见[实验日志](../experiments/HARNESS_EXPERIMENT_LOG.md)与[设计/实施计划](../handoffs/2026-11-12-three-integrations-plan.md)。

- Mem0：[官方固定源码](https://github.com/mem0ai/mem0/tree/f1791daa86d469249c9b002f62644d58a1eb96a0)，Apache-2.0。
- Langfuse：[SDK固定源码](https://github.com/langfuse/langfuse-python/tree/00e5098bd6511a2f7bc7b6813ac125a3af559697)，已安装发行元数据为MIT。
- Dream-RSI：[官方固定源码](https://github.com/google-research/dream-rsi/tree/8ec35bd2538220517ce1bfca4282db21b8c518db)，Apache-2.0；作者仓库README指向此代码发布仓库。未运行其第三方任务后端，其他依赖边界以原仓库README为准。
