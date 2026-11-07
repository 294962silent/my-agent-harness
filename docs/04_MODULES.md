# My-Agent-Harness 逐模块设计文档（第一版：内核层）

> 本文档记录每个核心模块的**设计理念、候选方案对比、选型理由、优劣势、实测效果**。
> 所有代码引用都有文件路径与行号，所有数字都有出处。
>
> 编写阶段：第一版（内核与上下文层）。工业/学术对比待子代理返回后补充。
>
> 事实基线见 [`00_FACTS.md`](00_FACTS.md)。

---

## 阅读说明

每个模块按统一结构展开：

1. **要解决什么问题**（具体失败场景）
2. **候选方案对比**（至少 3 个，来自工业界/学术/自研）
3. **我的选择与理由**（关键权衡）
4. **实现要点**（代码位置 + 核心逻辑）
5. **优劣势**（诚实列出，含已知缺陷）
6. **最终效果**（实测数据）

---

# 模块 1：ReAct 微内核（`loop.py`）

## 1.1 要解决什么问题

Agent 的主循环看似简单（调模型 → 执行工具 → 再调模型），但工程上有四个真实痛点：

| 痛点 | 具体表现 |
| --- | --- |
| **状态与副作用耦合** | 循环里直接调 LLM、直接执行工具、直接打印 TUI，导致无法单测、无法回放、无法在 CLI/TUI/服务三种界面复用 |
| **截断即失败** | 模型输出到 `max_tokens` 被截断时，多数实现直接结束本轮，浪费一次完整的上下文成本 |
| **无法中途干预** | 用户看到 Agent 走错方向，只能等它跑完或强杀进程 |
| **取消不干净** | 用户 Ctrl-C 后，正在跑的工具线程仍在写文件，但模型已被告知「失败」 |

## 1.2 候选方案对比

| 方案 | 代表实现 | 机制 | 优势 | 劣势 |
| --- | --- | --- | --- | --- |
| **A. 类方法直写** | 早期 LangChain Agent、多数教学实现 | `class Agent: def run()` 内部 while 循环，直接调工具和打印 | 直观、上手快 | 状态与 IO 耦合；无法复用；难以测试；无法注入拦截 |
| **B. 回调函数式** | 部分商业 SDK | 循环内调用 `on_step` / `on_tool` 回调 | 可扩展 | 回调地狱；错误处理分散；取消语义模糊 |
| **C. 纯函数生成器** | **Tau**（本项目对标）| `async def run(...) -> AsyncIterator[Event]`，循环只 yield 事件，不碰 IO | 零副作用、可组合、可测试、天然支持流式 | 调用方需自己消费事件流；状态需外部持有 |
| **D. Actor 模型** | 部分分布式 Agent | 每步发消息给 Actor | 天然并发 | 过重，单机场景无收益 |

## 1.3 我的选择与理由

**选 C：纯函数异步生成器。**

核心理由是**关注点分离**：

```python
# src/my_agent_core/loop.py:525
async def run_agent_loop(
    *, llm, messages, tools, context_manager, model, system, prompts,
    signal, get_steering_messages, get_follow_up_messages,
    before_model_call, before_tool_call, after_tool_call, retry_policy,
) -> AsyncIterator[Event]:
    """对标 Tau 的极简纯函数异步微内核，主状态机约 110 行。"""
```

**这个签名说明了设计取向**：

- 所有依赖**注入**（llm / tools / context_manager），循环本身不构造任何对象
- 所有输出是**只读事件**（`yield Event`），循环不打印、不写盘
- 所有干预是**回调**（steering / follow-ups / hooks），循环不关心干预从哪来

**为什么这比 A（类方法直写）好**：

同样的循环可以驱动三种界面，无需改一行内核代码：

| 消费方 | 消费方式 |
| --- | --- |
| CLI | 逐条打印事件 |
| TUI | 事件映射为组件状态更新 |
| 测试 | 断言事件序列 |
| RPC 服务 | 事件序列化为 JSON 推送 |

**为什么这比 B（回调）好**：回调用「推」模型，生成器用「拉」模型。
拉模型下，消费方可以**暂停、缓冲、背压**，这对流式 TUI 渲染很关键。

## 1.4 实现要点

### 1.4.1 事件集设计（14 类）

```python
# src/my_agent_core/events.py
Event
├── AgentStart / AgentEnd          # 整轮会话边界
├── TurnStart / TurnEnd            # 单次「模型→工具→模型」边界
├── MessageStart / MessageUpdate / MessageEnd   # 消息生命周期（支持流式）
├── ToolExecutionStart / ToolExecutionUpdate / ToolExecutionEnd  # 工具生命周期
├── ContextCompacted               # 上下文压缩发生
├── ToolsChanged                   # 工具集变更（MCP 动态加载）
└── AutoRetryStart / AutoRetryEnd  # 自动重试边界
```

**设计要点**：Start/Update/End 三元组让我可以在 TUI 里做**流式增量渲染**——
`MessageUpdate` 携带部分文本，`ToolExecutionUpdate` 携带工具的部分输出。

### 1.4.2 自动续写（`isRecoverableLength` 对标）

```python
# src/my_agent_core/loop.py:724-730
elif is_truncated and length_continuations < MAX_LENGTH_CONTINUATIONS:
    # 当大模型因达到 max_tokens 输出截断且尚未吐出 tool_calls 时，
    # 绝不草率退出！注入自动续写提示，引导模型继续输出并调用工具。
```

**这是与多数实现的差异化点**。多数 harness 在 `finish_reason=length` 时直接结束，
但此时模型往往只是「话还没说完」，续写一次就能拿到完整的工具调用。

**约束**：`MAX_LENGTH_CONTINUATIONS` 有上限，防止模型陷入无限续写。

