# Mem0、Langfuse、Dream-RSI 适配计划

> **For agentic workers:** 使用 subagent-driven-development；仅复用 SOL-1、SOL-2，不自行派生。按独立职责实现、交叉复核，主代理集成与验收。

**Goal:** 让三个项目具有可安装、可配置、可调用的适配入口，暂停后续SWE及效果benchmark。

**Architecture:** 本地事实继续为记忆权威，Mem0提供可选候选索引；LLM门面向Langfuse提供元数据订阅；Dream-RSI以独立离线CLI读取真实评分树并调用官方参考策略。默认不启用外部服务，调用方注入的资源和工厂创建的资源分别管理所有权。

**Tech Stack:** Python≥3.11、现有Settings/RPC、可选Mem0 OSS SDK、Langfuse v5 SDK、Dream-RSI trace/replay包。沿用用户已经确定的三项职责，接口细节由主代理作实施取舍；不重新恢复benchmark计划。

## 全局约束与源码依据

- 不运行SWE、效果benchmark、真实模型/embedding请求或Langfuse云上传。必要单元及真实SDK离线契约回归允许，不能冒充效果实验。
- Mem0：`mem0ai` 2.2.1，官方 `mem0ai/mem0@f1791daa86d469249c9b002f62644d58a1eb96a0`，Apache-2.0；add/search/delete API按此SHA，不猜旧limit参数。
- Langfuse：SDK5.2.0，已核 `langfuse-python@00e5098bd6511a2f7bc7b6813ac125a3af559697` 的start_observation/update/end。已有sink和有界exporter继续复用。
- Dream：作者zhengkid/Dream-RSI README明确指向 `google-research/dream-rsi@8ec35bd2538220517ce1bfca4282db21b8c518db`；distribution dream-rsi0.1.0，包核心无依赖、Apache-2.0。仅接trace/replay/policy，不运行第三方任务后端。
- 当前Dream的OptimalPolicy模板是空实现，首版必须用实际可执行的官方baseline ParallelRefineMethod，标记fixed_reference；不声称获得自动改进策略。
- 只保留SDK/实例所需凭据引用，不将真实key写入配置示例、任务、日志、结果或Git。
- 旧实验生成版本c69ff37和两个raw档案保持；已完成的结果收尾提交e81d424。当前累计客户端账本300请求/$0.888355不再增长，非供应商账单。
- 原checkout用户改动不动；当前worktree为interview-redesign。主代理独占pyproject/uv.lock、文档/共享实验日志及Git提交。

## 方案取舍

Mem0直接替换事实文件会同时改变精确删除、历史、权限和一致性，成本过大；只做演示则用户入口仍不可用。选语义候选索引，复用现有本地事实的完整回读和tombstone。

Langfuse自行实现HTTP协议会增加SDK升级维护，只有sink而无装配不能供正常会话使用。选官方SDK+现有有界导出器，补配置、订阅及生命周期。

Dream全量在线学习依赖模型、任务评分和费用计划；只重写一个同名算法又无法证明使用上游。选真实SDK离线回放桥，保留明确的未来学习入口边界。

## A / SOL-1：Mem0与中央装配

拥有：`src/my_agent_core/memory.py`、新`memory_semantic.py`/`memory_mem0.py`、core Agent的相关小接线；`src/my_coding_agent/agent.py`、`settings.py`、`rpc_server.py`、必要`session_handlers.py`及相应测试。不得修改SOL-2的LLM/Dream文件。

接口：

```python
Agent(memory_index=None, memory_index_owned=False, ...)
MemoryStore(..., memory_index=None)
await store.aretrieve(query, budget_chars=1200)
await index.search(scope=scope, query=query, records=records)
await index.aclose(timeout=close_timeout)  # bool，不能谎称杀死SDK线程
```

