# 工业界 Agent Harness 横向对比

> 目的：为每个设计选择提供**工业界的横向依据**——别人怎么做、我们为什么不同。
>
> 原始调研材料（10 个产品，1213 行）：[`raw/landscape_raw.md`](raw/landscape_raw.md)
>
> 配套：[`01_PI_DIFF.md`](01_PI_DIFF.md)（pi 量化对照）、[`03_ACADEMIC.md`](03_ACADEMIC.md)（学术对标）
>
> ⚠️ **资料可信度分级**：
> - 【官方】= 官方文档或开源源码
> - 【社区逆向】= 社区逆向分析（如 Claude Code 的 source map 泄漏）
> - 【未找到】= 无公开资料
>
> 2026-11-07 调研

---

## 1. 总览：10 个产品的架构坐标

| 产品 | 厂商 | 开源 | 语言 | 主循环 | 上下文压缩 | 工具并发 | 会话存储 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Codex CLI** | OpenAI | ✅ Apache-2.0 | Rust | 状态机 + Tokio | 阈值 + **后台推测式压缩** | **读并写斥**（RwLock） | JSONL(zstd) + SQLite |
| **Claude Code** | Anthropic | ❌ 闭源 | TS | 单层 `while(true)`，**单线程** | **4 级流水线** + 413 反应式 | **全串行**（刻意） | JSONL + 墓碑 |
| **Kimi Code CLI** | 月之暗面 | ✅ MIT | TS | 单层 + 后台 task 树 | 剩余窗口阈值触发 | 批量 + **并行子代理 swarm** | 每 agent 一条 `wire.jsonl` |
| **Trae 2.0** | 字节 | 产品闭源 / Agent 开源 | — | **去掉 Plan 阶段，纯 ReAct** | 近期逐字 + 旧轮摘要 | 未公开 | 未公开 |
| **DeepSeek Harness** | DeepSeek | ✅ MIT | TS | **事件溯源，loop 本身是插件** | 可插拔 seam + 0.8 阈值 + 16% 保留 | **工具自声明 + 默认 10** | 追加式事件日志 |
| **WorkBuddy** | **腾讯** | ❌ 闭源商业 | — | 云端沙箱混合 | 未公开 | 多 Agent 并行 | 云端 Session 模型 |
| OpenHands V1 | 开源 | ✅ | Python | stateless Agent | 按事件条数触发 | **默认 1（串行）** | 唯一可变 `ConversationState` |
| SWE-agent | 普林斯顿 | ✅ | Python | 单层 + YAML 驱动 | **HistoryProcessor（最早可替换组件）** | 串行 | 无 |
| Aider | 开源 | ✅ | Python | **编辑指令式**（非工具调用） | **前置选料（repo map）** | 不适用 | **Git 即会话** |
| Cline / Roo | 开源 | ✅ | TS | Plan/Act 双模式 | — | — | checkpoints |

---

## 2. 四个核心维度深度对比

### 2.1 维度一：主循环

| 产品 | 形态 | 关键设计 |
| --- | --- | --- |
| **Codex** | 状态机 + 异步任务 | `core/src/tasks/` 分 RegularTask / CompactTask / ReviewTask |
| **Claude Code** | 单层 `while(true)` async generator | **单线程是刻意选择**；513K 行里 loop 不到 1% |
| **Kimi CLI** | 单层 loop，参数化完整 | `max_steps_per_turn`（可无限）、`max_attempts_per_step=10` |
| **Trae 2.0** | **纯 ReAct（去掉了 Plan 阶段）** | 官方理由：刚性前期规划把模型带错路 |
| **dsh** | 事件溯源 turn/step | **agent loop 本身也是插件**（无特权核心） |
| **本项目** | **纯函数异步生成器** | `async def run_agent_loop() -> AsyncIterator[Event]` |

**一个有意思的对照**：Trae 2.0 明确**删掉了**固定 Plan 阶段，
理由与我们保留 `Task` 队列形成对比——

| | Trae | 本项目 |
| --- | --- | --- |
| 前期规划 | ❌ 删除 | ✅ 保留（外层 Task 队列） |
| 理由 | 刚性计划带错路 + 计划与工具调用脱节 | 长程任务需要跨轮次的组织 |

**这不是谁对谁错**：Trae 面向 IDE 内的单次改动（短程），我们保留 Task 队列是为长程。

