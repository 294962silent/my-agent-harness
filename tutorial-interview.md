# my-agent：面向 AI 应用开发面试的源码学习路线

- 项目用途：通过终端交互，让模型使用文件、命令和扩展工具完成编码任务；Python 运行内核也能供评测等入口复用。
- 目标：恢复对项目的理解，能从业务场景解释实现、设计取舍与异常边界。
- 主方向：AI 应用 / Agent 业务开发；后端与模型训练问题作为递进追问。
- 源码基线：`5e35dcb`，2026-11-10；建立本路线时工作区无本地改动。
- 本文件是新增的场景学习路线；原 `tutorial.md` 的事件、Hook、Extension 关键词课程及笔记保持原样。
- 状态：下列十课均为大纲。每次只展开一课，并在理解后进入下一课。
- 前置知识：Python 函数与对象、`async/await`、`yield/async for`、子进程 stdin/stdout、模型消息与工具调用。
- 证据范围：复用已调查的核心运行、RPC、上下文、记忆、子代理和评测源码，并复核当前入口。已通过版本差异检查确认本路线主链涉及的 CodingAgent、核心 Agent、loop、RPC 和 TS 客户端自 `081c3f9` 后未变。尚未对全部工具、技能、插件、后台任务或训练实验进行逐行核验。
- 验证边界：所列测试是建议阅读或运行的验证入口；本次整理课程不重新运行整库测试，不把历史测试结果直接当作当前版本的全量结果。

## 学习方法

每课按“用户要完成什么 → 入口接收什么 → 程序如何流转 → 何时结束或失败 → 怎样向面试官解释”展开。

源码符号是定位证据，不作为背诵目标。先能复述流程，再解释一处取舍，最后检查异常边界。

区分三类表述：源码已实现的行为、对设计动机的分析、尚未验证的效果。项目行为与个人职责分别确认，不根据源码推断个人贡献。

## 01：用户发出一句请求，Agent 怎样完成任务？

- 状态：大纲。
- 场景：用户要求“读取某个文件并解释这段实现”。
- 能回答：项目解决什么问题？模型、Harness、终端界面各负责什么？
- 主链：输入 → `PythonKernelClient.prompt` → RPC `_handle_prompt` → `CodingAgent.run_stream` → `Agent.prompt_stream` → `run_agent_loop` → 工具结果进入上下文或生成最终文本 → 事件返回终端。

必读顺序：
1. `my-pi-tui/src/client.ts::PythonKernelClient.prompt`：输入用户文本和选项，交给 `sendRequest("prompt", ...)`，输出请求完成的 Promise；流式内容另走事件通知。
2. `src/my_coding_agent/rpc_server.py::RpcServer._handle_prompt` → `src/my_coding_agent/agent.py::CodingAgent.run_stream`：输入 RPC 参数，调用编码业务门面，消费并序列化运行事件。
3. `src/my_agent_core/agent.py::Agent.prompt_stream` → `src/my_agent_core/loop.py::run_agent_loop`：输入用户消息、会话、模型和工具，按轮次运行并产出事件；工具调用与最终回答是分支，不能假设每次都调用工具。

启动入口补充：npm 包中的 `my-agent`、`my-agent-harness` 都指向 `my-pi-tui/bin/my-agent.js`；该脚本的 `main` 创建 `AgentApp(options)` 并执行 `app.start()`，导入的是编译后的 `dist/app.js`，没有经过 `src/index.ts`。Python 发行包也提供同名 `my-agent-harness` 入口，实际启动哪个取决于安装来源和 PATH；其参数转交和解释器选择在展开本课时复核。

选读验证：`tests/coding/test_rpc_server.py`、`tests/core/test_agent_loop_pure.py`。

暂缓：工具参数校验与权限见 02；流式呈现和中途干预见 03；完整 UI 绑定和 Python 选择在展开本课时补充。

## 02：模型决定调用工具以后，谁真正执行？

