# 逐模块实验设计（12 个模块 × 每个专属实验）

> **问题**：现有文档的「最终效果」多为「测试通过」或泛指，
> 不足以回答「这个设计到底带来了什么差异」。
>
> **本文档**：为 12 个模块各设计实验，要求满足：
> 1. **可对比**（有对照组/开关）
> 2. **可量化**（有数字，不是"更快"）
> 3. **可复现**（命令能跑，零外部依赖优先）
>
> 状态：设计阶段 → 逐个实现 → 填入实测数字

---

## 实验设计原则

| 原则 | 说明 |
| --- | --- |
| **必须有一个开关** | 打开/关闭某设计，对比差异 |
| **zero-cost 优先** | 不调真实 API，用假 provider / 本地执行 |
| **单一变量** | 每次只改一个设计点 |
| **记录基线** | 对照组必须是"如果不这么设计会怎样" |

---

## 模块 1：ReAct 微内核（`loop.py`）

### 实验 1-A：纯函数 vs 类封装 —— 单测速度

**假设**：纯函数生成器无需装配整个 Agent，单测应快一个数量级。

**方法**：

| 组 | 做法 |
| --- | --- |
| 对照组 | 构造完整 `Agent`（Session 树 + MemoryStore + Skills + Plugins），再跑循环 |
| 实验组 | 直接 `run_agent_loop(llm=FakeLLM(), messages=[])` |

**指标**：
- 单次测试准备耗时（构造对象的时间）
- 单次测试执行耗时
- 内存占用

**预期**：作者博客称「单测快 100 倍」，实测验证。

**命令**：`python -m experiments.per_module.exp_loop_purity`

---

### 实验 1-B：自动续写（截断自愈）的价值

**假设**：模型输出被 `max_tokens` 截断时，自动续写能挽回一次完整工具调用。

**方法**：

| 组 | 做法 |
| --- | --- |
| 对照组 | `MAX_LENGTH_CONTINUATIONS = 0`（直接结束） |
| 实验组 | `MAX_LENGTH_CONTINUATIONS = 3`（默认） |

**注入**：假 LLM 第一次返回 `finish_reason=length` 且无 tool_calls，
第二次返回完整 tool_calls。

**指标**：任务完成率（能否拿到工具调用）

**预期**：对照组 0%，实验组 100%

**命令**：`python -m experiments.per_module.exp_loop_continuation`

---

### 实验 1-C：Steering / Follow-up 两级干预的时序正确性

**假设**：steering 在内层生效、follow-up 在外层生效，语义不同。

**方法**：构造 4 个场景，检查干预消息注入的**位置**和**时机**。

| 场景 | 预期 |
| --- | --- |
| 内层工具执行后注入 steering | 消息追加在 tool 结果之后 |
| 内层未结束时注入 follow-up | **不生效**（要等内层结束） |
| 内层结束后注入 follow-up | 外层继续，新轮次开始 |
| 两者同时注入 | steering 先，follow-up 后 |

**指标**：事件序列是否符合预期（逐事件断言）

**命令**：`python -m experiments.per_module.exp_loop_intervention`

---

## 模块 2：工具执行与并发（`registry.py`）

### 实验 2-A：批级门控 vs 全并发 vs 全串行

**假设**：批级门控在保证正确性的同时，只读批次能获得并发收益。

**方法**：三种策略 × 三种批次组成

| 批次组成 | 全串行 | 全并发 | 批级门控 |
| --- | --- | --- | --- |
| 5 个 read（全只读） | 慢 | 快 | **快** |
| 1 write + 4 read | 正确 | **竞态** | 正确 |
| 5 个 write | 正确 | **竞态** | 正确 |

**指标**：
- 耗时（用 sleep 模拟工具延迟）
- **正确性**（写后读是否读到正确版本）

**关键设计**：让每个 `write` 写入递增版本号，`read` 读取并记录，
最后检查读取序列是否单调——非单调即为竞态。

**命令**：`python -m experiments.per_module.exp_registry_concurrency`

---

### 实验 2-B：`is_parallel_safe` 默认值的失败安全价值

**假设**：默认 `False` 能防止「忘记声明」导致的数据竞争。

**方法**：

| 组 | 默认值 | 新增一个未声明的新工具 |
| --- | --- | --- |
| 对照组 | `True`（fail-open） | 会并发执行 → 可能竞态 |
| 实验组 | `False`（fail-safe） | 强制串行 → 安全 |

**指标**：模拟"开发者忘记标记"的场景下，是否产生竞态

**命令**：`python -m experiments.per_module.exp_registry_failsafe`