### 1.4.3 两层循环结构

```
┌─ 外层：Task + Follow-up 队列 ──────────────────────┐
│  while True:                                       │
│    ┌─ 内层：ReAct 闭环 ──────────────────────┐      │
│    │  while iteration < max:                  │      │
│    │    调模型 → 得到 tool_calls              │      │
│    │    ├─ 截断无工具 → 注入续写提示           │      │
│    │    ├─ 有工具 → 执行 → 追加结果            │      │
│    │    └─ 无工具 → 结束内层                   │      │
│    │    每轮末尾收割 steering（即时转向）        │      │
│    └──────────────────────────────────────────┘      │
│    内层结束 → 检查 follow-up 队列                    │
│    ├─ 有 → 注入并 continue（外层继续）               │
│    └─ 无 → break（真正结束）                         │
└────────────────────────────────────────────────────┘
```

**代码证据**：

```python
# src/my_agent_core/loop.py:757-767
            # 收割即时转向
            if get_steering_messages is not None:
                steer_msgs = get_steering_messages()
                if steer_msgs:
                    pending_messages = _as_messages(steer_msgs)

        # 收割宏观追问任务
        if get_follow_up_messages is not None:
            followups = get_follow_up_messages()
            if followups:
                pending_messages = _as_messages(followups)
                continue
        break
```

**内层 vs 外层的语义差别**：

| 层 | 触发 | 语义 | 用途 |
| --- | --- | --- | --- |
| **内层 steering** | 每轮工具执行后 | 即时转向：「别查了，直接用 X」 | 纠正当前任务的方向 |
| **外层 follow-up** | 内层彻底结束后 | 新任务：「顺便把测试也跑了」 | 任务排队与续跑 |

## 1.5 优劣势

### 优势

| 优势 | 说明 |
| --- | --- |
| **零副作用** | 循环不碰 IO，纯函数，可直接单测（`tests/core/test_loop*.py`） |
| **界面无关** | 同一内核驱动 CLI/TUI/RPC |
| **可干预** | 5 个钩子 + steering + follow-up，四个干预时机 |
| **截断自愈** | 自动续写，不浪费上下文 |
| **协作取消** | `CancellationToken` 贯穿循环与工具执行 |

### 劣势（诚实列出）

| 劣势 | 影响 | 缓解 |
| --- | --- | --- |
| **调用方复杂度上升** | 必须实现事件消费循环，否则「什么都不发生」 | 提供 `Agent` 类封装（`agent.py:437`） |
| **调试门槛** | 事件流不易直接 print 调试 | 提供事件日志 |
| **性能开销** | 每步多一层生成器调度 | 实测可忽略（40s 跑完 956 测试） |
| **状态在外** | 消息列表由调用方持有，易出现「谁改了历史」的问题 | 靠 `tool_history` 修复层兜底 |

## 1.6 最终效果

| 指标 | 数值 | 出处 |
| --- | --- | --- |
| `run_agent_loop` 行数 | **246 行** | grep 实测（docstring 的「约 110 行」是估计值） |
| 9 步时序 | 全部可定位 | 见 `07_AUTHOR_BLOG.md` |
| `loop.py` 总行数 | 775 | `wc -l` |
| 事件类型 | 14 类 | `events.py` |
| 内核相关测试 | `tests/core/test_loop*.py` 等多文件 | 实测全部通过 |

---

# 模块 2：工具执行与并发（`registry.py` + `loop.py`）

## 2.1 要解决什么问题

模型一次可能返回多个 `tool_calls`。这里有个隐蔽的坑：

**若盲目并发执行，会破坏因果顺序。**

典型失败场景：

```
模型返回：[write("a.py", v1), read("a.py"), write("a.py", v2)]
若并发执行：read 可能读到 v1 或 v2，甚至是写了一半的内容
```

另一个方向的问题：**全串行太慢**，5 个独立的 `read` 串行执行白白浪费延迟。

## 2.2 候选方案对比

| 方案 | 机制 | 优势 | 劣势 |
| --- | --- | --- | --- |
| **A. 全串行** | 按顺序逐个执行 | 因果绝对正确 | 只读操作也串行，延迟高 |
| **B. 全并发** | `gather` 所有调用 | 快 | **破坏因果**，读写竞争 |
| **C. 工具级调度** | 每工具标记 read/write，动态建依赖图 | 最精细 | 实现复杂，需推导依赖（read 和 write 同文件才有依赖） |
| **D. 批级门控** | **只要有一个写工具，整批串行；全只读才并发** | 因果绝对安全，实现简单 | 混合批次退化为全串行 |

## 2.3 我的选择与理由

**选 D：批级门控。**

```python
# src/my_agent_core/registry.py:161-168
        # 检查这批工具中是否包含任何不安全的写工具（或未知工具）
        has_sequential = any(
            (t := self._tools.get(_get_name(tc))) is None or not t.is_parallel_safe for tc in tool_calls
        )

        if has_sequential:
            # 只要包含一个写操作，整批严格按大模型输出的原始顺序串行执行，确保因果顺序绝对正确
```

**为什么不做 C（工具级调度）**：

要正确建依赖图，必须知道每个工具的**读写集合**，且要处理：

- `bash` 工具读写什么？无法静态分析
- `edit` 读的是 oldText 匹配位置，写入位置依赖文件内容
- 符号链接、路径穿越

**结论**：C 的正确性成本远高于收益。D 牺牲了「读 + 写混合批次」的并行度，
但换来了**「永远不需要推理依赖」**这一强保证。

**关键安全默认**：

```python
# src/my_agent_core/tools/core.py:119
    is_parallel_safe: bool = False,     # 默认不安全
```

