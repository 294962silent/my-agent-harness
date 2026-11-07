# 设计溯源文档集（INDEX）

> 本文件是 **2026-11-07 设计溯源工程**的索引。
> 与 [README.md](README.md)（架构规范）互补：README 讲「怎么实现」，这里讲「为什么这么设计」。

---

## 文档导航

| 编号 | 文档 | 内容 | 适合谁看 |
| --- | --- | --- | --- |
| — | [PLAN.md](PLAN.md) | 六阶段工作规划 | 整理思路 |
| **00** | [**00_FACTS.md**](00_FACTS.md) | **事实基线**：所有数字的出处 | 核实任何数字 |
| **01** | [**01_PI_DIFF.md**](01_PI_DIFF.md) | **与 pi 的量化对照**：借鉴 32% / 重写 37% / 原创 32% | **原创性** |
| **02** | [**02_LANDSCAPE.md**](02_LANDSCAPE.md) | **工业界 10 产品横向对比** | **判断力** |
| **03** | [**03_ACADEMIC.md**](03_ACADEMIC.md) | **学术前沿对标**（52 篇论文） | **研究能力** |
| **04** | [**04_MODULES.md**](04_MODULES.md) | **逐模块设计论证**（10 个模块） | **核心技术文档** |
| **05** | [**05_VALIDATION.md**](05_VALIDATION.md) | **验证闭环与缺陷档案**（14 项 bug） | **工程严谨性** |
| **06** | [**06_INTERROGATION.md**](06_INTERROGATION.md) | **简历盘问 + 创新点** | 面试准备 |
| **07** | [**07_AUTHOR_BLOG.md**](07_AUTHOR_BLOG.md) | **原作者博客核对**（10 篇，含数字争议澄清） | **溯源权威性** |
| **08** | [08_EXPERIMENT_DESIGN.md](08_EXPERIMENT_DESIGN.md) | 28 个实验的设计（对照/指标/复现） | 实验方法论 |
| **09** | [**09_EXPERIMENTS.md**](09_EXPERIMENTS.md) | **23 个实验的实测报告** | **量化证据** |

原始调研材料：
- [`raw/landscape_raw.md`](raw/landscape_raw.md)（1213 行，10 产品）
- [`raw/academic_raw.md`](raw/academic_raw.md)（329 行，52 篇论文）

---

## 三分钟速览

### 项目规模（实测）

| 项 | 数值 |
| --- | --- |
| 源码 | **18,170 行** / 89 文件（core 7,713 + llm 3,047 + coding 7,410） |
| 测试 | **956 passed in 40.27s**，**覆盖率 85%** |
| 提交 | **737 次**，2026-08-01 → 11-04 |
| 缺陷档案 | 14 项，已修 11 项 |

### 与 pi 的关系

| 类别 | 占比 |
| --- | --- |
| 结构借鉴 | **32%**（循环时机、事件类型、会话树、Entry 类型、Extension、TUI） |
| 重写 | **37%**（压缩、工具、子代理、MCP、钩子、技能） |
| **原创** | **32%**（免费层、批级并发、fail-safe 默认、**长期记忆**、Plugin、权限门禁、转录本自愈） |

**代码规模**：18,170 / 108,459 = **16.8%**

### 最强的一组数字

| 指标 | 我们 | 官方 pi |
| --- | --- | --- |
| Terminal-Bench 2.1 | **71/89 (79.8%)** | 72/89 (80.9%) |
| Token | **41.7M** | 122.3M（**省 66%**） |
| 费用 | **$4.62** | $18.84（**省 75%**） |

逐任务 **6 胜 7 负**交叉分布 → 独立实现，非复制。

---

## 五个核心设计论点

### 1. 循环是纯函数生成器，不是类

```python
async def run_agent_loop(...) -> AsyncIterator[Event]:
    """对标 Tau 的极简纯函数异步微内核"""
```