---

### 实验 2-C：流式进度回传的延迟改善

**假设**：`ToolExecutionUpdate` 让长命令的首次反馈延迟大幅降低。

**方法**：模拟一个产出 10 行的命令。

| 组 | 做法 | 首字节时间 |
| --- | --- | --- |
| 对照组 | `communicate()` 全缓冲 | 等全部完成 |
| 实验组 | 逐行 `on_update` | 第一行完成即可见 |

**指标**：首次反馈延迟 vs 总耗时

**命令**：`python -m experiments.per_module.exp_registry_streaming`

---

## 模块 3：上下文压缩（`context.py`）

### 实验 3-A：三层压缩对 prefix cache 的影响（核心实验）

**假设**：Append-Only + 免费层的设计显著减少 prefix 变更次数。

**方法**：模拟 50 轮对话，每轮追加消息，观察「发送视图」的 prefix 变化。

| 组 | 策略 | 
| --- | --- |
| 对照组 A | 滑动窗口（保留最近 N 条） |
| 对照组 B | 每轮重新截断工具输出 |
| 对照组 C | 只在超阈值时全量摘要（pi 方案） |
| **实验组** | **三层管线（本项目）** |

**指标**：
- **prefix 变更次数**（最关键——每次变更 = cache 全失效）
- 平均 prefix 长度
- 总 token 消耗

**预期**：本项目的 prefix 变更次数应显著低于 A、B，接近 C。

**命令**：`python -m experiments.per_module.exp_context_prefix`

---

### 实验 3-B：免费层（工具输出落盘）的成本收益

**假设**：落盘层零 LLM 调用，能替代相当比例的摘要调用。

**方法**：

| 组 | 做法 |
| --- | --- |
| 对照组 | 只有摘要层（超阈值直接摘要） |
| 实验组 | 免费层 → 判断是否还超 → 才摘要 |

**指标**：
- 摘要调用次数（成本）
- 最终 token 数（效果）
- 信息保留度（关键文件路径是否仍在上下文里）

**命令**：`python -m experiments.per_module.exp_context_freelayer`

---

### 实验 3-C：边界定位的容错（`_locate_boundary`）

**假设**：用消息 key 定位比裸下标更健壮。

**方法**：注入历史篡改

| 场景 | 裸下标 | key 定位 |
| --- | --- | --- |
| 删除中间一条消息 | 切点错位 | 安全重建 |
| 在中间插入消息 | 切点错位 | 安全重建 |
| 历史被 fork 改写 | 切点错位 | 安全重建 |

**指标**：压缩后上下文是否仍然合法（工具调用配对完整）

**命令**：`python -m experiments.per_module.exp_context_boundary`

---

## 模块 4：会话树（`session/`）

### 实验 4-A：Fork 成本对比（树状 vs 线性）

**假设**：树状结构的 fork 是 O(1)，线性结构的 fork 是 O(n)。

**方法**：100 / 1000 / 10000 条消息的会话，各 fork 一次。

| 结构 | 操作 |
| --- | --- |
| 线性 JSON 数组 | 复制整个数组 |
| **树状 Entry** | 新增一条 Entry + parentId |

**指标**：fork 耗时、新增的磁盘写入字节数

**命令**：`python -m experiments.per_module.exp_session_fork`

---

### 实验 4-B：增量追加 vs 全量重写（P1-2 缺陷的量化）

**假设**：增量追加在长会话下写入量低一个数量级。

**方法**：追加 100 条消息。

| 组 | 做法 | 总写入字节 |
| --- | --- | --- |
| 对照组 | 每条消息全量重写文件 | O(n²) |
| 实验组 | JSONL 追加一行 | O(n) |

**指标**：总写入字节数、总耗时

**预期**：1000 条消息时，对照组写入量是实验组的 **500 倍**（n/2）。

**命令**：`python -m experiments.per_module.exp_session_append`

---

### 实验 4-C：崩溃一致性（截断的 JSONL 能否恢复）

**假设**：JSONL 追加格式在最坏情况下只丢最后一行。

**方法**：写入 10 条后，人为截断文件中间（模拟崩溃）。

| 格式 | 结果 |
| --- | --- |
| 单个 JSON 数组 | **整个文件损坏，无法解析** |
| **JSONL** | **前 9 条完好，只丢 1 条** |

**指标**：可恢复的消息数

**命令**：`python -m experiments.per_module.exp_session_crash`

---

## 模块 5：长期记忆（`memory.py` + `memory_index.py`）

### 实验 5-A：双层时间 vs 覆盖更新（冲突处理）