- 状态：大纲。
- 场景：模型请求读取文件、修改文件或执行命令。
- 能回答：如何把函数暴露给模型？如何检查参数、处理错误和审批？
- 主链：注册工具 → 生成模型可见 schema → 模型返回工具名与参数 → 工具调用 Hook → 注册表与工具对象执行 → `ToolResult` 和工具消息 → 下一轮模型调用。

必读顺序：
1. `src/my_agent_core/tools/core.py::Tool.to_openai_schema` / `Tool.execute`：输入函数定义或调用参数，分别输出工具描述和结构化执行结果。
2. `src/my_agent_core/registry.py::ToolRegistry.get_schemas` / `execute_tool` / `execute_batch`：输入工具名、参数及批次，定位已注册工具并组织执行；并发条件以实际分支为准。
3. `src/my_coding_agent/permissions.py::PermissionGate.__call__`：输入工具调用 Hook，依据模式、规则及审批回调返回控制决策；审批属于应用层机制。

选读验证：`tests/core/test_registry.py`、`tests/core/test_tools.py`、`tests/coding/test_permissions.py`、`tests/coding/test_path_confinement.py`。

暂缓：具体 read/edit/bash 实现按需要单独带读；取消与超时见 09。

## 03：为什么能流式显示，还能在运行中插话？

- 状态：大纲。
- 场景：终端持续显示回答和工具状态，用户在执行中补充要求。
- 能回答：事件与 Hook 有什么区别？RPC 响应与流式通知怎样区分？Steering 和 follow-up 怎样进入循环？
- 主链：模型流式片段 → 内核运行事件 → RPC `event` 通知 → TS 客户端及事件转换 → 展示更新；用户补充输入经 steer/followup 请求进入核心队列，在相应循环边界被消费。

必读顺序：
1. `src/my_agent_core/loop.py::_assistant_turn` / `run_agent_loop`：输入模型输出与排队消息，输出消息、工具和轮次事件。
2. `src/my_agent_core/agent.py::Agent.subscribe` / `Agent._notify`，配合 `src/my_agent_core/events.py` 与 `hooks.py::HookRegistry.emit`：定位观察通知和控制决策各自的通道。
3. `src/my_coding_agent/rpc_server.py::send_notification` / `_handle_steer` / `_handle_followup` → `my-pi-tui/src/client.ts::handleLine` → `my-pi-tui/src/bridge/event-translator.ts::EventTranslator.translate`：追踪协议到界面事件的转换；实际组件订阅在展开时继续定位。

选读验证：`tests/core/test_agent_steering.py`、`tests/coding/test_agent_steering.py`、`my-pi-tui/test/bridge/event-translator.test.js`；原 `tutorial.md` 的事件与 Hook 两课。

暂缓：具体终端布局不作为首轮面试重点；任务生命周期见 08。

## 04：关闭窗口、恢复会话或切换分支，状态怎样保留？

- 状态：大纲。
- 场景：恢复历史任务，或者从某条历史消息开始尝试另一种方案。
- 能回答：为什么需要会话树？当前分支与完整历史有什么区别？
- 主链：消息结束 → `Agent.prompt_stream` 更新 Session → 保存消息条目 → 加载或调整当前指针 → 沿当前路径重建消息 → 继续模型调用。

必读顺序：
1. `src/my_agent_core/agent.py::Agent.prompt_stream`：输入运行事件，定位消息结束时的会话更新。
2. `src/my_agent_core/session/session.py::Session.add_message` / `save` / `load`：输入消息和存储路径，输出可恢复的持久化状态；`get_current_path_messages` 构造当前分支上下文。
3. 同文件 `Session.rewind` / `fork`，配合 `src/my_agent_core/session/tree.py::path_to_entry` / `lowest_common_ancestor`：输入节点 id，定位分支与路径变化；持久化细节展开时继续追到 JSONL 编解码。

选读验证：`tests/core/test_session_append.py`、`tests/core/test_session_tree_modular.py`、`tests/core/test_session_jsonl_modular.py`。

暂缓：不要把会话历史当成长久用户画像，后者见 06；压缩边界见 05。

## 05：任务越来越长，怎样控制上下文和工具输出？