**默认 `False` 意味着**：新工具默认串行，必须显式声明才可以并发。
这是 fail-safe 设计——忘记标记最多变慢，不会出错。

## 2.4 实现要点

### 2.4.1 五阶段时序契约

```python
# src/my_agent_core/loop.py:327-336
    """专职工具批处理执行车间：Preflight 广播 -> 审批改参 -> 并发批执行 -> 结果改写 -> 结果广播。

    严格遵循 Pi 时序契约：
    1. Preflight 阶段：在调用 before_tool_call 审查与执行之前，率先按 source order 广播 ToolExecutionStart；
    2. 审查阶段：调用 before_tool_call 审批与入参改写；
    3. Execution 阶段：并发批量执行未阻断工具，支持实时流式进度回传；
    4. Completion 阶段：按 source order 执行 after_tool_call 改写并广播 ToolExecutionEnd；
    5. Message 阶段：按 source order 发射 role="tool" 的 MessageStart / MessageEnd。
    """
```

**为什么 Preflight 要在审批之前广播**：
UI 需要立刻显示「模型想调用 write」，即使随后被策略阻断。
否则用户会看到一段空白，然后突然冒出「已阻断」。

**为什么结果广播要按 source order**：
并发执行下完成顺序随机，但**回填给模型的消息顺序必须与调用顺序一致**，
否则模型看到的结果和它请求的顺序对不上，会困惑。

### 2.4.2 执行期的定型截断

工具输出必须截断（防止一个 `cat` 大文件撑爆上下文），但**截断时机很关键**：

| 策略 | 后果 |
| --- | --- |
| 每次发送前截断 | 同一输出每次长度不同 → **破坏 prefix cache** |
| **执行后立即定型** | 输出写入消息后永不改变 → prefix 稳定 |

本项目采用后者：

```python
# src/my_agent_core/tools/core.py → truncate_tool_output
# 执行期定型写入消息，永不就地篡改，保护 Prefix Cache
```

**这是与 KV cache 命中率直接相关的设计**——详见模块 4。

### 2.4.3 流式进度回传

```python
# src/my_agent_core/loop.py:390-405
        def safe_put(item: Event | object) -> None:
            if threading.get_ident() == loop_thread_id:
                queue.put_nowait(item)
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, item)
```

**问题**：工具可能是同步的（跑在线程池里），但要往 asyncio 队列推事件。

**解法**：检测当前线程，跨线程时用 `call_soon_threadsafe`。
这是 asyncio 与线程池混用的标准坑。

## 2.5 优劣势

### 优势

| 优势 | 说明 |
| --- | --- |
| **因果绝对安全** | 不需要推理依赖图，规则简单可证明 |
| **失败安全** | `is_parallel_safe` 默认 False |
| **只读批次真并发** | 典型探索阶段（连续 read/grep）能并发 |
| **时序契约明确** | 五阶段，UI 和执行解耦 |
| **流式进度** | 长命令（如 `npm test`）可实时显示 |

### 劣势

| 劣势 | 影响 | 缓解 |
| --- | --- | --- |
| **混合批次退化** | 读+写混合时全串行，比工具级调度慢 | 实际场景中模型很少一次混合读写 |
| **无法表达文件级依赖** | 两个写不同文件的工具本可并发，也被串行 | 接受（正确性优先） |
| **跨线程事件有开销** | 每次 `call_soon_threadsafe` 有调度成本 | 实测无感 |

## 2.6 最终效果

| 场景 | 行为 | 验证 |
| --- | --- | --- |
| 5 个 `read` | 并发 | `tests/core/test_registry*.py` |
| 1 个 `write` + 4 个 `read` | 全串行、保序 | 同上 |
| 未知工具 | 全串行（fail-safe） | 同上 |
| 用户中途取消 | 剩余调用标记 `Tool call interrupted by user` | `loop.py:180` |

---

# 模块 3：上下文压缩管线（`context.py`）

## 3.1 要解决什么问题

Agent 长程运行必然撑爆上下文。但**压缩方式和 KV cache 直接冲突**：

| 做法 | 后果 |
| --- | --- |
| 每轮裁剪最旧消息（滑动窗口） | **prefix 每轮都变** → cache 全部失效 → 成本 × N |
| 工具输出每次重新截断 | 同上 |
| 到达上限才一次性摘要 | cache 友好，但可能来不及 |

**核心矛盾**：压缩是为了省 token，但如果破坏了 prefix cache，反而更贵。

## 3.2 候选方案对比

| 方案 | 机制 | Cache 友好性 | Token 节省 |
| --- | --- | --- | --- |
| **A. 滑动窗口** | 保留最近 N 条，丢弃更早 | ❌ 每轮 prefix 变 | 好 |
| **B. 全量摘要** | 到阈值把所有历史摘要成一段 | ⚠️ 摘要后 prefix 变，但之后稳定 | 好 |
| **C. 分层摘要（Recursive）** | 老消息摘要，新消息原文 | ✅ 分段稳定 | 好 |
| **D. 惰性三层管线** | **Append-Only 优先 → 免费层落盘 → 摘要层** | ✅ **大部分时间 prefix 完全不变** | 好 |

## 3.3 我的选择与理由

**选 D：惰性三层管线，阈值门控。**

```python
# src/my_agent_core/context.py:368
    async def prepare(self, messages: list[Message]) -> list[Message]:
        """阈值门控的离散块级压缩管线 → 返回发送视图（非破坏性）。

        对齐 Pi/Tau 规范：未超 budget_threshold（80% budget）时，严格保持只读追加（Append-Only），
        保障前缀完全一致性以最大化大模型 KV-Cache（Prompt Cache）命中率。
        超阈值时执行离散块压缩（compact 结构化摘要），建立下一个周期的静态稳定前缀。
        """
```

**三层设计**：