**Claude Code 的「513K 行里 loop 不到 1%」是一个重要提示**：
循环本身很简单，**难点全在让循环活下来的工程**（压缩、存储、错误恢复）。

### 2.2 维度二：上下文压缩——拉开工程水平的分水岭

调研的核心结论：

> **「前缀缓存」是真正拉开工程水平的分水岭**。
> 只有 **Claude Code** 和 **dsh** 把它写成了显式设计目标；
> **其余产品完全无公开说明**。

#### 各产品的压缩策略

| 产品 | 层数 | 机制 | prefix cache 处理 |
| --- | --- | --- | --- |
| **Claude Code** | **4 级** | 工具结果落盘（≤50k 字符）→ 无模型裁剪 → 微压缩 → 全量摘要 | ✅ **显式设计**：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 切段；**压缩子代理刻意继承完整工具集**以保 cache 命中；微压缩后**显式失效缓存** |
| **Codex** | 后台推测 | 阈值触发 + 远程 compact 端点 + **后台推测式压缩**（checkpoint 上预跑，命中即用、未命中也只浪费一次调用） | ⚠️ 用 `reference_context_item` diff 增量 |
| **dsh** | 可插拔 seam | 默认 `trigger = floor(min(W×0.8, W−O−64K))` + 保留最近 16% 逐字 + **压缩前先剪工具输出（不调模型）** | ✅ system prompt 走历史，"能追加就不替换"，**并逐条列出破坏点** |
| **Kimi CLI** | 阈值 | `reserved_context_size=50000` 触发 | ❌ 无公开说明 |
| **Trae** | 摘要 | 近期逐字保留 + 旧轮摘要（**不消耗用户配额**） | ❌ 无公开说明 |
| **OpenHands** | 条数触发 | `LLMSummarizingCondenser` **按事件条数** + `keep_first` | ❌ 无公开说明 |
| **SWE-agent** | 可替换组件 | `HistoryProcessor`（最早把压缩做成独立可替换组件） | ❌ 无公开说明 |
| **Aider** | **前置选料** | tree-sitter 抽符号 → 图排序（PageRank 系）→ token 预算（repo map） | **唯一用「前置选料」而非「事后压缩」** |
| **本项目** | **3 层** | Append-Only（<80%）→ 工具输出落盘 → 摘要 | ✅ 显式设计（免除滚动裁切） |

#### 我们的定位

**我们属于「有 prefix cache 显式设计」的少数派（Claude Code / dsh / 我们）**。

**与 Claude Code 的 4 级流水线对比**：

| 层 | Claude Code | 本项目 |
| --- | --- | --- |
| 1 | 工具结果落盘 ≤50k 字符 | ✅ 工具输出落盘（`budget_tool_results`） |
| 2 | 无模型裁剪 | ❌ **无此层** |
| 3 | 微压缩 | ❌ **无此层** |
| 4 | 全量摘要 | ✅ 离散块摘要 |
| 5 | 413 反应式压缩 | ❌ **无此层**（我们只有主动阈值） |

**我们缺三层**。其中「413 反应式压缩」最值得补——
当 provider 返回 context overflow 错误时，自动压缩重试一次。
（pi 也有这个：`compaction.md` 提到「provider context-overflow error... can select one compact-and-retry recovery attempt」）

#### 与 dsh 的对比（dsh 是最接近的对手）

| 维度 | dsh | 本项目 |
| --- | --- | --- |
| 触发公式 | `floor(min(W×0.8, W−O−64K))` | `0.8 × budget` |
| 保留量 | 最近 16% 逐字 | `_find_cut` 找 user 边界 |
| 压缩前剪工具输出 | ✅ **不调模型** | ✅ 我们的「免费层」 |
| 可插拔 | ✅ seam | ❌ 硬编码 |
| 破坏点清单 | ✅ 逐条列出 | ⚠️ 只有 docstring 说明 |

**dsh 的公式 `min(W×0.8, W−O−64K)` 比我们的 `0.8×budget` 严谨**：
它考虑了输出预留（`O`）和固定保留（64K）。
我们的百分比在小窗口下会压得太狠。

### 2.3 维度三：工具并发——有四种正交范式

**这是本次调研最有价值的发现**：并发不是"串行 vs 并行"二选一，而是**四种正交范式**：