**假设**：双层时间能回答"之前是什么"，覆盖更新不能。

**方法**：模拟偏好变化

```
Day 1: 记 "Python 版本 = 3.11"
Day 5: 记 "Python 版本 = 3.12"
Day 10: 查询 "Python 版本" / 查询 "之前的 Python 版本"
```

| 策略 | 当前值查询 | 历史值查询 |
| --- | --- | --- |
| 覆盖更新 | ✅ 3.12 | ❌ **查不到** |
| **双层时间** | ✅ 3.12 | ✅ **3.11** |

**指标**：两个查询的正确率

**命令**：`python -m experiments.per_module.exp_memory_bitemporal`

---

### 实验 5-B：BM25 vs 纯 FTS5 的检索质量

**假设**：双引擎在 FTS5 不可用时仍能工作。

**方法**：准备 50 条记忆，20 个查询。

| 引擎 | 可用性 | Recall@5 |
| --- | --- | --- |
| 仅 FTS5 | 不可用时崩溃 | — |
| **BM25 回退** | 始终可用 | 需测 |

**指标**：Recall@5、平均耗时

**命令**：`python -m experiments.per_module.exp_memory_bm25`

---

### 实验 5-C：记忆对系统 prompt 的 token 占用

**假设**：记忆注入可控，不会撑爆 system prompt。

**方法**：0 / 10 / 50 / 100 条记忆，测量 system prompt token 数。

**指标**：token 占用曲线 + 是否触及 budget 上限

**命令**：`python -m experiments.per_module.exp_memory_budget`

---

## 模块 6：Extension 与钩子（`extensions/` + `hooks.py`）

### 实验 6-A：钩子能否真正阻断（P0-1 的延伸）

**假设**：`HookResult.block=True` 能阻止工具执行。

**方法**：对 5 个工具调用注入 blocker hook。

| 组 | 结果 |
| --- | --- |
| 无 hook | 5 个都执行 |
| 有 block hook | **0 个执行**，且返回 `blocked` 错误 |

**指标**：实际执行数、错误消息正确性

**命令**：`python -m experiments.per_module.exp_hooks_block`

---

### 实验 6-B：钩子改写入参的效果

**假设**：`updated_args` 能改变工具行为。

**方法**：hook 把 `write(path="a.txt")` 改成 `write(path="sandbox/a.txt")`。

**指标**：文件实际落盘位置

**命令**：`python -m experiments.per_module.exp_hooks_rewrite`

---

### 实验 6-C：钩子异常是否影响主循环（Never-Throw）

**假设**：钩子抛异常不应导致主循环崩溃。

**方法**：让 hook 主动 `raise`。

**指标**：主循环是否继续、错误是否被转为工具结果

**命令**：`python -m experiments.per_module.exp_hooks_neverthrow`

---

## 模块 7：子代理（`subagents.py`）

### 实验 7-A：子代理的 token 收益 vs 成本（**学术空白**）

**假设**：子代理隔离上下文，但未必省 token。

**方法**：同一任务，两种做法

| 组 | 做法 |
| --- | --- |
| 对照组 | 主代理直接做（上下文持续累积） |
| 实验组 | 派子代理做，只把结果返回主代理 |

**指标**：
- 主代理的最终上下文 token 数
- 总 token 消耗（含子代理）
- 任务成功率

**注**：这是学术调研发现的**空白点**——"同等 token 预算下子代理 vs 单 agent 的收益曲线"至今没人做过。

**命令**：`python -m experiments.per_module.exp_subagent_cost`

---

### 实验 7-B：子代理上下文隔离的有效性

**假设**：子代理看不到主代理的历史，避免上下文污染。

**方法**：主代理历史里放一条"错误结论"，看子代理是否会继承这个错误。

**指标**：子代理是否复述了错误结论

**命令**：`python -m experiments.per_module.exp_subagent_isolation`

---

## 模块 8：转录本自愈（`tool_history.py`）

### 实验 8-A：三阶段 vs 单阶段修复（已有 runtime_ab 的细化）

**现状**：已有 40 例 A/B（20% → 100%），但没测**三阶段各自的贡献**。

**方法**：分别关闭 Phase 1 / 2 / 3。

| 配置 | 会话可继续率 |
| --- | --- |
| 全关 | 20% |
| 只开 Phase 2+3 | ? |
| 只开 Phase 1+3 | ? |
| 全开 | 100% |

**指标**：每个阶段的边际贡献

**命令**：扩展 `experiments/runtime_ab/run_ab.py` 加 `--disable-phase` 参数

---

