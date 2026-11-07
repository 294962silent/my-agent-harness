# 工业界 Agent Harness 横向调研

> 调研时间：2026-11-07。对象：6 个商业产品 + 4 个开源参考基线。
> 方法：优先一手资料（官方文档 / 公开仓库源码 / 厂商技术博客），二手博客单独标注。
> 标注约定：
> - **【事实】** 来自官方文档或源码
> - **【社区逆向】** 来自反编译/源码泄漏分析（非官方），可信但要打折
> - **【推测】** 无公开证据，按行为反推
> - **未找到公开资料** 明确写出，不编造

---

## 摘要：一图看差异

| 产品 | 主循环 | 上下文压缩 | 工具并发 | 会话存储 |
|---|---|---|---|---|
| Codex CLI | 单层 state machine + Tokio 异步任务 | 阈值触发 + 远程 compact endpoint + **后台推测式压缩** | **读并写斥**（`tokio::sync::RwLock`，`supports_parallel_tool_calls` 决定读锁/写锁） | JSONL rollout（zstd）+ **SQLite 状态库** |
| Claude Code | 单层 `while(true)` async generator，**单线程** | **4 级流水线**（预算→裁剪→微压缩→全量）+ 413 反应式 | **全串行**（刻意选择） | JSONL `~/.claude/projects/`，实时追加，支持 resume/fork |
| Kimi Code CLI | 单层 agent loop + 后台 task/子代理树 | 窗口剩余低于 `reserved_context_size` 触发摘要压缩 | 工具批量 + 并行子代理（swarm 无上限爬坡） | `sessions/<workDir>/<sid>/agents/*/wire.jsonl` + `state.json`，支持 fork |
| Trae（IDE/Agent 2.0） | 去掉固定 Plan 阶段，纯 ReAct 循环 | 近期逐字保留 + 旧轮 LLM 摘要（不占用户配额） | 未公开（工具执行器支持顺序/并行两种模式） | 未公开 |
| DeepSeek Harness (dsh) | **事件溯源 turn/step 循环，loop 本身是插件** | 压缩是**可插拔 seam**，默认 0.8 阈值 + 16% 保留 + 工具输出 spill 落盘 | **执行器可插拔**：parallel-safe 并发、exclusive 成屏障，native 默认 10 | 追加式事件日志（JSONL + zstd 分帧，版本化迁移）+ SQLite 可选 |
| WorkBuddy | 云端沙箱 Agent + 本地桌面执行混合 | 未公开（有"记忆"与 RAG 知识库） | 多任务/多 Agent 并行（专家团） | 云端 Session/Runtime 模型，本地 `workbuddy.db` |

---

## 产品 1：Codex（OpenAI）

### 基本信息

- 厂商：OpenAI
- 开源/闭源：**开源**，Apache-2.0
- 仓库：<https://github.com/openai/codex>（核心 agent + CLI 全开源）
- 主要语言：**Rust**（`codex-rs/` workspace，TUI 用 Ratatui，TS 只剩少量 SDK/包装）
- 相关产品线：Codex CLI（本地）、Codex Web（云端异步）、Codex IDE 扩展、Codex App（桌面）
- 全局 compaction 文档：[Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)（Michael Bolin, 2026-01-23）

### 架构

#### 主循环

**【事实】** 主循环是**状态机 + 事件驱动**，不是纯同步调用：

- app-server 侧由 `codex-rs/core/src/tasks/` 驱动，任务种类 `RegularTask` / `CompactTask` / `ReviewTask` / `UserShellCommandTask`（见 `core/src/tasks/mod.rs`），`TaskKind` 区分 turn 类型，非"一个 while 循环跑到底"。
- 每个 turn 内部：`Session` → `TurnContext` → 模型流式请求 → 工具调度（`core/src/tools/`）→ 结果回灌 → 继续 step。
- 【事实】`agent/pre-step`、`agent/request` 类事件在第一手材料里对应 Codex 的 `EventMsg` 协议族（`codex-rs/protocol`），前端（TUI/app-server/IDE）全是事件消费者。
- **【事实】** 这层与 deep dive 描述一致："The core loop is a state machine, and the agent loop is the core logic"（[Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/how-codex-is-built)）。
- 两层结构：**sub-agent 是工具**（`multi_agent_tool.rs`、`agent/host/spawn.rs`、`spawn.rs`），子代理有自己的 Session/TurnContext，父代理只收结果，不共享上下文窗口 → 事实上形成"内层独立循环"。
- 自动续写：`turn_end_compaction_threshold_reached` 与 `AutoCompactWindowIds`（`core/src/state`）支持在 turn 结束时自动压缩并继续（见 `context_window.rs` 字段）。另有 `auto_compact` 特性开关。

#### 上下文管理

**【事实】**（源码 `codex-rs/core/src/compact.rs`、`compact_remote_v2*.rs`、`session/async_compaction.rs`、`session/context_window.rs`）：

1. **触发**：`ContextWindowTokenStatus` 计算 `active_context_tokens`、`auto_compact_scope_limit`、`full_context_window_limit`、`turn_end_compaction_threshold_reached` 等，超阈值触发。`AutoCompactTokenLimitScope` 允许按 scope（模型/turn）配置不同限额。
2. **压缩方式**：调用 **Responses API 的专用 compaction 端点**（远程压缩），把历史替换成"更小的表示"；`compact_remote_v2.rs` 是 v2 实现，含媒体预算 `compact_remote_v2_media.rs`（图像/音频单独计价）。同时保留本地 `compact.rs` 的 `SUMMARIZATION_PROMPT` + `SUMMARY_PREFIX`。
3. **后台推测式压缩**（`async_compaction.rs` 模块注释原文）："Holds the current speculative request, polled at checkpoints or awaited when compaction is due. Discarded requests still charge completed usage."——即在 checkpoint 上**预跑**压缩请求，命中就地采用，未命中丢弃但仍计费。
4. **保留策略**：`compact_retained_tool_outputs.rs`、`compact_token_budget.rs`；`COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000` 限制单条 user message 在压缩产物里的上限。
5. **工具输出截断**：`codex-rs/utils/output-truncation/src/lib.rs` —— `TruncationPolicy::Bytes` / `::Tokens`，超限输出被**中段截断**（`truncate_middle_chars`）并加前缀 `Warning: truncated output (original token count: N)`。"Tool output truncation applies only to live history, preserving full rollout payloads"（`context_manager/history.rs` 注释）——**磁盘上有全量，模型只见截断版**，和 spill 落盘同构但实现更轻。
6. **prefix cache 保护**：`context_manager/history.rs` 里 `reference_context_item` / `WorldStateSnapshot` 机制——每轮只 **diff** 上下文更新并发送增量（`merge_world_state_updates`、`split_prefix_updates`），避免整段 system 前缀变化导致缓存失效。`ModelSwitchInstructions` 在换模型时单独注入，避免污染公共前缀。
7. 图像 token 估算：`RESIZED_IMAGE_BYTES_ESTIMATE = 7373`，按 4 bytes/token 折算（源码注释明确写了这个换算）。

#### 工具系统

**【事实】**（`core/src/tools/mod.rs`、`tools/parallel.rs`、`tools/handlers/mod.rs`）：

- 内置工具（handlers 目录）：`shell`/`unified_exec`（+`shell_spec`）、`apply_patch`、`view_image`、`plan`、`update_plan`、`request_user_input`、`request_permissions`、`mcp` / `mcp_resource`、`multi_agents(_v2)`、`tool_search`、`new_context_window`、`get_context_remaining`、`sleep`、`current_time`、`list_available_plugins_to_install`、`request_plugin_install`、`extension_tools`、`send_message_to_user`、`wait_for_environment`、`dynamic`（动态工具）、`test_sync`。**估计 20+ 个**（`handler` 目录名逐一枚举所得，实际暴露给模型的数量随 feature flag 变化）。
- **并发模型是本调研里最精细的**（`tools/parallel.rs`）：
  ```rust
  parallel_execution: Arc<tokio::sync::RwLock<()>>,
  let guard = if supports_parallel {
      Either::Left(lock.read().await)      // 并行安全 → 读锁，可共存
  } else {
      Either::Right(lock.write().await)    // 非并行安全 → 写锁，独占屏障
  };
  ```
  即 **读共享 / 写独占**（reader-writer）并发，每个工具用 `ToolExecutor::supports_parallel_tool_calls()` 声明自己能否并行。这是"读写分离"模型的最纯粹实现。
- **工具暴露面**（`tools/src/tool_executor.rs`）：`ToolExposures` bitflags = `DIRECT`（直接进 prompt）/ `DEFERRED`（可被 tool search 发现）/ `CODE_MODE`（可在嵌套脚本里调用）—— **工具按需加载**，控制工具 schema 对 prompt 的体积占用。
- **权限门禁**：`execpolicy` crate（独立 crate，`exec_policy.rs` 46KB）+ `sandboxing` / `linux-sandbox` / `windows-sandbox-rs` / `bwrap` / `mxc-sandbox`；`approvals.rs`、`network_approval.rs`、`guardian/`（Guardian AI 二次审批）。REPL 有 `Esc to interrupt`、审批弹窗。
- 输出上限：见上"工具输出截断"。MCP 结果单独有 `truncate_mcp_tool_result(max_bytes)` 循环收缩逻辑。

#### 会话存储

**【事实】**：

- **两层存储**：
  1. **Rollout（JSONL）**：`codex-rs/rollout/`，`RolloutLine { timestamp, ordinal, item }`，追加写；默认**Zstandard 分帧压缩**（`compression.rs`：`COMPRESSED_SUFFIX = ".zst"`），冷文件由后台 worker 压缩（`spawn_rollout_compression_worker`，fire-and-forget）。
  2. **SQLite 状态库**：`rollout/src/state_db.rs` —— `codex_state::StateRuntime`，存 thread metadata、记忆流水（memories 的 stage-1 输出）、session index、rollout 索引。
- **结构**：线性 append-only，但支持 **checkpoint / rollback**：`CompactionCheckpoint`、`Rollback`（`history.rs` 里 `drop_last_n_instruction_turns` 回滚 N 轮），`thread_rollout_truncation.rs`。
- **fork**：`ThreadId` + `thread_manager.rs`（128KB）支持 thread spawn / fork；`agent/host/spawn.rs` 里子代理是独立 thread。
- `codex-rs/history/src/lib.rs` 明确定义 `SessionMetaLine` / `ThreadHistoryMode` / `InitialHistory` / `ResumedHistory` —— **支持 resume**。

#### 扩展机制

**【事实】**（crate 列表）：

- **MCP 一等公民**：`codex-mcp` / `rmcp-client` / `mcp_tool_call.rs`（100KB）/ `mcp_tool_exposure.rs`。
- **Skills**：`codex-rs/skills/` + `core/src/skills.rs`（与 Claude Code 的 Skills 概念对齐；官方文档 <https://developers.openai.com/codex/skills/>）。
- **Plugins**：`codex-rs/plugin/`、`plugin-host/`、`core-plugins/`、`core/src/plugins/`，支持运行时装载/卸载。
- **Hooks**：`core/src/hook_runtime.rs`（53KB）+ `hooks/` crate，含 `PreCompact`/`PostCompact`/`turn_interrupt` 等钩子。
- **Extensions**：`core/src/ext/`、`codex_extension_api`（扩展注册表）。
- **AGENTS.md**：`agents_md.rs` / `agents_md_manager.rs`，目录树向上查找 + 合并。

#### 子代理

**【事实】**：`multi_agent_tool.rs`、`multi_agents_v2`、`agent/host/{admission,spawn,ownership,completion}.rs`、`codex_delegate.rs`（16KB）。子代理有独立的 `TurnContext`、独立的 rollout thread；`agent-message-board` / `message-board-mcp` 提供代理间消息；`agent-roles` 定义角色。**隔离方式：独立 Session + 独立 history，父只拿最终 assistant message**。`collaboration-mode-templates` 提供多种协作模式模板。

#### 记忆系统

**【事实】** `codex-rs/memories/README.md`（这是本调研里**最完整的一份公开记忆实现说明**）：

- 两阶段流水线，在 **root session 启动时**后台异步跑（且满足：非 ephemeral、feature 开启、非 subagent session、state DB 可用）。
- **Phase 1（逐 rollout 抽取）**：从 SQLite 里按启动规则 claim 一批"够旧的"rollout → 过滤成 memory-relevant items → 并发（固定并发上限）喂模型 → 结构化输出 `raw_memory` / `rollout_summary` / `rollout_slug` → **脱敏 secrets** → 写回 DB 作为 stage-1 输出。租约（lease）+ 失败退避。
- **Phase 2（全局归并）**：取全局锁 → 按 `usage_count` / `last_usage` 选 top-N → 同步文件系统产物 `raw_memories.md`、`rollout_summaries/` → **用 git 做基线**（`~/.codex/memories/.git`）→ 生成 `phase2_workspace_diff.md` → 起一个**内部 consolidation 子代理**（无审批、无网络、只本地写、禁用 collab 防递归）→ 完成后重置 git 基线。
- 产物：`MEMORY.md`、`memory_summary.md`、`skills/`。
- 读取侧：`codex-rs/memories/read` 负责"memory developer-instruction 注入 + 引用解析 + 使用 telemetry"。