**为什么**：类持有状态 → 难单测、难在三界面（CLI/TUI/RPC）复用。
代价是调用方要实现事件消费循环（我们加了 `agent.py` 559 行装配层）。

### 2. 工具并发是「批级门控」，不是 DAG

```python
# 只要有一个非并行安全工具 → 整批串行
has_sequential = any(t is None or not t.is_parallel_safe for tc in tool_calls)
```

**为什么不做 DAG**：`bash` 的副作用无法静态分析（`echo hi > a.txt` 写了什么？）。
批级门控零静态分析依赖，**永远不需要推理依赖**。

**业界坐标**：Codex 用 RwLock 读并写斥、dsh 用 `isConcurrencySafe(args)` + 上限 10、
Kimi 用子代理爬坡、Claude Code 全串行。**我们是批级，最保守但可证**。

### 3. 压缩三层，核心是「能不压就不压」

| 层 | 触发 | LLM 调用 |
| --- | --- | --- |
| 0 | <80% 阈值 | 0（完全不动） |
| 1 | 超阈值 + 超大工具输出 | **0**（落盘） |
| 2 | 还超 | 1（摘要） |

**核心矛盾**：压缩省 token，但破坏 prefix cache 反而更贵。
**调研发现**：10 个产品里只有 Claude Code 和 dsh 把 prefix cache 写成显式设计目标。

### 4. 长期记忆是全行业空白

| 产品 | 跨会话记忆 |
| --- | --- |
| pi / Claude Code / Codex / Kimi / Trae | ❌ 无 |
| WorkBuddy | 有「记忆」但**完全未公开** |
| **本项目** | ✅ 双层时间建模（`valid_from`/`valid_to` + history 链） |

### 5. 最危险的 bug 是静默失效

**P0-1 权限门禁**：单测全绿，用户点「拒绝」文件还是被写了。
因为单测测了「门禁函数」，没测「门禁有没有接进主链路」。

**修复**：fail-closed（无回调一律 block）+ **端到端验证**（拒绝后文件真的不存在）。

---

## 诚实的缺口清单

| 缺口 | 严重度 | 说明 |
| --- | --- | --- |
| **KV cache 命中率口径不明** | 🔴 | 66.3% 无法从现有数据反推 |
| **LongMemEval 无原始日志** | 🔴 | -88.6% 需重跑 |
| ~~测试覆盖率未知~~ | ✅ **已实测 85%** | 9697 行中 1449 未覆盖 |
| **只跑过单次评测** | 🟠 | 无 pass^k 数据 |
| **子代理收益归因未做** | 🟠 | 学术空白，可做原创 |
| 缺 413 反应式压缩 | 🟠 | Claude Code / pi 都有 |
| 缺 branch-summarization | 🟠 | pi 有（261 行） |
| 无审计链 | 🟠 | AEGIS / PRISM 主张 |
| 「只读/非安全」二分不表达组合风险 | 🟠 | SCR-Bench 已证明 |
| Windows 退出留孤儿进程 | 🟡 | P1-4 未修 |

---

## 学术空白（可做原创的机会）

1. **工具并发控制的副作用语义理论** — LLMCompiler(ICML'24) 后无继任者
2. **子代理委派的定量收益归因** — **至今无受控实验**
3. **Harness 级 scaling law** — EFC 是孤例
4. **树状会话作为规划原语** — 我们的 fork/rewind 正好是缺的那一半

---

## 复现命令

```bash
# 全部测试
.venv/Scripts/python.exe -m pytest tests/ -p no:cacheprovider   # 956 passed

# 运行时自愈 A/B（40 例故障注入）
uv run python experiments/runtime_ab/run_ab.py                   # 20% → 100%

# 代码规模
find src -name "*.py" | xargs wc -l | tail -1                    # 18,170

# pi 包对照
npm pack @earendil-works/pi-coding-agent && tar -xzf *.tgz
```