### 实验 8-B：Phase 1（预留配对）为什么必须在前

**假设**：先贪心匹配会拆散本来正确的配对。

**方法**：构造一个"正确配对 + 干扰调用"的场景，对比两种顺序。

**指标**：修复后有多少配对被打乱

**命令**：`python -m experiments.per_module.exp_toolhistory_order`

---

## 模块 9：权限门禁（`permissions.py`）

### 实验 9-A：fail-closed vs fail-open（P0-1 的量化）

**假设**：fail-open 在"忘记传回调"时静默放行。

**方法**：

| 组 | 默认行为 | 不传 confirm_callback 时的结果 |
| --- | --- | --- |
| fail-open | 放行 | **文件被写（危险）** |
| **fail-closed** | block | **文件不存在（安全）** |

**指标**：危险操作的实际执行率

**命令**：`python -m experiments.per_module.exp_permissions_failclosed`

---

### 实验 9-B：路径越界检测的覆盖率

**假设**：15 个越界用例全部被拦截。

**方法**：构造 15 种越界路径（`../`、绝对路径、符号链接、Windows 驱动器等）。

**指标**：拦截率

**命令**：`python -m experiments.per_module.exp_permissions_paths`

---

### 实验 9-C：组合风险的漏检（检验 SCR-Bench 的批评）

**假设**：我们的工具级门禁**无法**检测组合风险。

**方法**：构造 `read(".env")` + `write("public/x.txt", <内容>)` 的组合。

| 检查粒度 | 能否拦截 |
| --- | --- |
| 我们的工具级 | ❌ **不能**（read 和 write 都合法） |
| 路径级/数据流级 | ✅ 能 |

**指标**：漏检率

**注**：这是**主动暴露缺陷的实验**，用于支撑 `03_ACADEMIC.md` 里的 SCR-Bench 批评。

**命令**：`python -m experiments.per_module.exp_permissions_composition`

---

## 模块 10：任务系统（`task_store.py`）

### 实验 10-A：成环死锁检测的有效性

**假设**：BFS 可达性检测能阻止所有环。

**方法**：构造 3 / 5 / 10 节点环，尝试创建闭环依赖。

**指标**：拦截率

**命令**：`python -m experiments.per_module.exp_task_cycle`

---

### 实验 10-B：TaskGuardHook 的提前收工拦截

**假设**：模型"以为做完了"时，守卫能提醒。

**方法**：模拟 TurnEnd 事件 + 存在 in_progress 任务。

**指标**：是否触发 steer、提醒次数（应只提醒一次）

**命令**：`python -m experiments.per_module.exp_task_guard`

---

## 模块 11：MCP 客户端

### 实验 11-A：连接池复用 vs 每次重连

**假设**：连接池减少握手开销。

**方法**：调用 20 次工具。

| 组 | 做法 |
| --- | --- |
| 对照组 | 每次重新连接 |
| 实验组 | 复用连接 |

**指标**：总耗时、握手次数

**命令**：`python -m experiments.per_module.exp_mcp_pool`

---

## 模块 12：Plugin（`plugins.py`）

### 实验 12-A：插件资源发现完整性

**假设**：能从目录约定正确发现 skills / agents / MCP 配置。

**方法**：构造标准插件目录，检查发现结果。

**指标**：发现率（3 类资源是否都找到）

**命令**：`python -m experiments.per_module.exp_plugin_discovery`

---

# 执行计划

## 阶段划分

| 阶段 | 模块 | 实验数 | 优先级 |
| --- | --- | --- | --- |
| **P0** | 1, 2, 3, 4 | 12 | **核心主张**（微内核、并发、压缩、会话） |
| **P1** | 5, 8, 9 | 8 | 记忆、自愈、安全 |
| **P2** | 6, 7, 10, 11, 12 | 8 | 其余 |

**合计 28 个实验。**

## 交付物

| 文件 | 内容 |
| --- | --- |
| `experiments/per_module/` | 28 个实验脚本 |
| `experiments/per_module/results.json` | 全部实测数据 |
| `docs/08_EXPERIMENTS.md` | 实验报告（每个实验的方法、结果、结论） |

## 验收标准

每个实验必须：
1. 有明确的对照组
2. 输出至少一个数字
3. 结果写入 `results.json`
4. 结论能在 `04_MODULES.md` 对应模块的「最终效果」里引用

---

## 当前状态

| 项 | 状态 |
| --- | --- |
| 实验设计 | ✅ 完成（28 个） |
| 实验实现 | ⏳ 待开始 |
| 实测数据 | ⏳ 待填充 |