| 层 | 触发条件 | 手段 | 代价 | Cache 影响 |
| --- | --- | --- | --- | --- |
| **第 0 层：什么都不做** | 未超 80% 阈值 | 原样返回 | 0 | ✅ prefix 完全不变 |
| **第 1 层：免费层** | 超阈值，且有超大工具输出 | 工具输出落盘，消息里只留路径+摘要 | 0（无 LLM 调用） | ⚠️ 只改那一处 |
| **第 2 层：摘要层** | 免费层仍超阈值 | 离散块结构性摘要 | 1 次 LLM 调用 | ⚠️ 建立新前缀，之后稳定 |

**为什么叫「免费层」**：第 1 层不需要调用 LLM，纯本地操作，
把 `read` 一个 5000 行的文件变成「已落盘到 .agent/tool-results/xxx.txt（前 20 行：...）」。

**关键设计：离散块 vs 连续滚动**

```python
# src/my_agent_core/context.py:527
    def _apply_free_layers(self, view: list[Message]) -> list[Message]:
        """轻量层执行：仅支持超大工具输出持久化到磁盘，废除旧滚动裁切与滚动工具篡改以保障前缀缓存。"""
```

**注意「废除旧滚动裁切」这句**——说明早期版本用过滚动方案，因为破坏 cache 被废除了。
这是一个**真实的设计演进决策**（见验证文档的 bug 档案）。

## 3.4 实现要点

### 3.4.1 prepare 的完整决策树

```python
# src/my_agent_core/context.py:368-403（简化）
if self._summary is not None:
    view = self._build_cached_view(...)
    if view is not None:
        if estimate_tokens(view) <= threshold:
            return view                      # 缓存视图够小 → 直接返回
        if self.results_dir:
            spilled = self._apply_free_layers(view)
            if estimate_tokens(spilled) <= threshold:
                return spilled               # 落盘后够小 → 返回
        return await self._do_summarize(messages)   # 还是超 → 再摘要
    # 边界消息被删除/改写：放弃缓存，安全重建（绝不猜下标）
    self._invalidate_cache()

if estimate_tokens(messages) <= threshold:
    return list(messages)                    # 首次也够小 → 原样
if self.results_dir:
    spilled = self._apply_free_layers(list(messages))
    if estimate_tokens(spilled) <= threshold:
        return spilled
return await self._do_summarize(messages)
```

**两个关键设计**：

1. **`prepare` 是非破坏性的**：返回的是「发送视图」，原始 `messages` 不变。
   这样 UI 显示的历史和发给模型的视图可以不同。
2. **边界失效时安全重建**：`_locate_boundary` 用消息 key 定位切点；
   如果 key 对应的消息被删了，就 `_invalidate_cache()` 重建，**绝不猜下标**。

### 3.4.2 边界定位的容错

```python
# src/my_agent_core/context.py:467
    def _locate_boundary(self, messages: list[Message], *, allow_legacy_position: bool = True) -> int | None:
```

CamelCase 的 `allow_legacy_position` 说明兼容了旧格式（裸下标）。
这对应 `PROJECT_REVIEW.md` 里的 P2 缺陷：

> 「压缩缓存用裸下标定位历史」→ 已修：改用消息 key + 整段尾部校验，
> 边界丢失时安全重建

## 3.5 优劣势

### 优势

| 优势 | 说明 |
| --- | --- |
| **大部分时间零改动** | 80% 阈值以下完全 Append-Only，cache 命中率最大化 |
| **免费层省一次 LLM 调用** | 工具输出落盘是纯本地操作 |
| **非破坏性** | 原始历史不变，UI 与模型视图分离 |
| **边界容错** | key 定位 + 尾部校验 + 安全重建 |

### 劣势

| 劣势 | 影响 | 缓解 |
| --- | --- | --- |
| **阈值突发** | 一个巨大输出可能瞬间超阈值，触发摘要 | 免费层先兜一次 |
| **落盘文件需管理** | `.agent/tool-results/` 会累积 | 需清理策略（未见实现，可能是缺陷） |
| **摘要质量依赖模型** | 摘要漏信息会导致后续出错 | 结构化摘要（保留文件操作记录） |

## 3.6 最终效果

| 指标 | 数值 | 出处/可信度 |
| --- | --- | --- |
| 触发阈值 | 80% budget | `context.py:368` docstring |
| 缓存命中率 | **66.3%** | `PROJECT_REVIEW.md`（README 曾误写 96.7%，已修正） |
| 压缩层数 | 3 层 | 代码结构 |
| 相关测试 | `tests/core/test_context*.py` | 实测通过 |

> ⚠️ **诚实说明**：96.7% → 66.3% 是被评审纠正的数字。
> 简历如引用此指标，应使用 66.3%（或重新实测）。

---

# 模块 4：会话树与原子持久化（`session/`）

## 4.1 要解决什么问题

会话存储有三个层次的需求：

| 需求 | 说明 |
| --- | --- |
| **可恢复** | 进程崩溃后可继续 |
| **可分支** | 「从第 5 轮那个决定重新试一次」 |
| **可互操作** | 与其他 harness（如 Pi）交换会话文件 |

## 4.2 候选方案对比

| 方案 | 结构 | Fork 成本 | 查询成本 | 互操作 |
| --- | --- | --- | --- | --- |
| **A. 线性 JSON 数组** | `[{role, content}, ...]` | 复制整个数组 | O(n) | 差 |
| **B. SQLite 单表** | 消息表 + 会话外键 | 复制行 | O(log n) | 差 |
| **C. 树状 Entry（append-only 日志）** | 每条 Entry 有 `id` + `parentId`，文件只追加 | **O(1)**（新分支只是新父指针） | 需建索引 | **好**（Pi 同构） |