- 状态：大纲。
- 场景：多轮任务积累了大量消息和超长命令输出。
- 能回答：预算针对什么？何时压缩？保留什么、摘要什么？
- 主链：每轮模型调用前 → `ContextManager.prepare` 判断预算 → 处理超长工具输出及摘要缓存 → 选择摘要切点并保留近期内容 → 模型获得处理后的视图 → 压缩事件关联会话。

必读顺序：
1. `src/my_agent_core/loop.py::run_agent_loop`：定位每轮调用 `context_manager.prepare` 的位置，区分原始消息和模型可见消息。
2. `src/my_agent_core/context.py::ContextManager.prepare` / `_find_cut` / `_summarize_from_cut`：输入消息和预算，输出整理后的上下文；追踪没有合法切点和摘要失败的实际分支。
3. `src/my_agent_core/agent.py::Agent.prompt_stream`：消费压缩事件并关联 Session 状态，检查压缩与持久化如何衔接。

选读验证：`tests/core/test_context_boundary.py`、`tests/core/test_compaction_orphan_tool_prevention.py`。

暂缓：具体缓存命中率和成本收益需要实验，不能从“实现了压缩”直接推出；长期事实检索见 06。

## 06：Agent 怎样跨会话记住偏好和经验？

- 状态：大纲。
- 场景：用户纠正工作习惯，希望下次执行时仍然有效。
- 能回答：会话记录、长期记忆、技能和模型训练分别改变什么？
- 主链：保存事实或偏好 → Markdown 和结构化事实持久化 → 维护检索索引 → 按当前输入检索并控制字符预算 → 形成后续使用的记忆内容；注入时机在展开时追到 Agent 接线。

必读顺序：
1. `src/my_agent_core/memory.py::MemoryStore.remember`：输入记忆类别或事实，定位保存、事实更新和历史处理。
2. 同文件 `MemoryStore.retrieve` → `src/my_agent_core/memory_index.py::MemoryIndex.retrieve`：输入查询和字符预算，定位候选召回与排序输出。
3. `src/my_agent_core/memory_index.py::FtsIndex` / `BM25` / `Consolidator`：说明 FTS5、BM25 和事实巩固的分工；此路径不修改模型权重。

选读验证：`tests/core/test_memory_markdown.py`、`tests/core/test_memory_retrieval.py`。

暂缓：技能完整发现流程、外部记忆服务与 LongMemEval 指标另开专题；不把关键词检索描述为向量检索。

## 07：增加一个外部能力，怎样接入运行时？

- 状态：大纲。
- 场景：给 Agent 增加外部搜索或业务系统工具。
- 能回答：Extension、Hook、MCP 各解决什么问题？工具怎样进入同一注册表？
- 主链：CodingAgent 准备扩展或 MCP 配置 → 加载扩展 → MCP 建连并发现工具 → `api.register_tool` → 核心工具注册表 → 模型获得工具 schema 并调用。

必读顺序：
1. `src/my_coding_agent/agent.py::CodingAgent.run_stream`：定位首次执行前确保 MCP 初始化的接线。
2. `src/my_coding_agent/mcp.py::extension` → `MCPClientManager.connect_server`：输入服务配置，连接、发现并包装外部工具；错误、连接生命周期在展开时核验。
3. `src/my_agent_core/extensions/core.py::ExtensionAPI.register_tool` / `on` → `src/my_agent_core/registry.py::ToolRegistry.register`：定位能力与回调怎样进入运行内核。

选读验证：`tests/coding/test_mcp_autoload.py`、`tests/coding/test_mcp_extension_e2e.py`、原 `tutorial.md` 的 Extension 课。

暂缓：Skill、插件与 Extension 的全部加载差异另开专题；子代理执行见 08。

## 08：把子任务交给另一个 Agent，实际发生什么？

- 状态：大纲。
- 场景：主 Agent 把一个明确子任务交给指定角色。
- 能回答：子代理独立了什么、共享了什么？同步等待和后台运行如何判断？
- 主链：模型调用 task 工具 → 管理器读取角色定义 → 过滤继承工具 → 创建子 Session 和 Agent → 等待 child.run → 把结果交回父调用链。