| 范式 | 代表 | 机制 | 并发性来自 |
| --- | --- | --- | --- |
| **① 读并写斥** | **Codex** | `Arc<RwLock<()>>`；`supports_parallel_tool_calls()` 决定拿读锁还是写锁 | **工具属性**（每工具声明） |
| **② 全串行** | Claude Code / SWE-agent / Aider / OpenHands（默认 1） | 逐个执行 | 无 |
| **③ 自声明 + 上限 + 保序** | **dsh** | `isConcurrencySafe(args)` + `maxParallelToolCalls`（默认 10）+ **结果按模型给出顺序** | 工具自声明（**可看参数**） |
| **④ 子代理爬坡** | Kimi `AgentSwarm` | 5 起、每 700ms +1、可封顶、≤128 | 子代理数量 |

#### 本项目的定位：范式 ③ 的保守变体

我们的做法：

```python
# registry.py:161
has_sequential = any(t is None or not t.is_parallel_safe for tc in tool_calls)
if has_sequential:
    # 整批串行（按模型输出顺序）
else:
    # 全只读 → asyncio.gather 并发
```

**与 dsh 对比**：

| 维度 | dsh | 本项目 |
| --- | --- | --- |
| 声明粒度 | `isConcurrencySafe(args)`（**看参数**） | `is_parallel_safe`（**不看参数**） |
| 混合批次 | 可部分并行（安全工具并发，不安全的成屏障） | **全部串行** |
| 结果顺序 | 按模型给出顺序 | 按模型给出顺序 ✅ |
| 并发上限 | 默认 10 | 无上限（只读批次） |
| 默认值 | — | **`False`（fail-safe）** |

**我们比 dsh 保守的地方**：

dsh 的 `isConcurrencySafe(args)` 接受参数，意味着 `read("/etc/passwd")` 和
`read("/tmp/x")` 可以判定为不同安全性。我们只看工具名。

**为什么我们这样设计**（第 04 文档已论证）：判断"参数是否安全"需要语义分析，
容易出错。批级门控牺牲并发换正确性。

**但 dsh 的做法给了我们一个改进方向**：
如果 `is_parallel_safe` 能接受 args，就能处理"读不同文件可并发、读同一文件需串行"。

#### 与 Codex 的对比（最精细的方案）

Codex 用 Rust 的 `RwLock`：

```rust
// 概念示意
if tool.supports_parallel_tool_calls() {
    // 拿读锁，可并发
} else {
    // 拿写锁，排他
}
```

**这比我们的方案精细**，但同样依赖「工具自己声明」。

**一个关键洞察**：Codex 的 `supports_parallel_tool_calls()` 是**无参数的**，
说明它也不看参数——和我们的选择一致。
**dsh 是唯一看参数的**。

### 2.4 维度四：会话存储——三种「谁是真相」哲学

| 哲学 | 代表 | 机制 | 代价 | 收益 |
| --- | --- | --- | --- | --- |
| **① 存消息** | 多数产品 | 消息列表持久化 | 低 | fork/replay 需额外逻辑 |
| **② 存事件，消息靠投影** | **dsh** | `deriveMessages()`；压缩也只是一条带 `surfaceOp: replace` 的 user/message | **最高** | **fork/replay/崩溃修复全部免费** |
| **③ 存 git commit** | **Aider** | "Git 即会话" | 低 | 天然版本控制 |

#### 本项目的定位：① + 树结构

| 维度 | dsh | 本项目 |
| --- | --- | --- |
| 存储单位 | 事件 | **Entry**（本质是事件） |
| 模型历史 | **投影**（`deriveMessages`） | **投影**（`path_to_entry` 取活跃分支） |
| 压缩记录 | 一条 `surfaceOp: replace` | `CompactionEntry` |
| 崩溃安全 | **压缩锁最后释放**，崩溃留"孤儿锁"而非假"已完成" | ⚠️ 未考虑 |

**我们实际上接近 ②（事件 + 投影）**，因为我们存的是 Entry 树，
活跃分支通过 `path_to_entry` 投影出来。

**dsh 有一个我们没做的细节**：

> **它的压缩锁最后才释放，崩溃留下可检测的「孤儿锁」而不是假的「已完成」**。

这是分布式系统里的经典问题（两阶段提交/幂等）。我们的压缩没有锁机制，
崩溃后可能留下"有 compaction entry 但历史未压缩"的不一致状态。

---

## 3. 三个重要提醒（写对比时容易出错的地方）

### 3.1 Trae 的 75.2% 和 mini-swe-agent 的 74% 不可直接比较