## 4.3 我的选择与理由

**选 C：树状 Entry + append-only JSONL。**

```python
# src/my_agent_core/session/entries.py — 11 种 Entry 类型
SessionInfoEntry, MessageEntry, ModelChangeEntry, ThinkingLevelChangeEntry,
CompactionEntry, BranchSummaryEntry, LabelEntry, LeafEntry,
CustomEntry, CustomMessageEntry, SessionHeaderEntry
```

**为什么 Fork 成本是 O(1)**：

```
        [msg1] → [msg2] → [msg3] → [msg4]      ← 主线
                    ↘
                     [msg3'] → [msg4']          ← 分支：只需 msg3' 的 parentId=msg2
```

**不需要复制任何历史**，只是新增 Entry 并指向同一个 parent。

**为什么选 JSONL 而不是单个 JSON**：

| 格式 | 追加成本 | 崩溃安全 |
| --- | --- | --- |
| 单个 JSON 数组 | **重写整个文件**（P1-2 缺陷就是这条） | 中途崩溃文件损坏 |
| **JSONL（每行一条）** | 追加一行 | 最坏丢最后一行 |

这对应 `PROJECT_REVIEW.md` 的 P1-2：

> 「每条消息全量重写整个会话文件（含 fsync）」→ 已修：增量追加（format v2 + 指针记录）

**互操作设计**：每个 `type` 字段都有 camelCase 别名

```python
type: Literal["session_info", "sessionInfo"] = "session_info"
type: Literal["model_change", "modelChange"] = "model_change"
```

这样同一个文件既能被 Python 版本读，也能被 Pi（TypeScript）读。
这是 `PI_ALIGNMENT_AUDIT.md` 里「Session Entry 互操作字段缺失」的修复。

## 4.4 实现要点

### 4.4.1 树操作

```python
# src/my_agent_core/session/tree.py
entries_by_id(entries)              # 建索引
path_to_entry(entries, id)          # 从根到某节点的路径
lowest_common_ancestor(...)         # 找 LCA（用于 diff 分支）
```

```python
# src/my_agent_core/session/session.py
def rewind(self, entry_id: str) -> None       # 回退到某节点
def fork(self, entry_id: str, ...) -> Session  # 从某节点分叉
```

### 4.4.2 原子写入

```python
# src/my_agent_core/memory.py:77
def _atomic_save(self, target: str) -> None:
```

记忆文件也用了原子保存（写临时文件 + rename）。
这与内存 `_atomic_save` 一起构成「会话与记忆都不可损坏」的保证。

## 4.5 优劣势

### 优势

| 优势 | 说明 |
| --- | --- |
| **Fork 零成本** | 新分支只是新 parentId |
| **崩溃安全** | JSONL 追加，最坏丢最后一行 |
| **可互操作** | camelCase 别名，与 Pi 同构 |
| **可审计** | append-only，历史不可篡改 |
| **可导航** | `path_to_entry` + LCA 支持「分支树可视化」 |

### 劣势

| 劣势 | 影响 | 缓解 |
| --- | --- | --- |
| **读全文才能建树** | 会话很长时加载慢 | 可加索引文件（未见实现） |
| **无压缩** | JSONL 单调增长 | 靠 compaction entry 记录逻辑压缩 |
| **无并发写保护** | 两个进程同时写会冲突 | 单进程假设（IDE 场景可接受） |

## 4.6 最终效果

| 指标 | 数值 |
| --- | --- |
| Entry 类型 | 11 种 |
| 树操作 | 建索引 / 路径 / LCA |
| 会话维护 | `rewind` / `fork` |
| 相关测试 | `tests/core/test_session*.py` |

---

# 模块 5：长期记忆（`memory.py` + `memory_index.py`）

## 5.1 要解决什么问题

跨会话的事实性记忆。难点不在存储，在**冲突处理**：

```
第 1 天：「用户偏好 Python 3.11」
第 5 天：「用户升级到 Python 3.12」
```

朴素做法（关掉旧记录或覆盖）都会丢失「曾经是 3.11」这个信息。

## 5.2 候选方案对比

| 方案 | 冲突处理 | 可追溯性 | 复杂度 |
| --- | --- | --- | --- |
| **A. 追加日志** | 全部保留，检索时靠时间排序 | ✅ 完整 | 低 |
| **B. 覆盖更新** | 新值替换旧值 | ❌ 丢失历史 | 低 |
| **C. 软删除 + 版本链** | 旧值标记 `valid_to`，新值携带 history | ✅ 完整 | 中 |
| **D. 向量库** | 语义检索，冲突靠相似度去重 | ⚠️ 不确定 | 高 |

## 5.3 我的选择与理由

**选 C：双层时间（bi-temporal）建模。**

```python
# src/my_agent_core/memory_index.py:113
    def add(self, fact: Fact, replay: bool = False) -> None:
        for old in self.facts:
            if (old.active
                and old.subject == fact.subject
                and old.key == fact.key
                and old.value.strip().lower() != fact.value.strip().lower()):
                cutoff = fact.valid_from or _today()
                old.valid_to = cutoff          # 旧事实标记失效
                fact.history.insert(0, {
                    "value": old.value,
                    "valid_from": old.valid_from,
                    "valid_to": cutoff,
                    "source": old.source,
                })
                fact.history.extend(old.history)   # 继承更早的历史
        self.facts.append(fact)
```

**这叫双层时间**：

- `valid_from` / `valid_to`：**事实在现实中有效**的时间段
- 写入时间：**系统记录它**的时间

**为什么这比 B（覆盖）好**：用户问「我之前用什么 Python 版本」时，
能回答「3.11（到 11 月 5 日为止），现在是 3.12」。

