# 与 pi-agent 的量化对照

> 核心问题：**我们借鉴了 pi 多少？改了多少？**
>
> 数据来源：`npm pack @earendil-works/pi-*@1.5.0`（6 个官方包，2026-11-07 下载解包）
> 提取位置：`%TEMP%\pi-reverse\`
>
> 说明：pi 只发布编译后的 `.js` 与 `.d.ts`（无原始 `.ts`），
> 但 `.js` 保留完整实现，`.d.ts` 给出完整类型签名，足以做结构对照。

---

## 1. 总体规模对比

| 维度 | pi（6 个包） | 本项目（Python） | 比例 |
| --- | --- | --- | --- |
| **总代码行** | **108,459** | **18,170** | **16.8%** |
| 文件数 | 730 | 89 | 12.2% |
| 语言 | TypeScript | Python | — |

### 1.1 逐包对比

| pi 包 | 文件数 | 行数 | 本项目的对应 | 我们的行数 |
| --- | --- | --- | --- | --- |
| `pi-coding-agent` | 340 | 65,603 | `my_coding_agent` | 7,410 |
| `pi-ai` | 204 | 20,759 | `my_agent_llm` | 3,047 |
| `pi-tui` | 46 | 16,212 | `my-pi-tui`（TypeScript，独立） | 未计入 |
| `pi-codemode` | 16 | 2,228 | ❌ 无对应 | 0 |
| `pi-mcp` | 18 | 2,217 | `my_coding_agent/mcp`（内嵌） | 含在 7,410 |
| `pi-agent-core` | 6 | 1,440 | `my_agent_core` | 7,713 |
| **合计（不含 TUI）** | 564 | 92,247 | — | 18,170 |

**一个反直觉的观察**：
`pi-agent-core` 只有 **1,440 行**，而我们的 `my_agent_core` 有 **7,713 行**（5.4 倍）。

**原因分析**：

| pi 的拆法 | 我们的拆法 |
| --- | --- |
| `pi-agent-core` 只放最抽象的循环协议 | 把 session / context / memory / skills / subagents / extensions / plugins 全放进 core |
| 具体实现都在 `pi-coding-agent/core/` | 产品层只放工具和 RPC |

**这不是优劣问题，是分层哲学不同**：
pi 把「框架」和「产品」切开（core 极小），我们把「所有可复用模块」都放在 core 里。

---

## 2. 核心模块行数对照

### 2.1 Agent 循环

| 模块 | pi | 本项目 | 差异说明 |
| --- | --- | --- | --- |
| 主循环 | `agent-session.js` **3,653 行** | `loop.py` **775 行** + `agent.py` 559 行 | pi 把会话、循环、事件、工具调度**全塞在一个类里**；我们拆成纯函数生成器 + 装配类 |

**这是最本质的架构差异**：

| 维度 | pi | 本项目 |
| --- | --- | --- |
| 循环形态 | `AgentSession` 类方法 | `async def run_agent_loop(...) -> AsyncIterator[Event]` |
| 状态持有 | 类实例属性 | 参数传入，调用方持有 |
| 副作用 | 类内部直接调用 | 只 yield 事件 |
| 可测试性 | 需 mock 整个类 | 纯函数，直接消费事件流 |

**我们的 `loop.py` docstring 自己承认了来源**：

```python
# src/my_agent_core/loop.py:540
"""对标 Tau 的极简纯函数异步微内核，主状态机约 110 行。"""
```

**注意：这里写的是「对标 Tau」，不是 pi**。
说明循环形态借鉴的是 **Tau**（另一个项目），而不是 pi 的 `AgentSession` 类。

### 2.2 上下文压缩

| 模块 | pi | 本项目 | 比例 |
| --- | --- | --- | --- |
| 压缩总计 | **1,162 行**（3 文件） | **679 行**（`context.py`） | 58% |
| └ 主压缩 | `compaction.js` 748 | 含在上面 | — |
| └ 分支摘要 | `branch-summarization.js` 261 | ❌ 未单独实现 | — |
| └ 工具函数 | `utils.js` 147 | 含在上面 | — |

**关键参数对比**（来自 pi 官方 `docs/compaction.md`）：

| 参数 | pi 默认 | 本项目 | 评价 |
| --- | --- | --- | --- |
| 触发条件 | `contextTokens > contextWindow - reserveTokens` | `tokens > 80% × budget` | **pi 用固定预留，我们用百分比** |
| reserve | **16,384 tokens** | 20% budget | pi 的口径与窗口无关，更稳 |
| keepRecent | **20,000 tokens** | ❌ 无此概念 | 我们用 `_find_cut` 找 user 边界 |
| 切点计算 | 从后往前累加到 keepRecent | 按 user 消息边界 | pi 更平滑，我们更语义化 |
| 分支摘要 | ✅ 独立机制 | ❌ 无 | **我们的缺口** |

**pi 文档里的两个细节我们没有**：

1. **「Summarization requests disable prompt-cache writes」**
   （摘要请求禁用 cache 写入，因为一次性 prompt 不会被复用）
   → 这是一个精细的成本优化，我们的 `_call_summarizer` 未做此处理。

2. **多触发点**：pi 在三个地方检查阈值——
   - 工具执行完、追加结果后（`prepareNextTurn`）
   - 新用户 prompt 前
   - 底层 run 结束后的「最终溢出恢复」
   → 我们只在 `prepare()` 一处检查。

### 2.3 会话管理

| 模块 | pi | 本项目 | 比例 |
| --- | --- | --- | --- |
| 会话管理 | `session-manager.js` **1,541 行** | `session/` 目录（session.py 635 + entries + tree + jsonl + store + storage + memory + assets） | 约 60% |

**结构对照**：

| 能力 | pi | 本项目 |
| --- | --- | --- |
| 存储格式 | JSONL | ✅ JSONL |
| 树结构 | Entry 有 ID + parent | ✅ 同构 |
| 分支切换 | 移动 currentEntryId | ✅ `rewind` |
| Fork | 复制到新文件 | ✅ `fork` |
| Entry 类型 | 见 `types.ts` | ✅ 11 种，**带 camelCase 别名** |

**11 种 Entry 类型是我们对照 pi 后补的**（见 `PI_ALIGNMENT_AUDIT.md`）：
`session` / `message` / `model_change` / `thinking_level_change` / `compaction` /
`branch_summary` / `label` / `leaf` / `custom` / `custom_message` / `session_info`

**每个 type 都有 camelCase 别名**：
```python
type: Literal["session_info", "sessionInfo"] = "session_info"
```
这是为了**文件级互操作**——同一个会话文件 Python 版和 pi 都能读。

### 2.4 工具实现

| 工具 | pi 行数 | 本项目行数 | 比例 | 说明 |
| --- | --- | --- | --- | --- |
| `read` | 185 | 102 | 55% | |
| `write` | 60 | 54 | 90% | |
| `edit` | 144 | 201 | 140% | **我们更多**（含 JSON 字符串容错） |
| `bash` | 332 | 304 | 92% | |
| `grep` | 253 | 150 | 59% | |
| `find` | 250 | 89 | 36% | 我们更薄 |
| `ls` | 125 | 81 | 65% | |
| **`powershell`** | 38 | ❌ **无** | 0% | **技术栈差异**（Python 用 bash 即可） |
| `file-mutation-queue` | 51 | 内嵌在 registry | — | 我们的单文件排队锁 |
| `output-accumulator` | 213 | 内嵌 | — | 我们无独立实现 |
| **合计（7 个共有工具）** | 1,349 | 981 | **73%** | |

**结论**：7 个共有工具的代码量是我们的 **1.37 倍**。
这说明**工具层是参考实现后重写的**，不是逐行翻译——
逐行翻译的话行数比例应该接近 1:1。

### 2.5 其它核心模块

| 模块 | pi | 本项目 | 说明 |
| --- | --- | --- | --- |
| `skills.js` | 403 | `skills.py` 约 120 | pi 含更多资源发现逻辑 |
| `output-guard.js` | 88 | 内嵌 | 输出守卫 |
| `nested-tool-calls.js` | 178 | ❌ 无 | **pi 支持工具内嵌套调用** |
| `system-prompt.js` | 150 | `agent.py` 内 | |
| `event-bus.js` | **24** | `events.py` 约 200 | pi 的事件总线极薄（只是类型定义），事件类型在别处 |
| `telemetry` | ✅ | ❌ | 遥测 |
| `bug-report` | ✅ | ❌ | 错误上报 |
| `package-manager` | 2,337 | ❌ | pi 包管理器（npm 生态） |
| `radius` | ✅ | ❌ | — |
| `trust-manager` | ✅ | 部分（`permissions.py`） | 项目信任 |

---

## 3. 借鉴 vs 原创：三分类评估

### 3.1 分类标准

| 类别 | 定义 |
| --- | --- |
| **A. 结构借鉴** | pi 有对应实现，我们的架构同构（Entry 树、JSONL、steering/follow-up 时机） |
| **B. 重写** | 功能对应，但实现语言/结构不同（工具函数、压缩算法） |
| **C. 原创** | pi 无对应，或形态完全不同 |

### 3.2 逐模块归类

| 模块 | 类别 | 依据 |
| --- | --- | --- |
| ReAct 循环形态 | **C（原创/对标 Tau）** | pi 是 `AgentSession` 类，我们是纯函数生成器；docstring 明确写「对标 Tau」 |
| 两层调度（内层 ReAct + 外层 Task） | **A** | pi 的 steering/follow-up 时机完全一致（官方文档 `how-pi-works.md` 有描述） |
| 14 类事件 | **A** | 事件名与 pi 对齐（`PI_ALIGNMENT_AUDIT.md` 记录了逐项对照） |
| 5 个生命周期钩子 | **B** | pi 的 extension 有 hooks，我们抽象成 5 个专职拦截点 |
| 上下文压缩三层管线 | **B** | pi 是 compaction（748 行），我们是三层门控（Append-Only → 免费层 → 摘要） |
| 「免费层」（工具输出落盘） | **C（原创）** | **pi 的 `compaction.md` 无此层**，我们排除了「滚动裁切」改为落盘 |
| 会话树 + JSONL | **A** | 结构完全同构 |
| 11 种 Entry 类型 | **A** | 逐项对照后补齐（`PI_ALIGNMENT_AUDIT.md` 记录） |
| 工具实现（7 个） | **B** | 功能对应，行数比 73%，重写 |
| 工具并发（批级门控） | **C（原创）** | pi 用 `file-mutation-queue`（文件级），我们用**批级**（一个写就全串行） |
| `is_parallel_safe` 默认 False | **C（原创）** | fail-safe 默认，pi 无此概念 |
| 长期记忆（双层时间） | **C（原创）** | **pi 无长期记忆模块** |
| 子代理 | **B** | pi 有 subagent，我们重构 |
| Extension | **A** | 5 大决策点对照 pi |
| Plugin（Claude Code 风格） | **C（原创）** | **pi 无此模块** |
| MCP 客户端 | **B** | pi 有独立 `pi-mcp` 包，我们内嵌 |
| 转录本自愈（三阶段） | **C（原创）** | pi 有类似逻辑，但我们的三阶段算法是独立设计 |
| 权限门禁（fail-closed） | **C（原创）** | pi 用 OS 权限，我们实现显式门禁 |
| TUI | **A（复用）** | 直接用 `pi-tui` 的组件模式（TypeScript） |

### 3.3 量化结论

| 类别 | 模块数 | 占比 | 说明 |
| --- | --- | --- | --- |
| **A. 结构借鉴** | 6 / 19 | **32%** | 循环时机、事件、会话树、Entry 类型、Extension、TUI |
| **B. 重写** | 7 / 19 | **37%** | 压缩、工具、子代理、MCP、钩子、技能 |
| **C. 原创** | 6 / 19 | **32%** | 免费层、批级并发、fail-safe 默认、长期记忆、Plugin、权限门禁、转录本自愈 |

> 注：模块数 19，其中「转录本自愈」和「权限门禁」都算原创，故 C 实际为 7 项。
> 按代码量加权：C 类约 **2,500 行 / 18,170 = 14%**，
> 但**它们是最能体现"想清楚了"的部分**（都是踩坑后设计的）。

---

## 4. 我们改了什么：五个具体差异

### 4.1 循环形态：从「类」到「纯函数生成器」

| | pi | 本项目 |
| --- | --- | --- |
| 代码 | `agent-session.js` 3,653 行 | `loop.py` 775 行 |
| 形态 | `class AgentSession { async run() }` | `async def run_agent_loop(...) -> AsyncIterator[Event]` |

**为什么改**：类持有状态 → 难以单测、难以在 CLI/TUI/RPC 间复用。
纯函数 + 事件流让同一个内核驱动三种界面。

**代价**：调用方必须实现事件消费循环。我们为此加了 `agent.py`（559 行装配层）。

### 4.2 工具并发：从「文件级锁」到「批级门控」

| | pi | 本项目 |
| --- | --- | --- |
| 机制 | `file-mutation-queue.js`（51 行）：**同文件的写排队** | `registry.py`：**只要有一个写，整批串行** |
| 粒度 | 文件级 | 批级 |
| 并行度 | 更高（不同文件的写可并发） | 更低 |
| 正确性保证 | 需正确识别「同文件」 | **永远正确**（无需识别） |

**为什么改**：

pi 的方案要判断两个工具是否操作同一个文件。但 `bash` 命令的副作用无法静态分析
（`bash: echo hi > a.txt` 算不算写 `a.txt`？），所以 pi 的 `file-mutation-queue`
对 bash 是失效的。

我们的批级门控**不依赖任何静态分析**——只要批里有非并行安全工具，整批串行。
牺牲并行度换正确性。

**这是本项目的核心设计论证之一**（见 `04_MODULES.md` 模块 2）。

### 4.3 上下文压缩：加了「免费层」

| 层 | pi | 本项目 |
| --- | --- | --- |
| 1 | — | **Append-Only（<80% 不改动）** |
| 2 | — | **工具输出落盘（零 LLM 调用）** |
| 3 | Compaction（摘要） | 摘要 |

**pi 只有第 3 层**。我们的第 1、2 层是针对 **prefix cache** 的优化：

- 第 1 层：不动历史 → cache 100% 命中
- 第 2 层：只改「超大工具输出」这一处 → 局部失效，其余仍命中

**代价**：多了落盘文件管理（`.agent/tool-results/`）。

### 4.4 长期记忆：pi 完全没有

| | pi | 本项目 |
| --- | --- | --- |
| 跨会话记忆 | ❌ 无 | ✅ `memory.py` + `memory_index.py`（约 600 行） |
| 冲突处理 | — | **双层时间建模**（`valid_from`/`valid_to` + history 链） |
| 检索 | — | BM25 / SQLite FTS5 双引擎 |
| 存储 | — | Markdown（人可读、可 git） |

**这是纯原创模块**，对应学术上的 Mem0 / A-MEM。

### 4.5 会话文件：加了 camelCase 别名

```python
type: Literal["session_info", "sessionInfo"] = "session_info"
```

**目的**：让 Python 版和 pi 能读同一个会话文件。
这是**主动的互操作设计**，不是照抄。

---

## 5. 我们缺什么（pi 有我们没有）

| 缺失 | pi 的规模 | 影响 | 优先级 |
| --- | --- | --- | --- |
| `powershell` 工具 | 38 行 | Windows 体验 | 低（bash 够用） |
| **`nested-tool-calls`** | 178 行 | 工具内嵌套调用 | **中** |
| `branch-summarization` | 261 行 | 切换分支时保留上下文 | **中** |
| `output-accumulator` | 213 行 | 长输出累积管理 | 低 |
| `telemetry` / `bug-report` | — | 可观测性 | 低 |
| `package-manager` | 2,337 行 | 扩展分发 | 低（我们不做生态） |
| `trust-manager` | — | 项目级信任 | 中（我们有简化版） |
| `codemode` 包 | 2,228 行 | 代码模式 | 低 |

**最值得补的是 `branch-summarization`**：
pi 在 `/tree` 切换分支时会生成「分支摘要」，让模型知道"另一条分支上发生了什么"。
我们的 `fork` 只切换指针，模型不知道被放弃的分支做过什么。

---

## 6. 一句话总结

| 问题 | 答案 |
| --- | --- |
| **代码规模** | 我们 18,170 行，pi 全家桶 108,459 行（**16.8%**） |
| **结构借鉴** | 32% 的模块（循环时机、事件、会话树、Entry 类型、Extension、TUI） |
| **重写** | 37%（压缩、工具、子代理、MCP、钩子、技能） |
| **原创** | 32%（免费层、批级并发、fail-safe 默认、长期记忆、Plugin、权限门禁、转录本自愈） |
| **最大差异** | 循环形态（纯函数 vs 类）；工具并发（批级 vs 文件级）；压缩（三层 vs 一层） |
| **最强原创** | 长期记忆（pi 完全没有）；批级并发门控（有完整论证） |
| **最大缺口** | `branch-summarization`、`nested-tool-calls` |

---

## 7. 面试话术

> "pi 是我最主要的参考实现。我把它的 6 个 npm 包全下载解包统计过：
> 全家桶 10.8 万行 TypeScript，我重写了 1.8 万行 Python，约 17%。
>
> 但结构上我只借鉴了三分之一——循环时机、事件类型、会话树这些**协议性**的东西
> 必须对齐才能互操作，我甚至给每个 Entry 类型加了 camelCase 别名，
> 让两边的会话文件能互相读。
>
> 另外三分之二是改的或原创的。最典型的是工具并发：
> pi 用文件级队列锁，但那对 bash 工具失效——你没法静态分析
> `echo hi > a.txt` 到底写了什么。我改成批级门控，只要有写操作整批串行，
> 不依赖任何静态分析，牺牲并行度换正确性。
>
> 长期记忆是 pi 完全没有的模块，我做了双层时间建模，
> 能回答『你之前用什么版本』这种问题。"

---

## 附录：可复现的统计命令

```bash
# 1. 下载全部 pi 包
npm pack @earendil-works/pi-coding-agent
npm pack @earendil-works/pi-ai
npm pack @earendil-works/pi-tui
npm pack @earendil-works/pi-agent-core
npm pack @earendil-works/pi-codemode
npm pack @earendil-works/pi-mcp

# 2. 解包
for f in *.tgz; do mkdir -p "x_${f%.tgz}"; tar -xzf "$f" -C "x_${f%.tgz}"; done

# 3. 统计每个包
for d in x_*/; do
  echo -n "$d "
  find "$d" -name "*.js" -exec cat {} + | wc -l
done

# 4. 统计本项目
find src -name "*.py" | xargs wc -l | tail -1
```