| 产品 | SWE-bench Verified | 方法 |
| --- | --- | --- |
| **Trae 2.0** | 75.2% | **多模型采样 + Selector Agent（回归测试过滤 + 语法聚类投票 + 多 agent 验证）** |
| **mini-swe-agent** | ~74% | **单次单 agent 轨迹** |
| **Aider**（architect 模式） | 85% | 同一问题两次推理（Architect 出方案，Editor 转格式） |

**分母不同**：Trae 的 75.2% 是"多次采样 + 投票"的结果，
和单次轨迹不是一回事。**横向比较必须写清楚，否则会得出错误结论。**

### 3.2 harness 侧唯一的强证据是 Terminal-Bench 2.1

Snorkel 榜（按 agent+model 组合打分）：

| 排名 | 产品 | 成绩 |
| --- | --- | --- |
| 前 11 中占 4 席 | **Codex CLI** | 最高 **87.4%** |
| 第 2 | **Claude Code** | **83.8%** |
| 第 9 | mini-SWE-agent | 76.2% |
| — | dsh / Trae / Kimi / WorkBuddy | **均无该榜成绩** |

**对我们项目的意义**：
我们的 Terminal-Bench 2.1 是 **71/89 = 79.8%**。

这个数字**放在榜单上大概在第 5-8 名的位置**（比 mini-SWE-agent 的 76.2% 高，
比 Claude Code 的 83.8% 低）。

**但要谨慎**：
- 榜单是按 agent+model 组合打分的，我们用的模型可能不同
- 我们是单次运行，榜单可能是多次取优

### 3.3 资料缺失清单（诚实标注）

| 产品 | 缺失内容 |
| --- | --- |
| **WorkBuddy** | **缺口最大**：主循环形态、上下文压缩、工具并发、会话格式、记忆提炼——**全部未公开** |
| **Trae 产品侧** | 会话格式、fork、子代理、权限门禁、Terminal-Bench——全缺（开源的 trae-agent 与产品内核不是一回事） |
| **Kimi Work** | 内部架构完全未公开；Kimi Code CLI 缺：普通工具是否并行、prefix cache 策略、**自家 harness 的公开 benchmark** |
| **dsh** | 跨会话长期记忆（`docs/subsystems/memory.md` 不存在）、官方 benchmark |
| **Claude Code** | 全部实现细节均为**【社区逆向】**（source map 泄漏），非官方确认 |
| **Codex** | CLI 在 SWE-bench 上的官方单独成绩未找到 |

---

## 4. 我们缺什么（工业界已有，我们没有）

| 缺口 | 谁有 | 价值 | 优先级 |
| --- | --- | --- | --- |
| **推测式预压缩** | Codex（后台预跑 compact） | 用户感知延迟降为 0 | **高** |
| **413 反应式压缩** | Claude Code、pi | 防止 provider 报错后无法继续 | **高** |
| **压缩锁 + 孤儿检测** | dsh | 崩溃一致性 | 中 |
| **`isConcurrencySafe(args)`** | dsh | 更细的并发粒度 | 中 |
| **子代理爬坡** | Kimi `AgentSwarm` | 大规模并行 | 低 |
| **前置选料（repo map）** | Aider | 减少需要压缩的量 | **高**（思路值得借鉴） |
| **编辑指令式**（非工具调用） | Aider | 大改动场景更可靠 | 低 |
| 微压缩 / 无模型裁剪 | Claude Code | 省 token | 中 |

**最值得补的两条**：

1. **Aider 的「前置选料」思路**：不是"撑爆了再压"，而是**一开始就只放必要内容**
   （tree-sitter 抽符号 → PageRank 排序 → token 预算）。
   这是与我们完全相反的思路，但代价是**需要静态分析能力**。

2. **Codex 的推测式预压缩**：在 checkpoint 上**后台预跑**压缩，
   下次真需要时直接命中。用户完全感知不到压缩延迟。

---

## 5. 我们的独特之处（别人没有的）

| 特性 | 谁有 | 说明 |
| --- | --- | --- |
| **长期记忆（双层时间）** | ❌ 全都没有 | pi 无、Claude Code 无、Codex 无；只有 WorkBuddy 有"记忆"但未公开 |
| **批级门控（一写全串行）** | ⚠️ 只有我们 | Codex 是读并写斥，dsh 是自声明 + 上限，我们是**批级** |
| **`is_parallel_safe` 默认 False** | ⚠️ 未见 | 多数产品默认并行或需显式声明并行；我们反过来的 fail-safe 默认 |
| **免费层落盘零 LLM 调用** | ✅ Claude Code 类似 | Claude Code 的"工具结果落盘"是 4 级的第 1 级 |
| **转录本三阶段自愈** | ⚠️ 未见完全对应 | 多数产品靠"不产生脏历史"，我们是主动修复 |
| **两种干预时机（steering/follow-up）** | ✅ pi、Kimi | 与 pi 语义一致 |