- [x] 保留同步remember/retrieve/read/delete；远端只索引已脱敏纯文本，infer=False、无vision/procedural，避免二次抽取。本地写入不等待远端。
- [x] 检索时增量reconcile；只接受scope/identity/revision一致且本地仍active、未删除的候选，正文从本地重取，重复候选去重。远端失败/陈旧/错scope不能复活本地删除，降级为原词法检索。
- [x] 每adapter单daemon worker执行reconcile/search；超时后永久熔断，不按轮次叠加挂起调用。aclose超时返回False并保留状态，明确底层SDK线程不能强制取消。
- [x] 两块仅全局可配置、项目覆盖剥离；Settings.memory_index默认禁用，provider=mem0，支持config_file/config、timeout=2、close_timeout=.1。Settings.observation默认禁用，backend=langfuse，env引用为LANGFUSE_PUBLIC_KEY/SECRET_KEY，另有base_url/capacity/network_timeout/close_timeout。
- [x] CodingAgent实例注入优先、默认caller-owned；配置工厂创建的资源owned。core不读环境/配置；显式启用但缺依赖/无效配置需清晰错误，不假装启用。
- [x] RPC新建/恢复/切换/fork/clone一致透传；现有同步session handler改为async，dispatcher原本支持await。先aclose旧Agent结束在途并detach，再装配新Agent；外部LLM不关闭。shutdown如实表达close状态。
- [x] 意义明确的红绿回归：stale/delete/scope/revision、无二次抽取、超时单inflight、caller ownership、配置缺失、RPC重建订阅；真实SDK契约使用本地向量/假embedding，禁网络和遥测，不测召回质量。

## B / SOL-2：Langfuse装配模块

拥有：`src/my_agent_llm/client.py`、必要observation小改、新`observation_runtime.py`、`langfuse_export.py`及对应测试。中央Settings/RPC/CodingAgent由SOL-1消费接口。

```python
subscription = llm.add_observer(observer)
subscription.close()  # 幂等，只移除此订阅
runtime = create_observation_runtime(
    enabled=False, public_key=None, secret_key=None, base_url=None,
    capacity=1024, network_timeout=5,
)
runtime.attach(llm)
runtime.close(timeout=5)  # bool
```

- [x] 原llm.observer保留；每次调用快照base+subscriptions，逐个异常隔离，五入口/辅助均覆盖，流close/取消终态契约保持。
- [x] disabled返回None且不导入SDK/建线程；enabled缺key或依赖清晰报错。凭据只在内存，不写metadata；默认无prompt/正文/异常消息。
- [x] runtime关闭先detach自身，只关自身exporter/client，保留已有observer与调用方LLM。close超时不能把worker alive记为已退出；状态保留dropped/errors/alive，支持再观察。
- [x] 核实际SDKusage_details标准键；完整一致时input=total_tokens-completion_tokens，不拿可能仅未缓存的prompt_tokens冒充完整输入；缺失不补零。
- [x] 离线回归实际订阅/关闭/错误/完整usage；真实SDK用本地受控transport或对象契约验证，不声称Langfuse云端已收录。

## C / SOL-2：Dream-RSI独立离线入口

拥有：新`src/my_coding_agent/dream_rsi_replay.py`、必要worker与专属测试；主代理添加console `my-agent-dream`及可选依赖。

- [x] 实现可调用Python函数与CLI main(argv=None)，显式trace/budget/输出新目录，按真实v1 schema校验baseline、nodes、parent/branch/attempt、evaluated/combined_score；未评分/畸形树拒绝。
- [x] 真实调用上游TraceEnv.replay和官方baseline solve，返回可复核summary/决策/版本与source信息，不开放首版任意Python候选代码，不伪造Session为评分分支树。
- [x] 限时子进程执行，不携带模型/embedding/API凭据；结束/取消清理自己完整进程树，输出目录不覆盖。进程隔离不冒充系统安全沙箱。
- [x] 真实SDK仅跑极小已评分树的契约回归，不作为策略质量/消融成绩；空OptimalPolicy模板问题和能力边界写入助手报告。

## 主代理验收与交付

- [x] 两名助手只读核现有接缝与三个上游固定SHA，分开文件所有权，给出自包含任务。
- [x] pyproject增加mem0/langfuse/dream-rsi三个可选extras；uv.lock冻结，未增加默认服务依赖。实际安装SDK供离线契约回归。
- [x] 双方交叉复核实现；主代理验证必要核心/LLM/RPC测试、Ruff、配置/CLI帮助、wheel包含新入口；真实SDK路径与fake边界分开。
- [x] 用户文档给出安装、启用、关闭及配置示例，记录优势/代价/问题/固定借鉴版本；更新持续实验日志，不编造质量数字。
- [ ] 明确路径提交自己的origin，更新PR2最终范围；核新head CI，不自动合并、不再发benchmark/收费请求。最终CI状态写PR/checks，不为终态再重复文档提交。

本计划开始于11月12日，实际适配和最终本地验收跨至11月13日。最终提交/CI终态以PR2和checks为准，不为回写终态重复触发文档提交。