必读顺序：
1. `src/my_agent_core/tools/builtin/task.py::make_task_tool`：定位工具入口及交给管理器的输入。
2. `src/my_agent_core/subagent_tasks.py::SubagentTaskManager.start_task` / `_run`：追踪创建、执行、结果和活动句柄清理；核验取消时的状态变化。
3. `src/my_agent_core/subagents.py::SubagentManager` 与同文件 `Subagent`：定位角色、模型与工具配置；逐项核对配置是否真正被执行链使用。

选读验证：`tests/core/test_subagent_tasks.py`、`tests/core/test_subagents.py`。

暂缓：独立后台任务系统另开专题；本项目的进程内子 Agent 与当前 Codex 外部 DS MCP 助手分别讲解。轮次限制、文件系统与权限边界不能只凭配置字段或“隔离”命名判断。

## 09：模型失败、工具失败、用户取消，系统怎样收尾？

- 状态：大纲。
- 场景：模型限流、工具异常或用户中途终止任务。
- 能回答：哪些错误重试？哪些结果回传模型？怎样区分完成、取消和失败？
- 主链：模型异常或取消信号 → 重试策略或终止分支 → 工具与消息事件补全 → RPC 响应和终端状态更新。

必读顺序：
1. `src/my_agent_core/retry.py::AutoRetryPolicy.is_retryable` / `compute_delay_ms`：输入异常和尝试次数，输出重试判定及等待时长。
2. `src/my_agent_core/loop.py::_assistant_turn` / `CancellationToken` / `run_agent_loop`：追踪重试、等待中取消、模型与工具结果的停止分支；命令进程清理在 bash 工具专题核验。
3. `src/my_coding_agent/rpc_server.py::_handle_abort` / `_handle_prompt`：定位取消请求、活动任务及响应状态，检查接口语义是否准确表达执行结果。

选读验证：`tests/core/test_agent_loop_retry.py`、`tests/core/test_tool_timeout_semantics.py`、`tests/coding/test_rpc_server.py`。

暂缓：性能和可靠性指标必须说明测量条件；本课不预设所有异常路径都已有恢复实现。

## 10：怎样证明 Agent 有用，后训练又处在什么位置？

- 状态：大纲。
- 场景：比较两个运行策略或模型版本，并核验训练是否带来真实任务收益。
- 能回答：单元测试、端到端评测、训练奖励分别证明什么？评测覆盖哪个 Agent？
- 主链：评测任务和环境 → `My-Agent-Harness.run` → 构造核心 Agent 与 Harbor 工具 → 执行并收集指标 → 外部评测器判断任务结果；训练是另一条流程，需要单独核验奖励、产物及接入。

必读顺序：
1. `my-pi-eval/src/my_pi_eval/agent.py::My-Agent-Harness._build_coding_agent` / `run`：输入任务、环境与上下文，明确这里实例化的是核心 `Agent` 并注入 Harbor 工具。
2. `my-pi-eval/src/my_pi_eval/tools.py::HarborToolRegistry`：定位工具如何调用评测环境；具体判分器在展开时沿外部接口继续核验。
3. `experiments/agent_rl/grpo_train.py::make_reward_fn` / `main`，对照 `grpo_reward.py::combine_with_execution`：检查训练入口实际使用的奖励和训练产物，区分已有实现、未接通链路和未来计划。

选读验证：`tests/eval/test_harbor_agent.py`、`tests/eval/test_harbor_tools.py`；真实 benchmark 的原始结果另作证据核验，文档数字不自动视为已复现。

暂缓：GRPO 公式、分布式训练与个性化在线 RL 待主链掌握后展开。未来综合 Harness 愿景与当前项目能力分别陈述。

## 每课结束时的验收

1. 用自己的话，从一个具体输入说到最终结果。
2. 指出模型、运行时与工具各自负责哪一段。
3. 解释一个取舍，并说出一个已知边界。

遇到不理解的文件或原理，可以继续要求按当前课程的调用链带读；每次只推进当前场景，保留已经完成的课程与笔记。