**最有说服力的一条**：**长期记忆是全行业空白**。
调研的 10 个产品中，没有一个有公开的跨会话记忆实现
（WorkBuddy 有"记忆"但完全未公开细节）。

---

## 6. 一张图总结我们的位置

```
                    上下文压缩精细度
                          ↑
   Claude Code (4级) ●    |    ● dsh (可插拔+工具输出剪枝)
                          |
        Codex (推测式) ●  |
                          |
   ───────────────────────┼───────────────────────→ 工具并发精细度
                          |            ● Codex (RwLock)
        本项目 (3层) ●    |      ● dsh (自声明+上限)
                          |
   OpenHands (条数) ●     |   ● 本项目 (批级门控)
        SWE-agent ●       |
   ───────────────────────┘
        Aider (前置选料) ●  ← 完全不同的思路：不压缩，一开始就少放
```

**我们的位置**：
- 上下文压缩：**中上**（有 3 层，有 prefix cache 显式设计，但缺反应式/推测式）
- 工具并发：**中**（批级门控，保守但正确）
- 长期记忆：**唯一有实现的**（全行业空白）
- 会话存储：**中上**（树状 + 投影，接近 dsh 的事件溯源）

---

## 7. 面试话术

### 关于横向定位

> "我把 10 个 agent harness 都调研过：Codex 开源 Rust、Claude Code 有社区逆向、
> 加上 Kimi CLI、dsh、OpenHands、SWE-agent、Aider。
> 最让我意外的是**工具并发其实有四种正交范式**，不是串行或并行二选一——
> Codex 用 RwLock 做读并写斥，dsh 让工具自声明`isConcurrencySafe(args)`再加并发上限，
> Kimi 用子代理爬坡，Claude Code 干脆全串行还明说这是刻意选择。
> 我选的是批级门控，比 dsh 保守，但换来『零静态分析依赖』。"

### 关于 prefix cache

> "调研下来发现，**前缀缓存是真正拉开工程水平的分水岭**——
> 10 个产品里只有 Claude Code 和 dsh 把它写成了显式设计目标。
> Claude Code 甚至让压缩子代理刻意继承完整工具集，就是为了让它命中缓存。
> 我的三层压缩也是围绕这个设计的：80% 以下完全不动，
> 只把超大工具输出落盘（零 LLM 调用），最后才摘要。"

### 关于长期记忆（这是我们的强项）

> "长期记忆这块，我调研的 10 个产品里只有 WorkBuddy 提了『记忆』但完全没公开细节，
> 其他 for pi、Claude Code、Codex 全都没有。
> 我做的是双层时间建模——每条事实带 valid_from/valid_to，
> 新值覆盖时旧值进 history 链，所以能回答『你之前用什么版本』这种问题。"

### 关于 Terminal-Bench 成绩的诚实表述

> "我 Terminal-Bench 2.1 是 71/89 = 79.8%。
> 放在 Snorkel 榜上大概是第 5-8 名——比 mini-SWE-agent 的 76.2% 高，
> 比 Claude Code 的 83.8% 低。但要说清楚：榜单是按 agent+model 组合打分的，
> 我用的是什么模型、跑了几次，都影响这个数字。"

---

## 附录：可信度声明

| 结论 | 依据 | 可信度 |
| --- | --- | --- |
| Codex 用 RwLock 做读并写斥 | 开源 Rust 源码 | 【官方】高 |
| Claude Code 4 级流水线 | 社区逆向（source map 泄漏） | 【社区逆向】中 |
| dsh 的 `isConcurrencySafe(args)` | 开源 TS 源码 | 【官方】高 |
| Trae 2.0 删除 Plan 阶段 | 官方博客 | 【官方】中 |
| Kimi `AgentSwarm` 爬坡 | 开源 TS 源码 | 【官方】高 |
| WorkBuddy 架构 | **未公开** | 【未找到】低 |
| Trae 产品侧细节 | **未公开** | 【未找到】低 |