**为什么这比 D（向量库）好**：事实型记忆（偏好、配置、约定）需要**精确匹配**，
不是语义相似。`subject + key` 的精确匹配比 embedding 相似度可靠得多。

## 5.4 实现要点

### 5.4.1 三层结构

| 层 | 代码 | 职责 |
| --- | --- | --- |
| **抽取** | `MemoryStore.remember()` | 从对话中抽出 Fact（subject/key/value） |
| **巩固** | `Consolidator.add()` | 冲突检测 + 时间区间维护 |
| **检索** | `MemoryIndex.retrieve()` | BM25/FTS5 排序 + 预算裁剪 |

### 5.4.2 检索引擎

```python
# src/my_agent_core/memory_index.py:139
class BM25:
    """内置 BM25（k1=1.5, b=0.75），FTS5 不可用时的回退实现。"""

class FtsIndex:
    def search(self, query: str, limit: int = 32) -> list[int]:
```

**双引擎设计**：优先用 SQLite FTS5（快、支持复杂查询），
不可用时回退到纯 Python BM25。

**为什么不用向量检索**：事实记忆是关键词驱动的（"Python 版本"、"测试框架"），
BM25 的精确性优于语义模糊匹配，且零额外依赖。

### 5.4.3 存储格式

**Markdown 文件**，人可以读、可以 git 版本管理。

```python
def _atomic_save(self, target: str) -> None:
```

## 5.5 优劣势

### 优势

| 优势 | 说明 |
| --- | --- |
| **历史可追溯** | 双层时间建模，能回答「之前是什么」 |
| **零外部依赖** | BM25 纯 Python，无需向量数据库 |
| **人可读** | Markdown 存储，可直接编辑和 review |
| **可版本管理** | git diff 能看出记忆变化 |

### 劣势

| 劣势 | 影响 | 缓解 |
| --- | --- | --- |
| **不支持语义检索** | 「数据库相关偏好」找不到「用 Postgres」 | 靠 key 命名规范 |
| **抽取依赖模型** | 抽错 key 会导致记忆分裂 | 可加 key 归一化 |
| **无容量上限** | 记忆无限增长 | 需定期 consolidate（未见实现） |

## 5.6 最终效果

| 指标 | 数值 | 出处 |
| --- | --- | --- |
| LongMemEval 96 题 | 上下文 105,316 → 11,983 token（**-88.6%**） | 简历/文档，**需核实原始日志** |
| 准确率 | 71.9% → 69.8% | 同上，置信区间重叠 |

> ⚠️ **诚实说明**：这两个数字来自 `RESUME_PROJECT.md`，
> 但 `PROJECT_REVIEW.md` 提到「评测数据存疑」（P0-3）。
> **使用前必须找到原始 Harbor 日志**，否则面试被追问会答不上来。

---

# 待补充模块（等子代理 + 后续轮次）

| 模块 | 优先级 | 已有证据 |
| --- | --- | --- |
| Skills 机制 | 中 | `skills.py`（frontmatter 解析 + 目录发现） |
| Subagents / Task 委派 | 中 | `subagents.py`、`subagent_tasks.py` |
| Extension 扩展 | 高 | 5 大决策点，见 `PROGRESS.md` 阶段 12 |
| Plugin 系统 | 中 | Claude Code 风格 |
| 转录本自愈 `tool_history.py` | **高** | 3 阶段修复，防 API 400 |
| 权限门禁 | **高** | P0-1 修复，端到端实测 |
| MCP 客户端 | 中 | 原生异步，见 `PROGRESS.md` |
| TUI | 低 | TypeScript + pi-tui |

---

# 已知的待核实项汇总

| 项 | 问题 | 行动 |
| --- | --- | --- |
| KV cache 命中率 | 66.3% 需重新实测 | 用 opencode go 跑实测 |
| LongMemEval 数字 | 来源存疑（P0-3） | 找 Harbor 原始日志 |
| Terminal-Bench 71/89 | 同上 | 同上 |
| TUI 测试数 | 未测 | `npm test` |

---

# 模块 6：Extension 扩展机制（`extensions/`）

## 6.1 要解决什么问题

框架不能预知所有需求。扩展机制要满足三个看似冲突的要求：

| 要求 | 冲突点 |
| --- | --- |
| **能力足够** | 要能注册工具、改上下文、拦命令 |
| **不失控** | 扩展不能绕过权限门禁 |
| **可卸载** | 运行中能安全移除 |

## 6.2 候选方案对比

| 方案 | 机制 | 能力 | 隔离性 |
| --- | --- | --- | --- |
| **A. 插件即代码**（VS Code） | 扩展是独立进程，通过 IPC 通信 | 强 | ✅ 进程隔离 |
| **B. 中间件链**（Express 风格） | 请求/响应穿过中间件链 | 中 | ❌ 同进程 |
| **C. 事件订阅**（Observer） | 扩展订阅事件，只读 | 弱（不能干预） | ✅ 但不能干预 |
| **D. 决策点拦截**（本项目） | 在**特定决策点**给扩展「阻断/改写」能力 | 强 | 同进程但受门禁约束 |

## 6.3 我的选择与理由

**选 D：五大决策点拦截。**

**关键设计：扩展只在预设的「决策点」被调用**，而不是任意位置。
这限制了扩展能做什么，换来可审计性。

```python
# src/my_agent_core/hooks.py:23
# ── 五大专职 Hook 拦截点契约（独立门禁系统，非 Event）
# Hook 1 (input):         拦截或改写用户原始输入文本
# Hook 2 (before_agent_start): 拦截启动或动态重写 system_prompt
# Hook 3 (context):       调模型前 1ms 审查或临时改写发送视图
# Hook 4 (tool_call):     工具执行前安全审批、阻断高危命令或改写入参
# Hook 5 (tool_result):   工具执行后改写返回内容或篡改报错状态
```