#### 评测

**【事实/第三方榜单】**：

- Terminal-Bench 2.1（[Snorkel 榜单](https://snorkel.ai/leaderboard/terminal-bench-2-1/)）：**Codex CLI + GPT-6 Astra = 87.4%**（第 1），Codex CLI + GPT-5.5 = 83.1%，Codex CLI + GPT-5.6 Terra = 78.4%，+ GPT-5.6 Luna = 75.7%。**Codex CLI 占据榜单前 11 中的 4 席，且是 harness 侧的强项**。
- SWE-bench：OpenAI 未以 CLI 形式单独打榜（Codex Web/模型分数与 harness 混在一起），未找到可靠的 "Codex CLI on SWE-bench Verified" 官方数字 → **未找到公开资料**。
- 内部实践（[Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/how-codex-is-built)）：Codex 自身 >90% 代码由 Codex 写；每位工程师并行 4–8 个 agent。

### 关键设计亮点

1. **读并写斥的工具并发**：`RwLock` + 每工具的 `supports_parallel_tool_calls` 把"哪些工具能并行"变成**工具属性而非调度器策略**，不需要在循环里维护依赖图。
2. **推测式后台压缩**：在 checkpoint 上前置发起 compact 请求，命中即用、未命中也只是浪费一次调用——把压缩延迟藏进正常 turn，避免"撞墙才压缩"的停顿。
3. **工具暴露面三分（DIRECT/DEFERRED/CODE_MODE）**：把"工具很多但 prompt 装不下"变成可调策略，配合 `tool_search` 做按需加载。
4. **记忆用 git 做 diff 基线**：Phase 2 把记忆工作区当作 git worktree，用 diff 来界定"这次归并到底改了什么"，失败可回滚，且 consolidation agent 的输入就是 diff 文件 —— 非常工程化。
5. **两层持久化**：JSONL 保真 + SQLite 做索引/记忆/claim 租约，两者职责清晰。
6. Rust + 极少依赖 + 全开源，沙箱（Linux bwrap / Windows sandbox service / macOS seatbelt 对应 `sandboxing`）也是一等公民。

### 来源

- <https://github.com/openai/codex>（README、`codex-rs/` 源码树）
- 源码文件（GitHub API `contents` 直读）：
  - `codex-rs/core/src/tools/parallel.rs`
  - `codex-rs/core/src/tools/mod.rs`、`codex-rs/core/src/tools/handlers/mod.rs`
  - `codex-rs/tools/src/tool_executor.rs`
  - `codex-rs/core/src/compact.rs`、`compact_remote_v2.rs`、`session/async_compaction.rs`、`session/context_window.rs`
  - `codex-rs/core/src/context_manager/history.rs`
  - `codex-rs/utils/output-truncation/src/lib.rs`
  - `codex-rs/rollout/src/{lib,state_db,compression}.rs`
  - `codex-rs/history/src/lib.rs`
  - `codex-rs/memories/README.md`
- <https://newsletter.pragmaticengineer.com/p/how-codex-is-built>（Pragmatic Engineer，访谈 Codex 负责人，二手但含一手引述）
- <https://openai.com/index/unrolling-the-codex-agent-loop/>（官方博客，正文为 JS 渲染，抓取时只有标题/作者；内容要点以 Pragmatic Engineer 转述为准）
- <https://developers.openai.com/codex/skills/>
- <https://snorkel.ai/leaderboard/terminal-bench-2-1/>

---

## 产品 2：Claude Code（Anthropic）

### 基本信息

- 厂商：Anthropic
- 开源/闭源：**闭源**（npm 分发，2026-03-31 因 source map 事故**泄漏完整 TypeScript 源码**，社区据此逆向）
- 仓库：无官方仓库；npm `@anthropic-ai/claude-code`
- 语言：**TypeScript**，UI 用 React + Ink + Yoga，Bun 构建（【事实】，见 [Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built)）
- 文档：<https://code.claude.com/docs/en/how-claude-code-works>

### 架构

#### 主循环

**【社区逆向】**（v2.1.88 泄漏源码分析，`query.ts`）：

- **单层 `while(true)`**，但写成 **async generator**：`export async function* query(params: QueryParams): AsyncGenerator<StreamEvent | RequestStartEvent | Message | TombstoneMessage | ToolUseSummaryMessage, Terminal>`。返回 `Terminal` 而非 `void` —— **退出原因是类型化的**（task complete / context limit / user interrupt / budget exhausted）。
- **单线程**，刻意如此："一个实例、一个线程、一个循环，没有共享可变状态，没有锁，没有竞态"。代价是长 bash 命令期间事件循环不能抢占。
- 早期版本是**递归**，长会话爆栈；现在用 `State` 对象在迭代间携带状态，`transition` 字段记录"上一轮为什么继续"。
- **9 个自动续跑条件**（原文列举）：① 工具结果回灌 ② API 错误/网络失败（指数退避最多 10 次）③ `stop_reason: max_tokens` 中途截断 ④ 注入 token 预算警告 ⑤ 主动压缩触发 ⑥ 413 后反应式压缩 ⑦ 子代理 Task 完成 ⑧ **Stop hook 否决停止** ⑨ **Pre-tool hook 拦截工具**。
  → ⑧⑨ 是**自动续写**的治理层：hook 可以让"模型说完了"变成"继续干"。
- 每轮全部走 Opus 级模型；另有一个小模型只做两件事：session 启动时的 warmup 健康检查请求、bash 输出后的**文件路径抽取**。
- 子代理作为 **Task 工具**存在 —— 父循环**无法区分"等子代理"和"等一次文件读"**，这也是它单线程却不阻塞的原因。

#### 上下文管理

**【社区逆向】**（两个来源互相印证）：

- **三层/四级流水线**，每次 API 调用前按"便宜优先"顺序跑：
  1. **Tool Result Budgeting**（无模型调用）：超阈值结果**落盘**，上下文里只留 2KB 预览 + 文件路径；每个工具声明 `maxResultSizeChars`（默认 50k 字符），`Read` 工具 opt-out（阈值设 `Infinity`，否则自己读自己写出的文件成环）。
  2. **Snip Compaction**（无模型调用）：直接删掉老的工具结果。
  3. **Microcompaction**（小模型调用）：逐条摘要中间轮次；**触发依据是时间**——距上次 assistant 消息超过阈值意味着服务端 prompt cache 已过期，反正要重算，索性先清。
  4. **Autocompact**（大模型）：先试 "Session Memory Compaction"（抽成紧凑对象），不够再全量摘要。
  5. **Reactive compact**：仅在 API 返回 **413** 后触发，全量折叠；`hasAttemptedReactiveCompact` 熔断，**第二次 413 是终止条件**。
- 全量摘要要求输出 **9 个结构化小节**（Primary Request and Intent / Key Technical Concepts / Files and Code Sections / Errors and fixes / Problem Solving / **ALL user messages** / Pending Tasks / Current Work / Optional Next Step with **verbatim quotes**）—— 最后一项是防"意图漂移"的锚点。
- 压缩后**重建**：重新注入最近 5 个文件附件（各 ≤5K token）、活跃 skills（≤25K）、plan 状态、工具/MCP delta。
- **prefix cache 保护**：
  - 系统提示用 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 把"可缓存段"和"会话特定段"切开。
  - 内容块带 `cache_control` 断点；观察到的会话里 **90%+ token 命中缓存**。
  - **压缩用的 fork 子代理故意继承完整工具集**——因为 Anthropic 的缓存按工具定义匹配，换了工具就会 cache miss，得重算整个 system prompt。副作用是模型可能真去调工具而不写摘要（Sonnet 4.5 上 0.01%，4.6 上 2.79%），于是加了硬 preamble："CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."
  - **microcompaction 会改 system prompt → 必须显式失效缓存**；漏了这一句导致 20% 的 prompt cache break。
- 启动成本：**8,000–12,000 token** 在第一次输入之前（200K 窗口占 4–6%；32K 窗口占 25–37%）。其中 system role ≈2,900、工具定义 ≈3,000（18+ 工具）、CLAUDE.md 500–2,000。`CLAUDE.md` 以 **user message** 注入而非 system prompt —— 因为模型对 system/user 的"服从强度"不同。
- **跨会话**：`autoDream` 特性——后台按"距上次归并时长 → 距上次归并的 session 数 → 锁检查"三道门筛选，起一个 fork 子代理跑 `/dream` 提示做记忆整合。
- **熔断器文化**：20 个 `MAX_*` 常量，每个自动恢复路径都有上限（`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` 等），代码注释里带内部 BQ 查询编号与损失数字（如"1,279 个 session 连续 50+ 次压缩失败，每天浪费 ~250K 次 API 调用"）。

#### 工具系统

**【社区逆向】**：

- **42 个工具**（泄漏版本计数），核心 18+ 个进 prompt；`utils/` 目录 18 万行（35% 代码量）。
- **并发模型：全串行**。明确的设计取舍：并行提高吞吐但引入共享状态竞态；单线程确定性优先。作者点名对照 [Hermes Agent](https://arize.com/blog/how-hermes-implements-open-source-agent-harness-architecture/)（thread pool 并行，吞吐更高但有同文件竞写风险）。
- **权限门禁是代码量最大的部分**：38,000 行（7.5% 代码库）用于权限与安全。`BashTool` 总计 12,414 行，其中执行逻辑仅 1,143 行，其余是：
  - `bashPermissions.ts` 2,621 行（规则匹配 + 分类器 + allowlist）
  - `bashSecurity.ts` 2,592 行（20+ 条 AST 级安全检查：注入、替换、混淆）
  - `readOnlyValidation.ts` 1,990 行（判定哪些命令可自动放行）
  - `pathValidation.ts` 1,303 行（禁止越界写）
  - `sedValidation.ts` 684 行（专门给 `sed` 写了一套校验）
  - `PowerShellTool` 8,961 行**完全重写**（download cradle、COM 滥用、CLM bypass 检测……）
  - **工具 = 10% 执行 + 90% 安全**。
- 输出上限：每个工具 `maxResultSizeChars`（默认 50k 字符）。
- 工具描述本身是巨型 prompt：37 个工具 prompt + 系统 prompt + 压缩 prompt ≈ **60,000 token**；`BashTool` 的 prompt 单条 ≈6,500 token，里面是"失败案例目录"（别用 `cat` 读大文件、git hook 失败后别 `--amend`……）。

#### 会话存储

**【社区逆向】**：

- **JSONL，实时逐事件追加**到 `~/.claude/projects/`（不是会话结束时批量写）。
- `claude --resume` 精确重建；`--fork-session` 在任意点**复制历史到新会话**，原会话不变。
- `TombstoneMessage` 类型配合压缩：被压缩掉的消息在 JSONL 里留墓碑，保证回放日志在压缩后仍一致。
- 结构：**线性** + 墓碑/检查点；fork 通过复制实现（非树状原生 DAG）。

#### 扩展机制

**【事实】**（官方文档）：MCP、Hooks（含 Stop hook / PreToolUse hook）、Skills、Subagents（`~/.claude/agents/`）、Output styles、GitHub Actions / GitLab CI、SDK（`@anthropic-ai/claude-agent-sdk`，把 harness 暴露成库）、Channels、Remote Control。

#### 子代理

**【事实+逆向】**：`Task` 工具派生；**独立上下文窗口**，父只收到 1,000–2,000 token 的摘要；子代理内部可能烧 100K+ token。子代理共享 module-level state，但只有主线程状态会随主线程压缩被清空。递归有 guard 防止压缩子代理再触发压缩。

#### 记忆系统

**【社区逆向】**：`autoDream` 后台整合（见上）；`CLAUDE.md`（项目级，以 user message 注入）+ Session Memory Compaction（压缩时抽"关键事实对象"）。**没有**独立的、可查询的长期记忆库（不像 Codex 的 SQLite + git 基线）。

#### 评测

- **【事实/第三方】** Terminal-Bench 2.1（[Snorkel](https://snorkel.ai/leaderboard/terminal-bench-2-1/)）：Claude Code + Claude Fable 5 = **83.8%**（第 2）、+ Opus 4.8 = 78.9%、+ Sonnet 5 = 74.6%、+ Opus 4.7 = 68.9%。TB2.0→2.1 中 Opus 4.6 + Claude Code 从 58% → 70.1%（+12.1pt，榜单最大涨幅）。
- SWE-bench：以模型分数为主（Opus 4 报 72.5%），harness 侧未单独打榜 → 以 Terminal-Bench 为准。
- 【事实】Anthropic 自述 >90% Claude Code 代码由 Claude Code 自己写（Boris Cherny）。

### 关键设计亮点

1. **"loop 不聪明，harness 才是产品"**：513K 行里 loop 占不到 1%，其余全是让 loop 活下来的工程（工具安全、上下文预算、错误分类、遥测）。
2. **压缩是分层的经济学**：先做零成本（截断/裁剪），再做小模型摘要，最后才对全量摘要付费；顺序不可打乱。
3. **熔断器与 BQ 编号文化**：每条自动恢复路径都有上限并附带"不加上限会怎样"的线上数据。
4. **为 prompt cache 做架构妥协**：连压缩子代理的工具集都要为了缓存命中而"多余地"保留；微压缩后必须显式失效缓存。
5. **单线程正确性优先**：在吞吐与确定性之间，明确站在确定性一边，并把代价写清楚。

### 来源

- <https://internals.laxmena.com/p/why-claude-codes-agent-loop-is-over>（INTERNALS.md，社区逆向，2026-06-03，基于 v2.1.88 泄漏源码）
- <https://newsletter.victordibia.com/p/inside-claude-code>（Victor Dibia，社区逆向，2026-04-06，含代码统计）
- <https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built>（一手访谈，技术栈/权限系统/设计哲学）
- <https://code.claude.com/docs/en/how-claude-code-works>（官方文档，ground truth）
- 二手参考：<https://harrisonsec.com/blog/claude-code-deep-dive-query-loop/>、<https://bits-bytes-nn.github.io/insights/agentic-ai/2026/03/31/claude-code-architecture-analysis.html>、<https://github.com/VILA-Lab/Dive-into-Claude-Code>
- <https://snorkel.ai/leaderboard/terminal-bench-2-1/>

---

## 产品 3：Kimi Work / Kimi Code CLI（月之暗面 Moonshot）

### 基本信息

- 厂商：Moonshot AI（月之暗面）
- 产品线：
  - **Kimi Code CLI**（终端 agent）：**开源 MIT**，<https://github.com/MoonshotAI/kimi-code>（前身为 `MoonshotAI/kimi-cli`，已归档 → 现参考 <https://github.com/MoonshotAI/kimi-cli>）
  - **Kimi Code Desktop** / **Kimi Code for VS Code**（闭源客户端，共用同一 CLI 引擎）
  - **Kimi Work**：Kimi AI 平台内的"生产力工作台"（写作/编码/研究/PPT/表格/多 agent 编排），**闭源 SaaS**，未见公开技术架构
- 语言：**TypeScript**，Node.js ≥22，npm `@moonshot-ai/kimi-code`，单二进制分发
- 文档：<https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html>

> ⚠️ 名称纠偏：任务里写的是 "Kimi Work / Kimi CLI"。可调研的是 **Kimi Code CLI**（开源）与 **Kimi Work**（SaaS，无公开架构）。以下以 Kimi Code CLI 为主体，Kimi Work 单列"未找到公开资料"。

### 架构

#### 主循环

**【事实】**（官方文档 + `config.toml` 字段）：

- 单层 **agent loop**（模型 call → 工具 → 回灌），由 `loop_control` 段参数化：
  - `max_steps_per_turn`（0/未设 = 无限）—— **支持"自动续写"到 unlimited**
  - `max_attempts_per_step`（默认 10）—— 单 step 总尝试次数
  - `reserved_context_size`（默认 50000）—— 预留给模型输出，**剩余窗口低于它即触发自动压缩**
  - `compaction_max_attempts`（默认 5）
- 只对**瞬时失败**重试：连接错误、超时、429、5xx；配额耗尽的 429 不重试。
- 后台 task 是一等公民：`Bash(run_in_background)`、`Agent(run_in_background)`、`AskUserQuestion(background)` 都会返回 task id，完成时**自动以通知形式回灌**（`TaskList` / `TaskOutput` / `TaskStop` / `WaitFor`）。`WaitFor` 上限 90s，超时不算错误。
- `Ctrl-S` 支持**流式中间插入消息**（steer），不必等当前回答结束。
- Plan 模式：只能用户开启（`Shift-Tab` / `/plan`），agent 无工具可主动进入；开启后 `Write`/`Edit` 只能写 plan 文件，`TaskStop` 被禁用；`ExitPlanMode` 读 plan 文件 → 弹审批（可附 1–3 个备选方案）。

#### 上下文管理

**【事实】**：

- 自动压缩：`reserved_context_size` 低于阈值触发（按官方配置："automatic compaction is triggered when the remaining context window falls below this value"）。
- 手动：`/compact [hint]` —— **可以给提示词指定保留重点**（"Keep the discussion about database migrations"）。运行中排队，已有排队则拒绝重复，若等待期间已自动压缩则跳过。
- `token_counting.strategy` = `measured+estimated` / `measured` / `estimated`：**实测 usage + 尾部估算**混合；内部逻辑（压缩触发、预算、溢出退避）始终同时用实测与估算。
- `max_input_size` 可单独声明，"compaction、context-overflow 检查和 usage ratio 优先用它"。
- **工具输出落盘**：`TaskOutput` 内联预览最多最近 **32KB**，完整日志写盘并返回 `output_path`，提示用 `Read` 分页读。`Read` 工具：默认与单次上限均 **100,000 字符**，且同时受"约 1/4 最大 token（默认 25,000）"约束；`Glob` 默认 100 条、`Grep` `head_limit` 默认 250，"Large pages are saved to a file that the agent can read with `Read`"。
- 压缩后的会话仍保留完整历史文件（`wire.jsonl`），只是模型侧上下文被摘要。
- 导入 Claude Code / Codex 会话时："It includes as many recent turns as fit while leaving room to keep working before the context is compacted; the complete history and a copy of the original session files are saved with the session" —— 与 spill 同思路。
- prefix cache 保护：**未找到公开资料**（无官方说明）。

#### 工具系统

**【事实】**（<https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html>）：

- **内置工具清单（明确分类）**：
  - 文件：`Read`、`Write`、`Edit`、`Grep`（ripgrep）、`Glob`、`ReadMediaFile`
  - Shell：`Bash`
  - Web：`WebSearch`、`FetchURL`
  - Plan：`ExitPlanMode`
  - 状态：`TodoList`
  - 协作：`Agent`、`AgentSwarm`、`AskUserQuestion`、`NotifyUser`、`Skill`
  - 后台任务：`TaskList`、`TaskOutput`、`TaskStop`、`WaitFor`
  - 定时：`CronCreate`、`CronList`、`CronDelete`
  - **共 22 个**
- **权限门禁 —— 分类默认放行（read/write 分离）**：
  | 类别 | 默认 |
  |---|---|
  | 只读（`Read`/`Grep`/`Glob`/`ReadMediaFile`/`WebSearch`/`FetchURL`/`TodoList`/`Agent`/`Skill`/`AskUserQuestion`/`NotifyUser`/`ExitPlanMode`/`TaskList`/`TaskOutput`/`WaitFor`/`CronList`） | **自动放行** |
  | 写/执行（`Write`/`Edit`/`Bash`/`TaskStop`/`CronCreate`/`CronDelete`） | **需审批** |
  - 三档权限模式：`manual`（默认）/ `auto` / `yolo`；`Ask When Needed` 模式下常规工具调用跳过审批（Plan 退出审批不受影响）。
  - 规则系统：`[[permission.rules]] decision = "allow"|"deny" pattern = "Bash(rm -rf*)"` —— **支持参数级模式匹配**（Claude Code 风格）。
  - 另有 `PreToolUse` / `PostToolUse` hooks（`matcher` + `command` + `timeout`）。
  - 子代理**继承**主代理的 "always allow" 规则。
- **并发模型**（部分可确认）：
  - 同一步内多个 `Agent` 前台调用会在 TUI 里分组展示。
  - `AgentSwarm`：**默认无上限爬坡并发**（立即启 5 个，之后每 700ms 加 1），可用 `KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY` 封顶；最多 128 个；`AgentSwarm` 必须是该响应里**唯一**的工具调用。
  - `task.max_running_tasks` 控制后台任务并发上限。
  - **普通工具（非 agent）是否可并行执行 → 未找到公开资料**。
- 输出截断：见上（32KB 预览 / 100k 字符 / 25k token）。
- 安全过滤：`Grep`/`Glob` **默认自动过滤 `.env` 和私钥等敏感文件**，`include_ignored` 可以搜 `.gitignore` 项但敏感文件仍被过滤。

#### 会话存储

**【事实】**（<https://www.kimi.com/code/docs/en/kimi-code-cli/guides/sessions.html>）：

```
~/.kimi-code/
├── config.toml
├── session_index.jsonl
└── sessions/
    └── <workDirKey>/
        └── <sessionId>/
            ├── state.json            # 标题、创建时间等元数据
            └── agents/
                ├── main/
                │   └── wire.jsonl    # agent 事件流（含请求 trace：工具 schema、请求参数、MCP 列表）
                └── <subagentId>/
                    └── wire.jsonl
```

- **按工作目录分组**（`<workDirKey>`），`session_index.jsonl` 做全局索引。
- **每个 agent（main + 每个 subagent）一条独立 `wire.jsonl`** —— 结构上天然是"树"，但每个节点内部线性。
- **支持 fork**：`/fork` 创建独立副本，**你停留在原会话**；打印并存剪贴板一条可直接运行的 `kimi --resume` 命令；`/goal` **不**被复制。
- **支持 resume**：`kimi --continue`（最近）、`kimi --session <id>`，二者互斥。
- **支持导出**：`kimi export <id>` 打包 ZIP；`/export-md` 导出 Markdown。
- **支持从 Claude Code / Codex 导入**：`kimi import-sessions`，最多导入最近 100 个会话、15/30 天窗口，**本地转换、不改原文件、不调模型**。
- 格式：JSONL（每 agent 一个文件）+ JSON 元数据，**不是**树状 DAG（fork 靠复制，不是指针）。

#### 扩展机制

**【事实】**：MCP（可用自然语言配置与鉴权）、Agent Skills（`SKILL.md`，支持 `type = "inline"`、`disableModelInvocation`、嵌套深度 ≤3）、**Plugins**（可携带 agents / skills / instructions）、**Hooks**（PreToolUse/PostToolUse/子代理完成等）、**自定义 Agents**（Markdown + frontmatter）、**ACP 子命令**（`kimi acp`，可被其他 agent 驱动）、Server API、Remote Control、Remote environments。

自定义 agent 的发现优先级（**少见的明确写死了优先级**）：
`Explicit(--agent-file)` > `Project` > `Extra` > `User` > `Plugin` > `Built-in`；同名时高优先级覆盖；目录型文件要覆盖内置需显式 `override: true`。

Agent 文件 frontmatter 字段：`name` / `description` / `whenToUse` / `override` / `tools`（允许表）/ `disallowedTools`（拒绝表）/ `subagents`（子代理允许表，支持 `*`）。模板变量：`${base_prompt}`、`${plugin_sections}`、`${skills}`、`${agents_md}`、`${cwd}`、`${cwd_listing}`、`${os}`、`${shell}`、`${now}`、`${additional_dirs_info}`。

**文档还专门写了信任模型警告**：项目级 agent 文件来自刚 clone 的仓库，可以用 `override: true` 整个替换主代理的 system prompt —— "review `.kimi-code/agents/` … with the same caution you would apply to scripts"。这一点比同类产品更直白。

#### 子代理

**【事实】**：

- 三个内置类型：`coder`（默认，可读写执行）、`explore`（**只读**）、`plan`（**连 shell 都没有**）。
- **上下文完全隔离**：子代理只看得到的任务描述，看不到主代理历史；中间推理与工具记录**不回传**，只回最终结果。
- **委派深度有硬上限**：三个内置子代理**不能再派子代理**；自定义 agent 默认继承 `coder/explore/plan` 白名单（同样不能再派），要更深必须显式声明 `subagents: *` 或具体列表 → "delegation chains always terminate"。
- 子代理在 turn 结束时若还有后台 task 在跑，**会等 task 落定才报告完成**。
- **子代理模型池**（`[secondary_model]`）：`default_model` + `models` 池；池里 ≥2 个时 `Agent`/`AgentSwarm` 多出 `model` 参数；子代理**不继承**调用方的 thinking effort；可用 `overrides` 注册同一模型的"变体 alias"来绑不同 effort。
- 子代理超时默认 2 小时（`[subagent] timeout_ms`）。
- 权限继承：主代理的 "always allow" 规则自动下发给所有子代理。
- 进阶：`Tower`（实验性，仓库级多 agent 协作模式，不是普通子代理类型）。

#### 记忆系统

- 会话内：`TodoList` 状态存于 agent session；`/goal` 概念（fork 不复制）。
- 跨会话长期记忆：**未找到公开资料**（文档里没有 `memory` 配置段，只有 `merge_all_available_skills` 等）。→ 结论：**Kimi Code CLI 未见公开的跨会话长期记忆实现**。

#### 评测

- 未在 Terminal-Bench / SWE-bench 官方榜上以 "Kimi Code CLI" 名义出现（[Snorkel TB2.1 榜](https://snorkel.ai/leaderboard/terminal-bench-2-1/) 18 条记录里无 Kimi）。
- 有第三方提及 Kimi 模型分数（如 "Kimi K3.1 Pro, Claude Code, 65.7%" 出现在 [CodingFleet TB4.0 汇总](https://codingfleet.com/blog/terminal-bench-4-leaderboard-2026)），但那是**模型 + Claude Code** 的组合，不是 Kimi 自家 harness。
- → **Kimi Code CLI 自身 harness 的公开 benchmark 成绩：未找到公开资料**。

### 关键设计亮点

1. **每个 agent 一条 `wire.jsonl`** —— 会话目录结构直接映射 agent 树，恢复/回放/导出都是"读文件"级别的事。
2. **后台任务是一等公民**：task id + 自动通知 + `WaitFor`（≤90s，可被新消息打断）—— 把"长时间运行"建模成会话内可等待的事件，而不是阻塞 turn。
3. **委派深度硬收敛**：内置子代理不能再派子代理，要更深的链必须显式 opt-in。防止递归爆炸的机制化（而非靠 prompt 提示）。
4. **子代理模型池 + 变体 alias 绑 effort** —— 让"用便宜模型跑子任务"成为配置能力，而不是改代码。
5. **`/compact` 可带 hint**，且与自动压缩有排队/去重语义。
6. **明确写出 agent 文件的信任模型风险**，还做了跨工具兼容（Claude Code / OpenCode 的 agent 文件格式基本能直接加载）。

### 来源

- <https://github.com/MoonshotAI/kimi-code>（README，MIT）
- <https://github.com/MoonshotAI/kimi-cli>（已归档的旧 Python 版）
- <https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html>
- <https://www.kimi.com/code/docs/en/kimi-code-cli/guides/sessions.html>
- <https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html>
- <https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents.html>
- <https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/config-files.html>
- <https://snorkel.ai/leaderboard/terminal-bench-2-1/>
- 二手：<https://www.marktechpost.com/2026/06/06/moonshot-ai-releases-kimi-code-cli-a-terminal-ai-coding-agent-built-in-typescript-for-next-gen-agents>

### 关于 Kimi Work

- **【事实】** 存在，是 Kimi AI 平台内的"生产力工作环境"，覆盖写作、编码、研究、演示、表格、多 agent 编排，由 Kimi K2.6 驱动（[第三方评测](https://www.eigent.ai/blog/kimi-work-moonshot-ai-workspace)，二手）。
- **其内部架构（主循环 / 上下文 / 工具 / 存储）：未找到公开资料**。Moonshot 未发布 Kimi Work 的技术博客或文档说明其 agent runtime。

---

## 产品 4：Trae（字节跳动）

### 基本信息

- 厂商：字节跳动（ByteDance）
- 开源/闭源：**产品闭源**（AI IDE / SOLO / TRAE Work）；但 **Trae Agent 开源**（MIT）：<https://github.com/bytedance/trae-agent>，另有技术报告 arXiv:2507.23370
- 语言：IDE 产品未公开；Trae Agent 为 **Python 3.12+**
- 形态：AI IDE（VS Code 分叉）、SOLO（全流程 agent，Desktop/Web/Mobile）、TRAE Work（2026-06 起的新产品线）

> ⚠️ 重要：**Trae 产品（IDE/SOLO）的 harness 架构未公开**。公开可考的是 ①官方技术博客 ②开源的 `trae-agent`。两者**不是同一个东西**（博客说的是产品内核，开源仓库是研究用的可复现实现）。下文严格区分。

### 架构（官方博客口径）

**来源**：<https://www.trae.ai/blog/product_thought_0617>（"Inside TRAE Agent 2.0"，2025-06-17，官方）

#### 主循环

**【事实，官方博客】**：

- **去掉了固定的 "Proposal" 规划阶段**。原先是 *Plan-then-Execute*（先出完整计划再执行），2.0 改为**纯 ReAct**：模型自己决定何时搜集上下文、何时推理、何时行动。
- 理由（原文）："rigid early planning sometimes led the model down the wrong path"；且"the model's follow-up actions lacked sufficient conversational context due to the initial plan being disconnected from later tool invocations"。
- 推理能力没有丢：改为**按需动态生成计划**（prompt engineering + 系统调优），并在试验 "sequential thinking tools" 这种**会话中途可调用的轻量规划模块**。
- **模式统一**：Chat 模式与 Builder 模式现在跑**同一套 agentic 工作流**，Chat 也拥有完整工具访问权；上下文在 Chat / Builder / 自定义 Agent 之间**完全共享**。
- 单层循环，无内外两层。

#### 上下文管理

**【事实，官方博客】**：

- 扩大窗口：借助更丰富的上下文 + **prompt caching**，**有效上下文窗口翻倍**，同时压低成本。
- **摘要机制**："continuously stores the raw content of your session history"，窗口接近上限时自动触发：
  - **近期轮次逐字保留**
  - **较早轮次用 LLM 摘要**，产出紧凑记忆条目，保留"高阶推理、工具使用、任务依赖"
  - 摘要**极快、不影响延迟、不消耗用户模型配额**（即使是高级模型也不扣）
- 摘要示例（原文）：旧版只记得"读过某文件"；新版摘要包含 **为什么读**、**哪些内容与当前任务相关**、**这次工具调用如何推进了问题**。
- 工具输出落盘：**未在这篇博客中说明**。→ **未找到公开资料**。

#### 工具系统

**【事实，官方博客（2.0 工具）】**：

- **简化与合并**：Builder 模式把 "create file" 与 "overwrite file" 合并为单一 action，删掉低频工具。"A leaner toolset reduces decision complexity"。
- **更聪明的编辑应用**：模型给的 pre-edit snippet 与实际文件不完全匹配时仍能成功应用编辑 → 编辑失败率可测下降。
- **读工具输出结构化**：文件夹浏览、文件读取、代码搜索返回更结构化、语义更丰富的信息。
- **`Workspace Search Tool`**：原先只服务 `#Workspace` 的代码知识图谱（CKG）被抽象成通用工具，**所有模式可用**，模型按需检索最相关的文件/函数/组件。

**【事实，开源 `trae-agent` 的工具集】**（注意：这是研究实现，不等于产品）：

- `str_replace_based_edit_tool`（view / create / str_replace / insert）
- `bash`（**持久 bash 会话**，带超时保护、输出捕获、会话管理）
- `sequential_thinking`
- `ckg_tools`（代码知识图谱：搜类与函数）
- `task_done`（**显式终止信号** —— 与"自动续写"相反的思路：让模型主动说"我干完了"）
- 架构上有 `Tool` 抽象基类 + `ToolExecutor`，**"supporting both parallel and sequential execution modes"**（这是唯一一处关于并发的公开描述，且属于开源实现）。

- 权限门禁（产品侧）：**未在这篇博客中说明**；另有单独博客 "Making AI Coding Safer: How TRAE Protects Your Codebase"（sandboxing + 多层安全，<https://www.trae.ai/blog/engineering_thought_0108>，未展开阅读）。
- 输出截断阈值：**未找到公开资料**。

#### 会话存储

- **未找到公开资料**。官方博客未涉及会话文件格式、fork/rewind。
- 开源 `trae-agent` 有 **trajectory recording**（完整记录 LLM 交互、agent steps、metadata、错误），格式未在 README 明确 → 属于"轨迹日志"，不是完整的会话树设计。

#### 扩展机制

**【事实】**：`@Agent`（用 Rules + Tools 定义自定义 agent）、**MCP**、Agent Skills（<https://www.trae.ai/blog/trae_tutorial_0115>）、TRAE Rules。具体 hook/plugin 机制未公开。

#### 子代理

- **未找到公开资料**（官方博客 2.0 未提子代理）。
- 注意区分：Trae 的 `Selector Agent` 是 **SWE-bench 评测**用的补丁选择机制（多 LLM 生成 → 回归测试过滤 → 语法聚类投票 + 多 agent 验证），**不是运行时子代理**。

#### 记忆系统

- 【事实，官方博客】"long-term memory" 指的是**会话内的**超长上下文摘要（"long-term memory within the chat session"），**不是跨会话记忆**。
- 跨会话长期记忆：**未找到公开资料**。

#### 评测

**【事实，官方博客】**：

- **SWE-bench Verified，75.2%**（500 题中 376 题），当时自称 #1（2025-06-25，Claude 4）：<https://www.trae.ai/blog/product_update_0625>
- 更早：**71.0%** with Claude 3.7（2025-05-28）：<https://www.trae.ai/blog/product_update_0528>
- 关键方法**不是**单次单 agent：
  - **Patch Generation**：单 agent，但用**多个模型各跑一遍**（Claude 4 Sonnet / 4 Opus / 3.7 Sonnet / Gemini 2.5 Pro）
  - **Patch Selection**：两个策略——baseline "LLM-as-a-Selector"，以及 **Selector Agent**（回归测试过滤 → 语法等价聚类投票 → 多 agent 验证）
  - → 这是**"多次采样 + 选择器"**范式，与 mini-SWE-agent / Codex CLI 的"单次轨迹"范式不同。**横向比较时必须注意分母不同。**
- Terminal-Bench：**未找到 Trae 的公开成绩**。

### 关键设计亮点

1. **删掉 Plan 阶段**是反直觉但有理据的：早期模型需要 scaffold，强模型上 scaffold 反而成为错误来源。这条与 Claude Code 的"每次新模型发布就删一堆代码"是同一个哲学。
2. **摘要"不消耗用户配额"** —— 把压缩成本从用户计费里摘出去，是个产品级决策（会显著影响用户对长会话的耐受度）。
3. **CKG → Workspace Search Tool 的抽象**：把"代码检索"从"某种模式的专属能力"提升为"所有模式可调的工具"。
4. **Patch Selection / Selector Agent** 用**回归测试 + 语法聚类投票 + 多 agent 验证** 做候选筛选，比 LLM-as-judge 更硬。
5. 工具集主动做减法（合并 create/overwrite），明确以"降低模型决策复杂度"为目标。

### 来源

- <https://www.trae.ai/blog/product_thought_0617>（Inside TRAE Agent 2.0，官方，一手）
- <https://www.trae.ai/blog/product_update_0625>（SWE-bench 75.2% + Selector Agent，官方）
- <https://www.trae.ai/blog/product_update_0528>（SWE-bench 71.0%，官方）
- <https://github.com/bytedance/trae-agent>（开源研究实现，MIT）
- <https://arxiv.org/abs/2507.23370>（技术报告，未展开阅读）
- <https://www.trae.ai/blog/engineering_thought_0108>（sandboxing 与多层安全，未展开阅读）
- 二手：<https://www.infoq.cn/article/cuhigtw8gwy5cw2aaxa0>（Trae 2.0 SOLO 上下文工程）、<https://api.treerouter.ai/en/blog/trae-ai-coding-solo-agent-guide>（SOLO 双 Agent 架构，**二手且未经证实，谨慎引用**）

---

## 产品 5：DeepSeek Harness / DeepSeek CLI（deepseek-ai）

### 基本信息

- 厂商：DeepSeek AI
- 开源/闭源：**开源 MIT**，Developer Preview
- 仓库：<https://github.com/deepseek-ai/deepseek-harness>（分支 `master`），CLI 名 `dsh`
- 语言：**TypeScript / Node.js**，插件框架 **Cordis**
- 发布：2026-08-13（随 DeepSeek V4 Pro），测试版本 `0.1.1-rc.2`
- 文档：仓库 `docs/` 目录极为详尽（`architecture.md`、`docs/subsystems/*.md` 每子系统一页）

> 说明：本机 `D:\soft\deepseek\resources\app.asar\dsh\` 即此项目生产构建。下文以官方 docs 为准。

### 架构

#### 主循环

**【事实】**（`docs/architecture.md`）：

- **核心命题：一切皆插件**。模型适配器、工具注册表、会话日志、**以及 agent loop 本身**都是插件，都可以从配置里替换。"There is no privileged core to patch"。
- 层级：**profile（命名组合）→ bundle（配置行 + 代码的分发格式）→ plugin tree**。内置 profile：`web` / `headless` / `sdk` / `sdk-minimal` / `acp`。`dsh-base` 是共享第一层。
- **Turn / Step 模型**（原文伪码）：
  ```
  turn/start
    claim next-step input plus one queued message
    snapshot Session model selection; assemble prompt sections + step-admitted tool schemas
    -> agent/pre-step; project runtime context at fallback
    step/start
    agent/request -> prepareCall
    reconcile system/message using the prepared call capability
    append entered messages as user/message
    log request/header and request/context as needed
    derive and freeze model history from the log
    stream the bound prepared call -> llm/stream -> agent/assistant-stream start
       tool/call* -> tools/pre-execute -> tools/execute -> tools/post-execute -> tool/result*
    step/end
    tools owe another request, or next-step input arrived -> claim -> next step
    -> agent/turn-stopping
  turn/end
  ```
  - **一个 step = 一次模型请求 + 它调用的工具；一个 turn = 0..N 个 step**。
  - 循环**靠"ouwes"驱动**："tools owe another request, or next-step input arrived" → 自动续跑；`agent/turn-stopping` 是**串行无 next() 的终止钩子**（可拦截停止）。
  - 【事实】"**No built-in turn budget** — tool calls or steering continue the current turn; a policy that bounds runaway turns must cancel from an existing lifecycle extension point"（`packages/core/agent-loop/README.md` 已知局限）——**默认无限续写**，上限靠插件。
- **三类事件域**：
  - **Session events**（持久事实，追加进 log，经 `session/event` 广播）
  - **Agent events**（`agent/*`：inbox、step、status、request、validation、continuation —— 活对象，可拦截）
  - **Capability events**（`fs/*`、`tools/*`、`telemetry/*` 挂策略与适配器）
- **瀑布式中间件**：`agent/pre-step`、`agent/request`、`llm/stream`、`tools/pre-execute`、`tools/execute`、`tools/post-execute` 都是 **waterfall**（监听器必须 `next()` 才能继续）；`agent/turn-stopping` 是 **serial**（无 next）。
- 取消与重试：请求不可变 + 活取消；失败 step 会"记录缺失的工具结果"。

#### 上下文管理

**【事实】**（`docs/subsystems/compaction.md`、`packages/compaction/compaction-basic/README.md`、`docs/subsystems/spill.md`）：

- **压缩是一个 capability seam，不是 loop 的主干**：Service Definition `ctx.compaction`（`dsh-compaction`）+ Provider（`dsh-compaction-basic`）+ Consumer（`/compact` 命令）。意味着**可以整体换掉压缩算法**（tokenizer 版、模板版都是同级包）。
- **默认阈值公式（原文）**：`trigger = floor(min(W × 0.8, W − O − B))`
  - `W` = 上下文窗口，`O` = 有效请求输出上限，`B` = headroom，**默认 65,536 token**
  - **保留最近 16% 的 `W − O` 逐字不动**（`retainRatio = 0.16`，或改用 `retainTokens`）
  - 可 per-model override（示例：`thresholdRatio: 0.7, retainTokens: 2048`）
  - 配置非法直接 fail-fast（阈值比 ≤ 保留比、重复 override、非法 token 数 → 插件加载失败）
- **两端压缩**：`thresholdRatio 0.8` 压上游，`headroomTokens` 默认 64K 留输出；小窗口部署必须显式配 headroom，否则 `W − O − B` 为负直接报错。
- **两级触发**：
  - 压力：`agent/pre-step` 监听器在请求派生**之前**用 `ctx.tokenMeter` 计价
  - 溢出：`agent/request-error` 对 `CONTEXT_WINDOW_EXCEEDED` **绕过阈值和保留策略**，做一次"最大均衡头部削减"，**只有 surface replacement generation 前进后才授权重试**
- **压缩前先剪工具输出**：可选 `dsh-compaction-tool-result-pruner`（**不调模型**）；剪完若能塞进阈值就完全跳过摘要。**剪枝只在触发条件满足后才跑**，低于压力阈值的会话永不被碰。
- **压缩产物是 session 事件**：`compaction/start`（拿锁）→ 摘要 → `compaction/summary`（含 `shadowedRange`、`shadowedSeqs`、`shadowedTokenCount`、`provider`、`model`、`maxTokens`、`usage`）→ `compaction/end`（放锁）。
  - 摘要本身通过一条 **`user/message` + `surfaceOp: { op: 'replace', startSeq, endSeq }`** 落地 —— 这是压缩对"模型可见面"的**唯一一次修改**。
  - **锁在最后才释放**：崩溃会留下"孤儿锁"（有 start 无 end），可被检测；反之若先放锁则会误判完成。
- **压缩请求的可复现性**：`llmStreamCall: true` 标记 + 完整 `rawOutput` 落进日志 —— 一次性的摘要请求可以**从 log + code 完全重建**。
- **工具输出落盘（spill）**：`dsh-spill` seam + `dsh-spill-local` provider + `dsh-spill-policy` consumer。
  - 配置 `maxInlineTokens`（示例 12500）；**省略即关闭**。
  - 保留**头尾各一半**预算，中间插入省略通知：
    ```
    <retained head/tail preview>
    (Omitted N bytes. Full formatted result stored at: /…/session-…/…-web_fetch.txt. Use read with offset/limit, or grep this path to search within it.)
    ```
  - 图像整体保留或整体省略（不切图），附件字节不复制进结果文件；本地附件对象独立于 spill 清理存活。
  - **失败降级**：spill 后端缺失 / 存储失败 / 图片路径不可用 → 打警告并**保留原文**（不拿不可读路径替换图片）。
- **图像卸载**：`dsh-compaction-image-offload` 单独负责 `image/offload` 声明与消息投影。
- **prefix cache 保护是显式设计目标**（`packages/core/agent-loop/README.md`）：
  - "Prefix reuse requires earlier messages and declarations to stay byte-identical under the same provider and model route."
  - 系统提示以 **`system/message` 历史**形式存在：能力支持 `systemPromptUpdate: 'in-history'` 的路由，**把新的 system prompt 追加在历史之后**，从而"the prefix through that history stays reusable"；不支持的才在 node 0 原地替换（必然 cache miss）。
  - **新会话轮次（new request series）**、工具声明重建、图像卸载决策都会打断复用，代码里明确列出这些失效点。
  - 压缩请求自己也会"preserve the provider's warm prefix where projection permits"。

#### 工具系统

**【事实】**（`docs/subsystems/tools.md`、`packages/core/tool-executor-native/README.md`）：

- 工具是 `ToolDefinition`：`ToolSchema`（对外）+ **强制的 canonical output 声明**（`ToolOutputDefinition.schema` / `.render()`）+ `execute(args, exec)` + **仅宿主的调度元数据**（`isConcurrencySafe`、`maxParallelToolCalls`、`timeoutMs`）+ 可选 presenter。
- **注册表用显式 allowlist 生成 `schemas()`**：`output`/`execute`/`projectContent`/`finalizeContent`/`timeoutMs`/`isConcurrencySafe`/`presentCall`/`presentResult` **绝不能泄漏进模型请求**。这是个很干净的边界。
- **并发模型——本调研里最可配置的**：
  - 执行器本身是 **插件**（`tool-executor-native` / `tool-executor-ptc`），"Choose your executor"。
  - Native 执行器：**"Parallel-safe calls can overlap; exclusive calls form barriers, and results remain in model order."**
  - 并发上限：`maxParallelToolCalls`（每批次可覆盖），**native 默认 10**，`1` = 串行；"The loop reads the limit once per batch"。
  - 并发安全性由工具自己声明：`isConcurrencySafe?(args) => boolean`；"recorder races are permitted only when they commute or fail closed"。
  - **结果严格保持模型给出顺序**（不是完成顺序）—— 对 prompt 稳定性友好。
- **工具暴露面（ToolExposures）**：`DIRECT`（进初始工具列表）/ `DEFERRED`（可被 tool search 发现）/ `CODE_MODE`（可在嵌套脚本里调用）/ `ALL`。
- 执行管道：`tools/pre-execute` → `tools/execute` → `tools/post-execute`（waterfall），`tool/result*` 再落日志。取消通过 `exec.signal` 传播；"it cannot hard-kill same-process code"（诚实标注了同进程代码无法强杀）。
- 权限：`approval.md`（one-shot 审批 seam，含 audit events）、`permission-presets.md`（preset 层 + 派生 custom 态）、`sandbox.md`（per-session 策略 + `ConfinedArgv`，**fail-closed**）。
- 输出上限：由 `spill-policy` 的 `maxInlineTokens` 控制（见上）。
- 其他工具 seam：`shell` / `subprocess` / `terminal` / `fs` / `lsp` / `web` / `jobs` / `ptc-runtime` / `computer-use` / `browser-use` —— 全部可换 provider，且"filesystem and subprocess providers share one execution world, so pointing them at a remote sandbox moves Bash, PTY, and LSP with them"。

#### 会话存储

**【事实】**（`docs/subsystems/session.md`、`persistence.md`）：

- **模型可见即已记录**（"Model-visible means logged"）：`deriveMessages()` 从事件日志**投影**出模型历史，历史**从不单独存**。回放 = 用同一批事件重新投影。
- **追加式事件日志**，`SessionEventMap` 可被插件用 declaration merging 扩展（如 compaction 加 `compaction/*`，hook bridge 加 `hook/*`）。
- **持久化也是 seam**：`SessionPersistence` 抽象 + 内置 `dsh-session-persistence-jsonl` provider：
  - 每 session 一个逻辑 JSONL，物理上默认是**带校验和的连续 Zstandard 帧**（可配原始行）
  - **崩溃安全**：原子物化、每批 `fsync`、首次新追加前截断撕裂尾帧（能部分解码）
  - **写后批处理**（write-behind window）：`session/event` 同步通知，后端按 session id 路由进有界窗口；`session/flush` 取消耗并抽干 → 循环用它做"下一个 turn 前"的顺序/错误观测点
  - **版本化迁移**：v0 `session.jsonl[.zstd]`，v1+ `session.vN.jsonl[.zstd]`；相邻 `vN → vN+1` 迁移包各自负责一步；**已提交的代际路径永不重命名/替换/删除**；写入前先编码、校验、**独占发布**新代际
  - 未来版本直接拒绝（即使还有可读的旧代际）
- **类别**：日志线性（seq 从 0 连续、永不重写），但**结构可 fork**：`ctx.agents.create({ sessionId, seed, inheritedEventCount, meta: { isSeeded: true } })` —— 在 turn 边界 fork。
- 投影（projection）单独一层：`ctx.sessionProjections` 注册纯函数 fold，客户端通过 `snapshot()` 读一致切面。
- 另有 `session-query`（语义过滤 + 全文检索分页）、`storage`（通用 backend）、`session-reference`（跨会话结构化引用）。
- 【第三方】`ctx.sessionPersistence` 有 JSONL 和 SQLite 两个后端可选（[DeepInfra 实测报道](https://deepinfra.com/blog/deepseek-harness-review)，二手）。本次一手中只确认了 JSONL provider。

#### 扩展机制

**【事实】** —— 本调研里扩展面最宽的：

- **Cordis 插件**（一切皆插件；注册即 effect，卸载自动回滚 —— "spatiotemporal composability"）
- **MCP**（`docs/subsystems/mcp.md`）
- **Skills**（`docs/subsystems/skills.md`：发现优先级、session-prefix 目录、模型侧 `skill` 加载）
- **Hooks**（`@deepseek-ai/dsh-hook-protocol`，还能**读取已有的 Claude Code hooks.json** 桥接）
- **Extensions**（`docs/subsystems/extensions.md`：版本化动态 Cordis Plugin/Package、Host/Client 激活、审批、运行时自省、生命周期拆除）
- **Agent preset / isolate realm**：给单个会话不同的能力集
- **profile + patch 文件**：`--patch` 覆盖任意配置行
- **Claude Code mods 桥**（实验性：`claude-code-mods.md`，把事件、`$` 成员、链路规则都映射过去）

#### 子代理

**【事实】**（`docs/subsystems/subagent.md`）：

- **统一的激活 API**：`startActivation` / `prepareContinuable` / `start`。
- **provider 注册表**：六种后端（第三方报道列举），包括 **`subagent-claude-code`** 和 **`subagent-codex`** —— 即**可以把任务委派给竞品 agent**（[DeepInfra](https://deepinfra.com/blog/deepseek-harness-review)，二手但描述具体）。
- 每个激活有 `delivery: 'parent' | 'caller'`：`parent` 会通知模型（插入父上下文），`caller` 只把结果返回调用者（不进 prompt）。
- 能力声明 `SubagentCapabilities`：`agentOptions` / `agentPreset` / `outputSchema` / `depthLimit` —— 每个请求选项在启动前校验。
- `Agent Teams`：root Session 开启后加任务策略 + **共享任务板（task DAG）**。
- 隔离：本地子代理是**独立 Session**，继承父的 project 与 cwd（可指定）；冷恢复保留其记录目录。

#### 记忆系统

- 【事实】有 `ctx.goals`（同会话目标，跨 round 持续）、`session-reference`（跨会话引用）、`skills`（过程性记忆）。
- **跨会话长期记忆的自动提炼**：`docs/subsystems/` 中没有 memory 页面（`memory.md` 404）→ **未找到公开的一手说明**。
- 【第三方】DeepInfra 的实测报道也未提记忆流水。→ 结论：**dsh 目前未见公开的跨会话自动记忆实现**（对比 Codex 的 Phase1/Phase2 是明确反例）。

#### 评测

- **未在 Terminal-Bench / SWE-bench 官方榜上找到 dsh 条目**（[Snorkel TB2.1 榜](https://snorkel.ai/leaderboard/terminal-bench-2-1/) 18 条无 dsh）。
- 【第三方，谨慎】MindStudio 提到 "Frontier Harness Eval，用同一模型（Kimi K3）跑 9 个 harness，DSH 以极低成本达到 Claude Code 的 pass rate"（<https://www.mindstudio.ai/blog/deepseek-harness-plugin-architecture>，**二手，未找到原始榜单，数字不可引用**）。
- → **官方 benchmark 成绩：未找到公开资料**。

### 关键设计亮点

1. **loop 本身就是插件**。这是本调研里最激进的一条：连 `agent-loop` 都能换，"框架"和"应用"的边界被彻底重画。
2. **压缩默认公式写死得很清楚**：`min(W×0.8, W−O−64K)` + 保留 16%，且**非法配置 fail-fast 而不是静默降级**。可复现、可审计。
3. **压缩的锁放最后**：把"崩溃"映射成"可检测的孤儿锁"，而不是"假的完成"。这是事件溯源的正确姿势。
4. **压缩请求双向可复现**：`llmStreamCall: true` + `rawOutput` 全量落日志，日志+代码足以重建那一次摘要调用。
5. **prefix cache 是一等目标**：system prompt 走 `system/message` 历史，能追加就不替换；代码里逐条列出"哪些操作会破坏前缀复用"。
6. **工具注册元数据与模型视图强隔离**（allowlist 生成 schema），执行器可插拔（native / PTC），并发靠工具自己声明 `isConcurrencySafe`，**结果顺序仍按模型给出的顺序**。
7. **spill 的降级策略**：失败就保留原文，绝不拿坏路径糊弄模型。
8. **子代理 provider 可以是竞品**（claude-code / codex），把"编排"和"执行"彻底解耦。
9. **会话格式版本化迁移**：已提交代际永不改写，写入走"编码→校验→独占发布"，比多数产品的"就地迁移"安全一档。

### 来源

- <https://github.com/deepseek-ai/deepseek-harness>（仓库，MIT，developer preview）
- `docs/architecture.md`（一手，最核心）
- `docs/subsystems/{session,tools,compaction,persistence,spill,subagent,extensions,approval,sandbox,agent-team,mcp,skills,goal}.md`
- `packages/compaction/compaction-basic/README.md`（阈值公式一手来源）
- `packages/spill/spill-policy/README.md`（`maxInlineTokens` 与降级策略一手来源）
- `packages/core/tool-executor-native/README.md`（并发语义一手来源）
- `packages/core/agent-loop/README.md`（prefix reuse 与 no-turn-budget 一手来源）
- 二手：<https://deepinfra.com/blog/deepseek-harness-review>、<https://www.mindstudio.ai/blog/deepseek-harness-plugin-architecture>、<https://theneuralfeed.com/article/deepseek-harness-v0-1-released-as-open-source-developer-preview/v8iJpU72>
- 本机：`D:\soft\deepseek\resources\app.asar\dsh\`（同一项目的生产构建）

---

## 产品 6：WorkBuddy（腾讯）

### 基本信息

- 厂商：**腾讯**（腾讯云 CodeBuddy 团队）
- 开源/闭源：**闭源商业产品**（有企业版、SaaS、桌面端、小程序端、移动端）
- 形态：**全场景 AI 办公工作台**（桌面客户端）+ **企业智能体云平台**（云端 Runtime/Session）+ CLI（`codebuddy`）+ MCP server（`workbuddy-mcp`）
- 官网：<https://cloud.tencent.com/product/workbuddy>、<https://www.workbuddy.cn/>
- 文档：<https://www.workbuddy.cn/docs/workbuddy/>、企业版 <https://cloud.tencent.com/document/product/1831/134384>
- 语言/仓库：**未公开源码**

> 命名澄清：任务里只说 "WorkBuddy"。经核实这是**腾讯**的产品（不是 OpenAI/字节/月之暗面），且与腾讯的 CodeBuddy（同名不同产品：CodeBuddy = AI 编程 IDE/CLI，WorkBuddy = AI 办公工作台）**同源团队、共享底座**。

### 架构

#### 主循环

**【事实，企业版文档】**（<https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/CloudAgent>）：

三层资源模型 —— **Agent / Runtime / Session**：

- **Agent**：完整配置（模型 + system prompt + skills/plugins/mcp/subagents + workspaces + secrets/envs），以声明式 **`Manifest`（JSON，`manifestVersion: "1.0"`）** 描述。
- **Runtime**：**一个云端沙箱实例**（完整 Linux 文件系统 + 终端）+ Agent 的 Manifest + 一个或多个 Session。状态机：`运行中` / `休眠`（空闲超时自动暂停，再访问自动唤醒）/ `失败`。
- **Session**：一次完整对话过程，独立历史与文件；**超过 10 分钟无访问自动休眠**。
- 交互协议：**ACP 协议 + SSE**（"通过 ACP 协议、SSE 协议发送指令，接收 Agent 的流式输出和工具调用结果"）。→ **事件驱动的流式架构**。
- **Checkpoint / Version**：支持创建快照、版本发布、**随时回滚到历史状态**。
- **主循环形态**：官方文档只描述为"理解 → 补齐上下文 → 规划 → 调工具 → 检查 → 生成交付物 → 迭代"，**未公开是单层 ReAct 还是两层**。→ **未找到公开资料**。
- 【二手】独立博客归纳为经典 Agent Loop（"理解目标 → 补齐上下文 → 规划步骤 → 调用工具执行 → 检查中间结果 → 生成交付物 → 根据反馈迭代"）：<https://www.cnblogs.com/tgzhu/p/21629693>（作者本人明确声明"腾讯公开资料重点披露的是产品能力、组件边界和使用方式，并未完整公开其内部的模型调度、任务编排和执行运行时"）。

#### 上下文管理

- **未找到公开资料**（无压缩策略/阈值/prefix cache 的公开说明）。
- 可确认的**相邻能力**：
  - 【事实】Session 休眠/唤醒机制（10 分钟无访问休眠，唤醒恢复）—— 会话持久化的产品化表现。
  - 【事实】知识库 RAG：Agent 高级配置可关联官方知识库与企业自定义知识库，"Agent 可通过 RAG 检索获取知识库中的相关信息"。
  - 【事实】"记忆"开关：开启后"记住多轮对话中的重要信息，跨轮次保持上下文"（企业版 Manifest 层面的描述）。
  - 【二手，有具体路径】本地/个人记忆文件：`~/.workbuddy/memory/xxx_memory.md`、`USER.md` / `SOUL.md` / `IDENTITY.md`、workspace 级 `<Workspace>/.workbuddy/memory/YYYY-MM-DD.md`；本地 DB `~/.workbuddy/workbuddy.db`（<https://www.cnblogs.com/tgzhu/p/21629693>）—— **文件路径级别的细节只能来自实测，标注为二手**。

#### 工具系统

- **扩展三件套（区分清晰，是它的设计亮点）**：
  - **Skill**：一组可执行脚本 + 工作流 + 工具白名单 + 模板，形态以 **`SKILL.md`** 为核心（与 Claude Code / Codex 的 Skills 概念一致）。"不是单纯给模型更多提示词，而是把流程、知识和执行方式一起固化"。
  - **Connector**：连外部系统（QQ 邮箱、腾讯乐享、腾讯文档、TAPD、微云、自定义连接器），**独立授权、不主动定时抓取、不越权**。
  - **MCP**：标准工具协议，"把外部世界的能力变成 Agent 可发现、可调用、可授权的工具"。
- 【事实】Manifest 能力字段：`rules[]`（行为规则文件）、`skills[]`、`plugins[]`、`mcp[]`、`subagents[]`。
- **权限门禁**（产品侧，【事实】）：
  - 两档：**默认权限** / **完全访问权限**。
  - 默认权限下以下动作需二次确认：写敏感路径、删除重要文件/目录、批量删除、执行脚本/命令/外部程序、网络访问或敏感能力调用。
  - "命令会优先在沙箱约束下执行，删除操作会尽量通过安全删除或回收站机制降低误删风险"（二手归纳，与官方文档一致）。
  - 敏感系统目录被拦截；文件默认本地处理，只能访问用户授权文件夹。
- **凭据管理**（【事实】）：凭据**加密存储**，仅在 Agent 运行时由代理层解密并注入请求头；`secrets` 支持 `${VAR_NAME}` 引用，**通过安全通道注入，不写进沙箱文件**；系统自动注入 `AGENTOS_RUNTIME_ID` 环境变量（注意这个名字暗示底层平台为 "AGENTOS"）。
- **并发模型 / 输出截断阈值**：**未找到公开资料**。

#### 会话存储

- 【事实】**Session 是云端一等资源**：每个 Session 独立对话历史 + 独立文件存储；Runtime 与 Session 通常一一对应；删除 Session 永久清除记录与文件。
- 【事实】**Checkpoint / Version 双轨**：可随时回滚到历史状态、可发布版本。→ 比多数 CLI agent 的"线性 JSONL"更接近**带版本的快照模型**，但格式未公开。
- 【事实】**渠道绑定**：企微 AIBot / QQ 机器人 / 飞书 / 钉钉，**一条接入记录 = 一个 Bot → Session 的绑定**。
- 【二手】本地底座 `~/.workbuddy/workbuddy.db`（任务/会话/状态/历史持久化）。
- → 会话格式、是否支持 fork/rewind 的**细节：未找到公开资料**（但产品层明确有"回滚到 Checkpoint"，等价于 rewind）。

#### 扩展机制

- 【事实】**Skills + Expert（专家）+ Connector + MCP + Plugin + Subagent（Manifest 字段）**。
- 【事实】**SkillHub 专家市场**：100+ 领域专家、7 万+ Skills。
- 【事实】**Plugin** 字段存在于 Manifest；`codebuddy plugin install` 支持非交互安装（<https://intl.cloud.tencent.com/document/product/1316/84467>）。
- 【事实】**评估能力**：企业版可"选择目标 Agent 和评测数据集，系统自动逐条运行测试用例并按指定评分方式计算得分"—— **自带 agent eval 平台**，这是本调研中少见的产品化能力。

#### 子代理

- 【事实】Manifest 有 `subagents[]`；产品层的"**专家团**"——"由团长自动拆解任务，安排成员并行执行，并整合成最终交付结果"；"多 Agent 并行推进：将同一目标拆分给多个 Agent 同时处理，各自负责其中一部分，结果统一汇总回流"。
- 上下文隔离方式：**未找到公开资料**。
- 【二手，作者已声明为推断】"至于一个复杂任务内部是否会像工程化多 Agent 系统一样自动拆成多个子任务并发执行，桌面端公开文档尚未给出明确的配置与运行说明"。

#### 记忆系统

- 【事实，产品层】Memory 开关 + 知识库 RAG（见上）。
- 【二手，有文件名级细节】四层上下文：**云端账号 Memory**（跨设备）/ **用户级 Memory**（跨 Session，`~/.workbuddy/memory/*.md`）/ **Workspace Memory**（项目日志 `.workbuddy/memory/YYYY-MM-DD.md`）/ **Task-Session**（当下）；另有用户可显式维护的 `USER.md` / `SOUL.md` / `IDENTITY.md`。
- 提炼机制（何时抽、抽什么、如何注入）：**未找到公开资料**。

#### 评测

- **未找到 WorkBuddy 的公开 benchmark 成绩**（SWE-bench / Terminal-Bench 均无）。
- 【事实】产品内置**评测平台**（自建数据集 + 自动跑 + 评分），但不出现在公开榜单。

### 关键设计亮点

1. **Agent / Runtime / Session 三层资源模型**：把"沙箱生命周期"和"对话生命周期"分开管（Runtime 休眠 ≠ Session 删除），并且 Runtime 是声明式 Manifest 驱动 —— 这是**平台化**而不是 CLI 化的设计。
2. **ACP + SSE 作为对外的 agent 协议**：让第三方（IM 渠道、企业系统、API Key 调用方）以统一方式驱动 agent。
3. **Checkpoint / Version 双轨**：可回滚 + 可发布版本，比"仅会话 resume"更接近软件交付的语义。
4. **Skill / Connector / MCP 三分**：Skill = 流程固化，Connector = 系统授权接入，MCP = 工具协议 —— 三者定位不重叠，文档写得比多数同类清楚。
5. **凭据代理注入**：secrets 走安全通道注入、不进沙箱文件，运行时由代理层解密注入请求头。
6. **自带 Agent 评测平台**：可以把"agent 好不好"变成可量化的产品内能力。

### 来源

- <https://cloud.tencent.com/product/workbuddy>（官网产品页，一手）
- <https://cloud.tencent.com/document/product/1831/134384>（WorkBuddy Enterprise 产品简介，一手）
- <https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/CloudAgent>（企业智能体 / Runtime / Session / Manifest，**最核心的一手来源**）
- <https://intl.cloud.tencent.com/document/product/1316/84467>（CodeBuddy CLI 命令参考）
- 二手：<https://www.cnblogs.com/tgzhu/p/21629693>（"WorkBuddy 运行逻辑解读"，2026-07-18，作者已声明为产品逻辑归纳而非内部实现）、<https://blog.csdn.net/EAlReport/article/details/160894027>、<https://zhuanlan.zhihu.com/p/2017886644569666895>（知乎，反爬未读到）

---

# 开源参考基线

## A. OpenHands（原 OpenDevin）

### 基本信息

- 组织：All Hands AI / OpenHands（charm 后改名）
- 开源：**MIT**（核心），<https://github.com/OpenHands/OpenHands>（原 `All-Hands-AI/OpenHands`）
- 语言：**Python**（SDK）+ TypeScript（Agent Canvas 前端）
- 现状（2026）：仓库主推 **Agent Canvas**（自托管 coding agent 控制中心），可运行 OpenHands / Claude Code / Codex / Gemini CLI / Pi / OpenCode，任何 **ACP 兼容 agent**
- 架构已重写为 **OpenHands V1 / Software Agent SDK**（4 个包：`openhands-sdk` / `openhands-tools` / `openhands-workspace` / `openhands-agent-server`）

### 架构

#### 主循环

- 【事实】V1 的 `Agent` 是 **stateless Pydantic 模型**，唯一的可变状态是 **ConversationState**（`openhands.sdk.event.conversation_state`）——"one source of truth that enables deterministic replay and robust persistence across sessions or distributed systems"。
- 循环仍是经典的 step → 模型 → 工具 → 事件追加；复杂度被推到 **事件流 + 状态对象**上。
- 【事实】三个设计原则（<https://docs.openhands.dev/sdk/arch/design>）：
  1. **Optional Isolation over Mandatory Sandboxing**（V0 强制 Docker，V1 默认同进程，需要时透明容器化）
  2. **Stateless by Default, One Source of Truth for State**
  3. **Clear Boundaries between Agent and Applications**（SDK / tools / workspace / agent server 四层，应用通过 API 而非嵌入）
  4. Composable components
- V0 的痛点被明确列出：agent 与 sandbox 分属两个进程、状态会漂移、多租户互相拖垮、MCP 假定本地执行。

#### 上下文管理

- 【事实】**`LLMSummarizingCondenser`**（默认实现），继承 `RollingCondenser`：
  - 触发条件：**历史事件数 > `max_context_length`**（注意：是**事件条数**，不是 token）
  - 保留：**`keep_first` 个事件永远保留**（system prompt、初始 user message）
  - 丢弃区间替换为 LLM 摘要
  - `llm.model_copy(update={"usage_id": "condenser"})` —— **condenser 有独立 usage 计量**
- 【事实】官方称收益：**每轮 API 成本最多降 2x**，长会话响应时间稳定，"equivalent or better performance on software engineering tasks"。
- 可扩展：`CondenserBase` 自定义实现。
- prefix cache：未在文档中作为设计目标提及 → **未找到公开资料**。
- 工具输出落盘：**未找到公开资料**（有 `Persistence` 但那是会话持久化）。

#### 工具系统

- 【事实】**`tool_concurrency_limit`**（`Agent` 参数）：
  - **默认 `1` = 串行**（"By default, `tool_concurrency_limit` is set to `1` (sequential execution)"）
  - `2–8` 中度并行；`>8` 仅限 I/O 密集且工具独立
  - 官方**明确标注为 experimental**，并警告"Concurrent execution can lead to race conditions or unexpected behavior for tools that share state"
  - **每个子代理可以有自己独立的并发上限**
- 核心工具：`TerminalTool`、`FileEditorTool`、`TaskToolSet`（子代理委派）。
- 权限/安全：`Security & Action Confirmation` guide、`SecretRegistry`。
- 输出截断：**未找到公开资料**。

#### 会话存储

- 【事实】`Persistence` guide + `ConversationState` 单源真相 + **deterministic replay**。
- 具体格式（文件/DB）：**未找到公开资料**（文档只给 API 层）。
- 分布式：agent-server 是 FastAPI（HTTP + WebSocket），REST/WS 端点覆盖 conversations / bash / files / events / desktop / VSCode。

#### 扩展机制

- 【事实】MCP、**Plugins**、**Agent Skills & Context**、Custom Tools、Structured Output、Task Tool Set、Metrics/Observability(Tracing)。
- 【事实】**ACP（Agent-Client Protocol）**：Canvas 可作为 Claude Code / Codex / Gemini CLI 的宿主。

#### 子代理

- 【事实】`TaskToolSet`，可与 `tool_concurrency_limit` 组合做**并行子代理委派**（"Delegating to multiple sub-agents at once"）。

#### 记忆系统

- 【事实】`Persistent Memory` 是独立的 SDK guide（说明有一等公民的长期记忆 API）；具体实现未展开 → **部分未找到公开资料**。

#### 评测

- 未在 Terminal-Bench 2.1 榜上以 OpenHands 名义出现。SWE-bench 历史成绩在 V0 时代常见引用，但本次未取得可靠一手数字 → **未找到更新的公开数字**。

### 关键设计亮点

- **V1 的核心是把"状态"提纯**：agent/tool/LLM/config 全不可变，唯一可变对象是 ConversationState → 重放、持久化、分布式同步全部免费。
- **"可选隔离"**：V0 的教训是强制沙箱会与 MCP 的本地执行假设冲突。
- **并发默认关**（=1），要开就自己担风险，并给出 2–8 的经验值。

### 来源

- <https://github.com/OpenHands/OpenHands>（README）
- <https://docs.openhands.dev/sdk/arch/overview>
- <https://docs.openhands.dev/sdk/arch/design>（V0 痛点 + V1 四原则，一手）
- <https://docs.openhands.dev/sdk/guides/context-condenser>（一手）
- <https://docs.openhands.dev/sdk/guides/parallel-tool-execution>（一手）
- <https://github.com/All-Hands-AI/OpenHands>

---

## B. SWE-agent / mini-SWE-agent（Princeton）

### 基本信息

- 组织：Princeton + Stanford（John Yang, Carlos Jimenez, Kilian Lieret, Shunyu Yao, Karthik Narasimhan, Ofir Press）
- 开源：**MIT**，<https://github.com/SWE-agent/SWE-agent>；后继 <https://github.com/SWE-agent/mini-swe-agent>
- 语言：**Python**
- 论文：arXiv:2405.15793
- ⚠️ **官方已声明开发重心转向 mini-swe-agent**（"mini-SWE-agent has superseded SWE-agent. It matches the performance of SWE-agent, while being much simpler."）

### 架构

#### 主循环

- 【事实】<https://swe-agent.com/latest/background/architecture/>：
  - 入口 `sweagent` → `SWEEnv`（V1.0 起是 [SWE-ReX](https://swe-rex.com) 的薄封装）→ 部署（本地 Docker / modal / aws）
  - 容器内 SWE-ReX 起 shell 会话，并把 **ACI（Agent-Computer Interface）** 元素装成 custom tools
  - `Agent.forward()` —— **最核心的方法**：prompt 模型 + 执行其动作
  - 模型输出用 **parser** 抽出 action，交给 `SWEEnv` 执行
- **单层 ReAct**，无内外循环；无自动续写概念（连续 forward 直到 `submit` 或预算耗尽）。
- **配置单一 YAML 文件**驱动（"Governed by a single yaml file"）—— 这是它作为 research platform 的核心卖点。

#### 上下文管理

- 【事实】**`HistoryProcessor`**（第 7 步）：历史在送进模型前被压缩，"In order to make the best use of the context window of the model, the history gets compressed by a HistoryProcessor"。
- 具体策略（last-N / 观测折叠等）由配置选，非单一固定策略。截断阈值也是配置项 → **无统一默认值**。
- 这是**最早把"上下文压缩"做成独立可替换组件**的设计之一（早于多数商用产品）。

#### 工具系统

- 【事实】**ACI（Agent-Computer Interface）** 是它的学术贡献：不是"给模型 shell"，而是给模型一套**为 LM 优化过**的命令集（带 linter、简洁报错、文件查看窗口等）。
- 工具通过 SWE-ReX 在容器内装成 shell 命令 —— **所有动作最终都是 bash**，不是函数调用 JSON。
- **并发：串行**（单一 shell 会话，天然串行）。
- 权限门禁：无（容器隔离代替权限）。

#### 会话存储

- 【事实】**Trajectory（`.traj` JSON）** 文件，含每一步的 prompt/response/action/observation + 元信息，可用 `inspector` 回看。
- 线性，无 fork/rewind。批量模式（batch）为 SWE-bench 而生。

#### 扩展机制

- 【事实】自定义 tools（`docs/config/tools.md`）、custom bundles、demonstrations、templates、environments。**无 MCP**（早于 MCP）。

#### 子代理

- **不支持**（单 agent，单 shell）。

#### 记忆系统

- **无**（每 issue 独立一个容器）。

#### 评测

- 【事实，官方】SWE-agent 1.0 + Claude 3.7 曾为 SWE-bench full & verified 的 SoTA（2025-02）；SWE-agent 在 SWE-bench 上是开源项目 SOTA。
- 【事实，官方】**mini-swe-agent**："Scores >74% on SWE-bench verified"、"achieves 65% on SWE-bench Verified in 100 lines of python"（2025-07-24 的公告）。
- 【事实】Terminal-Bench 2.1 榜上：**Muse Spark 1.1 + mini-SWE-agent = 76.2%**（第 9）。

### 关键设计亮点

- **"ACI 比模型更重要"**：把工具接口设计当作研究对象，而不是把 shell 直接丢给模型。
- **单 YAML 驱动全部行为**：可复现性最强，最适合做消融实验。
- **mini-swe-agent 的极简主义**：100 行 Python 拿到 65%+，反证了"复杂度未必带来收益"—— 横向对比时是**对照组的地板**。

### 来源

- <https://github.com/SWE-agent/SWE-agent>（README，含 mini-swe-agent 迁移声明）
- <https://swe-agent.com/latest/background/architecture/>（一手架构图与流程）
- <https://github.com/SWE-agent/mini-swe-agent>（README，>74% SWE-bench verified）
- <https://arxiv.org/abs/2405.15793>
- <https://snorkel.ai/leaderboard/terminal-bench-2-1/>

---

## C. Aider

### 基本信息

- 作者：Paul Gauthier（Aider-AI）
- 开源：**Apache-2.0**，<https://github.com/Aider-AI/aider>
- 语言：**Python**，pip `aider-chat`（680 万次安装，周处理 150 亿 token，README 自述）
- 定位：终端里的 AI pair programming

### 架构

#### 主循环

- 【事实】**不是"工具调用 agent"，而是"编辑指令 agent"**：模型被要求按特定**编辑格式**（`diff` / `diff-fenced` / `whole` / `udiff`、`editor-diff` / `editor-whole`）输出编辑指令，aider 解析后应用到本地文件。没有 `read_file`/`write_file` 这类工具调用循环。
- 因此"主循环"更接近：**每次用户消息 → 组装 prompt（含 repo map + 聊天文件 + 只读文件）→ 一次或两次推理 → 解析编辑 → 应用 → lint/test → auto-commit**。
- **四种 chat mode**【事实】：`code`（改代码）/ `ask`（只讨论）/ `architect`（**双模型两阶段**）/ `help`。
- **架构模式（architect）**：Architect 模型先给方案（随便怎么描述），Editor 模型再把方案翻译成精确编辑格式。理由：o1 这类强推理模型**不擅长守格式**，"the model has to split its attention between solving the coding problem and conforming to the edit format"。**本质上是把"推理"和"格式合规"分成两次推理**。
- 无自动续写；无子代理。

#### 上下文管理

- 【事实】**Repo map**（<https://aider.chat/docs/repomap.html>）：用 tree-sitter 抽出**全仓库的类/函数签名**，再用**图排序算法**（PageRank 系）按"与当前聊天文件的相关性"选进 token 预算，压缩成：
  ```
  aider/coders/base_coder.py:
  ⋮...
  │class Coder:
  │    abs_fnames = None
  ⋮...
  │    def run(self, with_message=None):
  ⋮...
  ```
  省略号是"有行被跳过"的标记。这解决两件事：① 模型知道别处有哪些 API ② 模型知道该把哪些文件加进聊天。
- **显式聊天文件集**：用户 `/add` 的文件才有完整内容；其余只见 repo map。这是**"检索预算"而非"上下文压缩"**的思路 —— 与其他产品的"窗口满了再压"相反，是**前置选料**。
- **Prompt caching**：有专门文档 `/docs/usage/caching.html`。
- 工具输出落盘：aider 也没有"工具输出"概念（唯一的外部输出是 lint/test 结果）。

#### 工具系统

- 几乎没有"工具"：核心是**编辑格式解析器**（每种格式一个 coder 类）。
- **Lint & Test 自动回环**：每次改完自动跑 linter/test，有问题就让模型修。
- **Git 集成是一等公民**：每次改动 **auto-commit**（带合理 commit message），`/undo` 靠 git。
- **并发：无**（单文件编辑流水线）。
- 权限：`--yes` / `--dry-run` 等，无细粒度门禁（本地 pair programming 定位）。

#### 会话存储

- 【事实】**基于 Git 的隐式会话**：每次改动 = 一个 commit，`/undo` = git revert。**没有独立的会话文件**。
- 有 `/history` 类命令（内存里的 chat history）+ `.aider.chat.history.md` 落盘。
- fork/rewind：**靠 git 分支**（用户自己做）。

#### 扩展机制

- 【事实】配置文件（`.aider.conf.yml`）、`CONVENTIONS.md`、`--read` 只读文件、repomap 语言支持（tree-sitter-language-pack，100+ 语言）、watch 模式（IDE 内用）、/commands 脚本化、Python API。
- **无 MCP**（README 未见；生态里有第三方 bridge）。

#### 子代理

- 不支持。

#### 记忆系统

- **CONVENTIONS.md** + 显式聊天文件 = 用户手工维护的"记忆"。**无自动跨会话记忆**。

#### 评测

- 【事实】自建基准：**Code Editing Leaderboard** / **Refactoring Leaderboard**（<https://aider.chat/docs/leaderboards/>）。
- 【事实】architect 模式曾达 **85%**（o1-preview architect + deepseek/o1-mini editor, whole 格式），基线 79.7%。
- 【事实】README 徽章：**88% 的新代码由 aider 自己写**。
- 不在 Terminal-Bench / SWE-bench 主榜 → 不直接可比。

### 关键设计亮点

- **Repo map + 图排序**：把"上下文管理"前置成"上下文选择"，用 token 预算做检索而非事后压缩。这是本调研里**唯一**这么做的。
- **Architect/Editor 分离**：同一问题两次推理，一次专注"怎么办"，一次专注"怎么写"。对格式敏感的编辑场景非常有效。
- **Git 即会话**：撤销/审计/分支全部复用 git 语义，零自研存储。

### 来源

- <https://github.com/Aider-AI/aider>（README）
- <https://aider.chat/docs/repomap.html>
- <https://aider.chat/docs/usage/modes.html>
- <https://aider.chat/2024/09/26/architect.html>（architect 论文式博客 + 完整基准表）
- <https://aider.chat/docs/leaderboards/>

---

## D. Cline / Roo Code

### D1. Cline

#### 基本信息

- 组织：Cline（原 `saoudrizwan/claude-dev`）
- 开源：**Apache-2.0**，<https://github.com/cline/cline>
- 语言：**TypeScript**（Node.js）
- 形态：VS Code 扩展 / JetBrains 插件 / **CLI**（`npm i -g cline`）/ **Desktop App**（Tauri + Bun sidecar + Next.js）/ **SDK**（`@cline/sdk`）
- 文档：<https://docs.cline.bot>

#### 架构

- **单一 agent core，多端宿主**【事实】：README 索引表明确 `sdk/` 是"Node.js programmatic agent API and extension exports"，CLI / VS Code / Desktop / JetBrains 都"talks to the shared agent core"。**JetBrains 插件未开源**。
- **Plan / Act 双模式**【事实】：Plan 模式探索代码库、问澄清问题、出策略；Act 模式执行。**这是"用户显式切段"的两阶段，不是内部的 planner 子代理**。
- 工具（产品层）：文件编辑（diff 视图可 review/修改/revert）、**终端命令执行**（实时盯输出，长进程转后台并继续响应新输出）、浏览器（README 提到 "browse the web"）、MCP。
- **Checkpoints**【事实】："All changes are tracked with checkpoints, so you can easily undo the agent's work" —— 工作区快照式回滚。
- **Provider 无关**：支持各家模型（Cline 的定位之一）。
- 并发模型 / 上下文压缩细节：**未在 README 中找到公开说明**（README 是产品介绍，非架构文档）。
- 子代理：README 提到 SDK 支持 "multi-agent teams"；具体隔离方式 **未找到公开资料**。

#### 来源

- <https://github.com/cline/cline>（README，含多端索引表）
- <https://docs.cline.bot>、<https://docs.cline.bot/cline-sdk/overview>

### D2. Roo Code

#### 基本信息

- 组织：Roo Code（`RooVeterinaryInc.roo-cline`），**从 Cline 分叉而来**
- 开源：**Apache-2.0**，<https://github.com/RooCodeInc/Roo-Code>
- 语言：TypeScript，VS Code 扩展
- ⚠️ **【事实】"The Roo Code Extension was shut down on May 15th."**（README 的 Disclaimer 原文）→ **项目已停止**；社区分叉为 [ZooCode](https://github.com/Zoo-Code-Org/Zoo-Code/)。

#### 架构

- **Modes**（核心设计）【事实】：`Code` / `Architect` / `Ask` / `Debug` + **自定义 Mode**。每个 mode = 一套 **角色 + 工具权限 + 提示词**。
  → 与 Cline 的 Plan/Act 相比，**Mode 是更通用的"人格 + 工具白名单"抽象**（与 Kimi Code 的 custom agent、dS 的 agent preset 同构）。
- 工具/MCP：支持 MCP Servers。
- 上下文 / 存储 / 子代理：**未在 README 公开**（README 以产品介绍为主）。

#### 来源

- <https://github.com/RooCodeInc/Roo-Code>（README，含 2026-05-15 停止公告）
- <https://roocodeinc.github.io/Roo-Code/>

---

# 横向对比（只覆盖四个必选维度）

## 1. 主循环

| 产品 | 形态 | 自动续写 | 备注 |
|---|---|---|---|
| Codex CLI | 状态机 + Tokio 任务（事件驱动） | 有（turn-end 压缩后继续、`auto_compact`） | subagent 是工具 → 事实上的两层 |
| Claude Code | `while(true)` async generator，**单线程** | **强**：9 个自动续跑条件，含 **Stop hook 否决停止** | 退出原因是类型化的 `Terminal` |
| Kimi Code CLI | 单层 loop，参数化 | 有（`max_steps_per_turn` 可无限） | 后台 task 自动回灌通知 |
| Trae（产品） | **纯 ReAct，明确删掉前置 Plan 阶段** | 未知 | 官方称按需动态出计划 |
| dsh | **事件溯源 turn/step 循环，loop 可插拔** | 有，且**默认无 turn 预算** | waterfall 中间件 + serial turn-stopping |
| WorkBuddy | 未知（云端 ACP+SSE 流式） | 未知 | 云端沙箱 + 会话休眠 |
| OpenHands V1 | stateless Agent + 单可变 ConversationState | 无公开说明 | 复杂度在事件流 |
| SWE-agent | 单层 `forward()`，单 shell | 无（直到 submit） | YAML 驱动 |
| Aider | **非工具循环**，编辑格式解析 + lint/test 回环 | 无 | architect = 两次推理 |
| Cline | agent core + Plan/Act 显式分段 | 未知 | — |

## 2. 上下文压缩

| 产品 | 策略 | 阈值 | 工具输出落盘 |
|---|---|---|---|
| Codex | 远程 compact 端点 + **后台推测式压缩** | `ContextWindowTokenStatus` 多档（scope/full/turn-end） | **否**，但磁盘 rollout 保留全量、上下文内中段截断（`TruncationPolicy`） |
| Claude Code | **4 级流水线** + 413 反应式 | `contextWindow − reservedForSummary − 13,000` | **是**：超 `maxResultSizeChars`（默认 50k）落盘 + 2KB 预览 |
| Kimi Code | LLM 摘要 | `reserved_context_size`（默认 50000，剩余低于即触发） | **是**：`TaskOutput` 内联 ≤32KB + `output_path`；`Glob`/`Grep` 大页落盘 |
| Trae | 近期逐字 + 旧轮 LLM 摘要（**不占用户配额**） | 未公开 | 未公开 |
| **dsh** | **seam 可换**；默认 `min(W×0.8, W−O−64K)` + 保留 16% + **先剪工具输出（不调模型）** | **公式公开** | **是**：`spill-policy` + `maxInlineTokens`，头尾保留 + 中间省略通知 |
| WorkBuddy | 未公开（有 RAG + Memory 开关） | 未公开 | 未公开 |
| OpenHands | `LLMSummarizingCondenser`，**按事件条数**触发 + `keep_first` | `max_context_length`（事件数） | 未公开 |
| SWE-agent | `HistoryProcessor`（可配置） | 配置决定 | 否 |
| Aider | **前置选料**：repo map 图排序进 token 预算 | repo map token 预算 | 否 |

**prefix cache 保护做得最显式的是两个**：Claude Code（`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` + 压缩子代理刻意继承工具集）与 dsh（system prompt 走 `system/message` 历史，"能追加不替换"，并逐条列出破坏点）。Codex 走 `reference_context_item` diff 增量。其余产品未见公开说明。

## 3. 工具并发

| 产品 | 模型 | 关键参数 |
|---|---|---|
| **Codex** | **读并写斥**：`Arc<RwLock<()>>`，`supports_parallel_tool_calls()` 决定拿 read 锁还是 write 锁 | — |
| Claude Code | **全串行**（刻意） | — |
| Kimi Code | 工具批量 + **swarm 无上限爬坡**（5 起，每 700ms +1，可封顶，≤128） | `task.max_running_tasks`、`KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY` |
| dsh | **执行器可插拔**；parallel-safe 重叠、exclusive 成屏障；**结果按模型顺序** | `maxParallelToolCalls`（默认 10，`1`=串行） |
| OpenHands | 默认 **1（串行）**，可调；标注 experimental | `tool_concurrency_limit` |
| Trae Agent（开源） | ToolExecutor 支持 parallel & sequential 两种 | 未公开细节 |
| Aider / SWE-agent | 无并发 | — |

**四种范式**：读并写斥（Codex）/ 全串行（Claude Code、SWE-agent、Aider）/ 并发上限 + 工具自声明（dsh、OpenHands）/ 子代理爬坡（Kimi）。

## 4. 会话存储

| 产品 | 结构 | 格式 | fork / rewind |
|---|---|---|---|
| Codex | 线性 + checkpoint/rollback | **JSONL（zstd 分帧）+ SQLite 状态库** | resume / fork（ThreadId）/ rollback N 轮 |
| Claude Code | 线性 + 墓碑 | **JSONL，实时逐事件追加** `~/.claude/projects/` | `--resume` / `--fork-session`（复制）/ 墓碑保证压缩后一致 |
| Kimi Code | **每 agent 一条线，整体是树** | `sessions/<workDirKey>/<sid>/agents/<agentId>/wire.jsonl` + `state.json` + `session_index.jsonl` | `/fork`（复制，停留在原会话）/ `--continue` / `--session` / export ZIP |
| Trae | 未公开 | 未公开（开源版有 trajectory recording） | 未公开 |
| **dsh** | **线性事件日志（seq 连续、永不重写）+ turn 边界 fork** | **JSONL 逻辑日志 / zstd 校验帧物理层，版本化代际 `session.vN.jsonl`** | fork（`create({seed, inheritedEventCount})`）/ resume / 崩溃修复（补 `turn/end{interrupted}`） |
| WorkBuddy | **Session + Checkpoint/Version 双轨** | 未公开（本地疑似 SQLite `workbuddy.db`，二手） | Checkpoint 回滚 / 版本发布 |
| OpenHands | 事件流 + ConversationState | 未公开（API 层） | deterministic replay |
| SWE-agent | 线性 trajectory | `.traj` JSON | 无 |
| Aider | **无独立会话文件** | Git commits | `/undo` = git |

---

# 资料缺口清单（明确"未找到公开资料"）

| 产品 | 缺口 |
|---|---|
| Codex | ① Codex CLI 在 SWE-bench Verified 上的官方单独成绩 ② `unrolling-the-codex-agent-loop` 博客正文（JS 渲染，抓取仅得标题） |
| Claude Code | ① 官方架构文档（只有 `how-claude-code-works` 用户级） ② 一切实现细节均为**社区逆向**（泄漏源码），非官方确认 |
| Kimi Code CLI | ① 普通工具（非 agent）是否并行 ② prefix cache 策略 ③ **自身 harness 的公开 benchmark** |
| **Kimi Work** | **内部架构（主循环/上下文/工具/存储）完全未公开** |
| Trae | ① 产品侧（IDE/SOLO）会话存储格式、fork ② 产品侧子代理 ③ 产品侧权限门禁细节 ④ Terminal-Bench 成绩 |
| dsh | ① 跨会话长期记忆 ② 官方 benchmark ③ SQLite persistence 后端（仅第三方提及） |
| WorkBuddy | ① **主循环形态** ② 上下文压缩策略 ③ 工具并发模型 ④ 输出截断阈值 ⑤ 会话文件格式 ⑥ 跨会话记忆的提炼机制 |
| OpenHands | ① 具体会话存储格式 ② 输出截断 ③ 子代理上下文隔离方式 ④ 最新 benchmark |
| Aider | ① 是否支持 MCP（README 未见） ② 并发（无） |
| Cline | ① 上下文压缩 ② 并发模型 ③ 子代理隔离 |
| Roo Code | **项目已停止（2026-05-15）**，仅 README 层面信息 |

---

# 对 `my-agent-harness`（Python）的对照提示

以下只是**从上面证据直接推得的对照点**，不含建议：

1. **主循环**：Python 同步循环的天然对手不是"要不要状态机"，而是**"自动续写条件"是否被显式枚举**。Claude Code 的 9 个条件、dsh 的 "tools owe another request"、Kimi 的 `max_steps_per_turn`，三者都可直接抄成 Python 里的一个 `while` + `reason: Continue` 枚举。
2. **上下文**：四种可辩护路线并存 —— 事后压缩（Codex/Claude/dsh/Kimi）、前置选料（Aider）、按事件数触发（OpenHands）、不压缩只落盘（部分产品）。**dsh 的公式最可审计**（`min(W×0.8, W−O−headroom)` + 保留比），Claude Code 的"零成本优先"顺序最可省钱。
3. **工具并发**：Python 里 GIL 不是阻碍（I/O 密集），真正的问题和 Codex 一样是**"哪些工具能并行"如何声明**。Codex 的 `supports_parallel_tool_calls()` + RwLock 是最小改动量的方案；dsh 的 `isConcurrencySafe(args)` + 结果保序更适合"参数相关"的场景。
4. **会话存储**：**JSONL 是事实标准**（Claude Code / Kimi / Codex rollout / dsh / SWE-agent 都是）；差异在**元数据层**（Codex→SQLite、Kimi→`state.json`+index、dsh→事件溯源投影）。"事件即真相、消息是投影"（dsh）比"存消息"更能支持 fork/replay，但改造成本最高。
5. **子代理**：Kimi 的"**默认委派深度硬上限**"、dsh 的 `delivery: parent|caller`、Claude Code 的"Task 是工具所以父循环不区分等待对象"——这三条是不同层次的工程解法。
6. **记忆**：只有 Codex 给出了**完整可抄的两阶段流水线**（租约 claim + 脱敏 + git 基线 + 内部 consolidation agent）。其他产品要么没有，要么不公开。

---

*文档结束。所有结论均标注了「事实 / 社区逆向 / 推测」与来源 URL；**未找到公开资料**的项已明确标出，未做推测性填充。*