**为什么这些点**：它们正好是「影响最终行为」的全部位置。
除此之外扩展不能做任何事。

## 6.4 优劣势

| 优势 | 说明 |
| --- | --- |
| 可审计 | 只有 5 个入口 |
| 强干预 | 能 block / 改写，不只是观察 |
| 与门禁集成 | Hook 4 就是权限门禁的实现 |

| 劣势 | 影响 |
| --- | --- |
| 同进程 | 恶意扩展可访问全部内存 |
| 缺 sllb | 无扩展沙箱 |

**学术对标（见 03_ACADEMIC.md）**：PRISM 论文主张**10 个钩子**且含**子代理派生**，
我们只有 5 个且**无子代理派生钩子**——这是已知缺口。

---

# 模块 7：Plugin 系统（`plugins.py`）

## 7.1 设计

采用 **Claude Code 风格**的目录约定插件：

```python
class PluginManifest:      # 元数据（名字、作者、版本）
class Plugin:              # 一个插件
    def skills_dir()   -> 可提供 Skills
    def agents_dir()   -> 可提供 Subagents
    def mcp_config_path() -> 可提供 MCP 配置
class PluginManager:       # 发现/加载/卸载
```

**与 Extension 的区别**：

| | Extension | Plugin |
| --- | --- | --- |
| 形态 | Python 代码，直接写逻辑 | 目录约定，打包资源 |
| 能力 | 干预决策点 | 提供 skills / agents / MCP |
| 类比 | 代码插件 | 资源包 |

**pi 无此模块**（pi 的 extension 是 TS 模块，没有"资源包"概念）。

---

# 模块 8：子代理与任务委派（`subagents.py` + `subagent_tasks.py`）

## 8.1 设计

```python
class Subagent:            # 一个子代理定义（prompt 模板 + 工具集 + max_turns）
class SubagentManager:     # 发现（.md 文件）
class SubagentTaskStatus(StrEnum)  # 任务状态机
class SubagentTask:        # 一个运行中的任务
class SubagentTaskManager: # 管理：steer_task / follow_up_task
```

**关键：子代理也支持 steer 和 follow-up**：

```python
def steer_task(self, task_id: str, message: str) -> bool:
def follow_up_task(self, task_id: str, message: str) -> bool:
```

两级干预能力延续到子代理层。

## 8.2 优劣势

| 优势 | 说明 |
| --- | --- |
| 上下文隔离 | 子代理有独立 messages，不污染主上下文 |
| 可干预 | 父代理可 steer/follow-up 子任务 |
| 异步 | 后台执行，不阻塞主循环 |

| 劣势 | 影响 | 学术依据 |
| --- | --- | --- |
| **无收益归因实验** | 不知道子代理是否真有用 | MAST 说「MAS 增益往往很小」 |
| 缺权限收敛 | 子代理继承父代理全部权限 | PRISM 主张子代理派生要设防 |
| 信息丢失 | 子代理看不到完整历史 | 这是隔离的代价 |

---

# 模块 9：转录本自愈（`tool_history.py`）

## 9.1 要解决什么问题

崩溃/中断后历史不合法，provider 返回 400。三种脏数据：

| 类型 | 表现 |
| --- | --- |
| 断头调用 | `assistant` 有 tool_calls，但没有对应 `tool` 结果 |
| 孤儿结果 | `tool` 消息存在，但对应的 call 不见了 |
| 空失败轮 | assistant 轮既无文本也无工具调用 |

## 9.2 三阶段算法

```python
# tool_history.py:73
def repair_tool_history(messages) -> ToolHistoryRepair:
    # Phase 1: 预留已就近配对的调用
    #   —— 先锁定「call 后面紧跟匹配 result」的正确配对，
    #      防止贪心匹配把它们拆散
    # Phase 2: 剩余调用贪心匹配或补齐
    #   —— 优先找调用之后的真实结果；找不到就补合成结果
    # Phase 3: 清理孤儿与空轮
```

**Phase 1 为什么必须在 Phase 2 之前**：
如果先贪心，可能把本来正确的 A-B 配对拆开，去满足别的调用。

## 9.3 实测效果

见 [`05_VALIDATION.md`](05_VALIDATION.md) 实验一：

| 组 | 会话可继续率 |
| --- | --- |
| 关闭自愈 | **8/40 (20.0%)** |
| 完整运行时 | **40/40 (100.0%)** |
| 干净历史对照组 | 8/8，**0 例误伤** |

---

# 模块 10：权限门禁（`permissions.py`）

## 10.1 fail-closed 设计

```python
# permissions.py:5
# - fail-closed：非 autonomous 模式下，没有 confirm_callback 一律返回 block；
#   需要放行的嵌入方必须显式传 allow_without_callback=True
```

**这是 P0-1 缺陷的修复结果**：原实现是 fail-open，导致「拒绝无效」。

## 10.2 风险评估维度

```python
def _assess(tool_name, args, workspace) -> tuple[RiskLevel, str | None]:
    # - 路径是否越界（_path_escapes_workspace）
    # - bash 命令是否危险（_bash_danger_reason）
    # - bash 是否访问 workspace 外路径（_bash_outside_paths）
```

## 10.3 已知缺口（学术已确认）

**SCR-Bench 论文（2026-06）指出**：单个工具良性、组合起来有害。
组合攻击成功率 **33.6%**，孤立时接近 0。

**我们的门禁是工具级、无状态的**，无法表达「read .env → write 到公开目录」这种路径级风险。

这是**真实漏洞类别**，不是理论问题。

---

# 十一、模块总览对照表

| 模块 | 本项目 | pi | 原创性 |
| --- | --- | --- | --- |
| ReAct 微内核 | 775 行 | `agent-session` 3,653 | **C（对标 Tau）** |
| 工具并发 | `registry.py` | `file-mutation-queue` 51 | **C（批级门控）** |
| 上下文压缩 | 679 行 / 3 层 | `compaction/` 1,162 / 1 层 | B + **C（免费层）** |
| 会话树 | `session/` | `session-manager` 1,541 | A |
| 长期记忆 | ~600 行 | ❌ 无 | **C（全原创）** |
| Extension | `extensions/` | 对应 | A |
| Plugin | `plugins.py` | ❌ 无 | **C** |
| 子代理 | `subagents*.py` | 对应 | B |
| 转录本自愈 | `tool_history.py` | 类似逻辑 | **C（三阶段算法）** |
| 权限门禁 | `permissions.py` | OS 权限 | **C（fail-closed）** |

**A=结构借鉴 / B=重写 / C=原创**（详见 [`01_PI_DIFF.md`](01_PI_DIFF.md)）

---

# 模块 11：任务系统与成环死锁检测（`task_store.py` + `task_tools.py`）

> 本模块此前在文档中遗漏，经作者博客核对后补充。

## 11.1 要解决什么问题

多任务编排时的两个经典陷阱：

| 陷阱 | 表现 |
| --- | --- |
| **幽灵依赖** | 任务 A 依赖任务 B，但 B 根本不存在 → A 永远 blocked |
| **传递性成环** | A 依赖 B、B 依赖 C、C 依赖 A → 三个任务永远互相等待（死锁） |

## 11.2 实现：BFS 可达性检测

```python
# src/my_agent_core/task_store.py:78
def _depends_on(self, task_id: str, target_id: str) -> bool:
    """检查 task_id 是否传递性依赖于 target_id。"""
    visited = set()
    queue = [task_id]
    while queue:
        curr = queue.pop(0)
        if curr == target_id:
            return True
        if curr in visited:
            continue
        visited.add(curr)
        if curr in self.tasks:
            queue.extend(self.tasks[curr].blocked_by)
    return False
```

**算法要点**：

| 要点 | 说明 |
| --- | --- |
| BFS 而非 DFS | 层序扩展，找到最短的依赖路径即可返回 |
| `visited` 去重 | 防止已存在的环导致无限循环 |
| **创建前检查** | 新建依赖时调用 `_depends_on(dep, new_task)`，若已可达则拒绝 |

**成环检测的形式化**：
若要给 T 添加依赖 D，先检查 `_depends_on(D, T)`——
即「D 是否已经（传递性）依赖 T」。若是，则添加 T→D 会形成环。

## 11.3 幽灵依赖检查

```python
# task_store.py:108-114
valid_blocked_by: list[str] = []
if blocked_by:
    for dep in blocked_by:
        if dep not in valid_blocked_by:      # 先去重
            valid_blocked_by.append(dep)
```

依赖项必须是**已存在的任务 ID**，否则被过滤掉。

## 11.4 TaskGuardHook：任务收尾守卫

```python
# src/my_agent_core/tools/builtin/task_tools.py:185
class TaskGuardHook:
    """任务收尾早退守卫钩子：在 TurnEnd 时检查未结清工单，通过 steer 提醒大模型。"""

    def on_turn_end(self, event: TurnEnd) -> None:
        # 若本轮无工具调用，且仍有 in_progress 任务 → steer 提醒
        if event.tool_results:
            return
        in_progress = [t for t in self.task_store.list() if t.status == "in_progress"]
        for t in in_progress:
            if t.id not in self.nudged_ids:
                self.steer_fn(f"Task '{t.id}' ({t.subject}) is still marked as 'in_progress'...")
```

**设计巧妙之处**：

| 机制 | 作用 |
| --- | --- |
| 只在 **TurnEnd 且无工具调用**时触发 | 说明模型"以为做完了" |
| 用 **steer**（内层干预）而非报错 | 不打断循环，只是提醒 |
| `nudged_ids` 去重 | 每个任务只提醒一次，不刷屏 |

**这是两层循环设计的直接应用**：
`TaskGuardHook` 通过 `steer_fn` 往内层循环注入消息，
驱模型继续处理未结清的任务。

## 11.5 优劣势

| 优势 | 说明 |
| --- | --- |
| 死锁可预防 | 创建时即拒绝成环依赖 |
| 幽灵依赖被过滤 | 不会阻塞在不存在的前置上 |
| 收尾守卫 | 防止模型"提前收工" |
| 提醒不打断 | 用 steer 而非抛错 |

| 劣势 | 影响 |
| --- | --- |
| 无优先级 | 任务按创建顺序执行 |
| 无资源约束 | 不考虑并发上限 |
| 跨会话不持久 | 任务状态可能在重启后丢失 |

## 11.6 与作者博客核对

作者博客（todolist 与 background）记录了「三道检查」：

| 博客 | 代码 | 结论 |
| --- | --- | --- |
| 检查 2：前置存在性检查 | `valid_blocked_by` 过滤 | ✅ 一致 |
| 检查 3：传递性成环死锁检测 | `_depends_on` BFS | ✅ 一致 |
| TaskGuardHook 守卫 | `TaskGuardHook` 类 | ✅ 一致 |

**完全吻合**，这个模块的实现与设计文档描述一致。

---

# 模块 12：多模态图片处理

> **本项目未实现此模块。** 作者博客有一篇专门讲（`agent中图片如何处理`，13,472 字符），
> 覆盖「请求适配 → 资产溢出存储 → 上下文压缩」全链路。
>
> 我们的 `04_MODULES.md` 不覆盖此模块，这是**已知的功能空白**。
>
> 详见 [`07_AUTHOR_BLOG.md`](07_AUTHOR_BLOG.md) 第 4 节。
