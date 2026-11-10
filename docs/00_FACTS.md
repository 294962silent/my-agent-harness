# 事实基线（Facts Baseline）

> 本文档记录**经过实测验证**的项目事实。每个数字都标注出处（命令或文件）。
> 目的：后续所有设计文档引用数字时，以本文档为准，避免「README 说 X、实际是 Y」的问题。
>
> 采集时间：2026-11-07

---

## 1. 代码规模

### 1.1 源码

| 模块 | 文件数 | 代码行数 |
| --- | --- | --- |
| `src/my_agent_core` | 38 | 7,713 |
| `src/my_agent_llm` | 18 | 3,047 |
| `src/my_coding_agent` | 33 | 7,410 |
| **合计** | **89** | **18,170** |

**出处**：`find src/{module} -name '*.py' | wc -l` 与 `xargs wc -l`

### 1.2 最大的 10 个文件

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `src/my_agent_core/loop.py` | 775 | ReAct 微内核 |
| `src/my_coding_agent/rpc_server.py` | 732 | RPC 服务端 |
| `src/my_coding_agent/model_catalog.py` | 707 | 模型目录 |
| `src/my_coding_agent/session_ops.py` | 688 | 会话操作 |
| `src/my_agent_core/context.py` | 679 | 上下文压缩管线 |
| `src/my_agent_core/session/session.py` | 635 | 会话持久化 |
| `src/my_agent_core/agent.py` | 559 | Agent 装配 |
| `src/my_agent_llm/providers/antigravity.py` | 503 | Antigravity provider |
| `src/my_agent_llm/providers/openai.py` | 465 | OpenAI provider |
| `src/my_coding_agent/rpc/session_handlers.py` | 461 | 会话 RPC 处理 |

**出处**：`find src -name "*.py" | xargs wc -l | sort -rn | head -15`

---

## 2. 测试

### 2.1 实测结果

```
956 passed in 40.27s
```

**命令**：
```bash
.venv/Scripts/python.exe -m pytest tests/ -p no:cacheprovider
```

**说明**：README 里写的「956 项测试」是**真实的**，与实测一致。

### 2.2 测试分布

| 目录 | 测试文件数 |
| --- | --- |
| `tests/core` | 41 |
| `tests/coding` | 43 |
| `tests/llm` | 17 |
| `tests/eval` | 4 |
| `tests/experiments` | 2 |
| **合计** | **107** |

**测试函数总数**：910（`grep -rhoE "^\s*(async )?def test_" tests/ | wc -l`）

> 注意：测试函数 910 个，但 pytest 报告 956 passed。
> 差异原因：部分测试通过 `parametrize` 展开成多个用例。

---

## 3. 开发历史

| 指标 | 数值 |
| --- | --- |
| 提交总数 | **737** |
| 起始日期 | 2026-08-01 |
| 最新提交 | 2026-11-04 |
| 跨度 | **约 3 个月** |

**出处**：`git rev-list --count HEAD`、`git log --reverse --format="%ad" --date=short | head -1`

---

## 4. 已有文档资产

| 文档 | 行数 | 内容 | 可信度 |
| --- | --- | --- | --- |
| `REFERENCES.md` | 457 | 14 个模块的借鉴溯源（Pi/Tau/Hermes/OpenHands/Learn-Claude-Code/pig-mono） | 需核对 |
| `docs/PI_ALIGNMENT_AUDIT.md` | 152 | 与 pi 官方包逐项对照，6+2 个缺陷 | 高（有具体代码位置） |
| `PROJECT_REVIEW.md` | 216 | **13 个缺陷的完整档案 + 修复状态** | **高（有实测验证）** |
| `PROGRESS.md` | 1,096 | 分阶段开发记录 | 高（有时间戳） |
| `RESUME_PROJECT.md` | ~190 | 简历版本 | 需核对数字 |

---

## 5. 缺陷档案（来自 PROJECT_REVIEW.md 第九节）

### 5.1 修复状态总表

| 编号 | 问题 | 状态 | 验证方式 |
| --- | --- | --- | --- |
| P0-1 | 权限门禁空转 | ✅ 已修 | 端到端实测：批准→落盘；拒绝→`Tool 'write' blocked` |
| P0-2 | API Key 写进 `os.environ` | ✅ 已修 | 删除该行 + 子进程 env 白名单（`env_filter.py`） |
| P0-3 | 评测 4 条数据来源存疑 | ⚠️ 未动 | 需重跑并补原始日志 |
| P1-1 | 流式 usage 恒为 0 | ✅ 已修 | `stream_options={"include_usage": True}` + 降级重试 |
| P1-2 | 每条消息全量重写会话文件 | ✅ 已修 | 增量追加（format v2 + 指针） |
| P1-3 | sync 工具超时后线程仍在跑 | ✅ 已修 | 写类工具标记 `uncancellable` |
| P1-4 | Windows 退出留孤儿进程 | ❌ 未修 | 遗留 |
| P1-5 | 工具路径无 workspace 边界 | ✅ 已修 | `PathEscapeError` + 15 个越界用例 |
| P1-6 | TUI 无 error/exit 监听 | ❌ 未修 | 遗留 |
| P1-7 | TUI 测试依赖未入库的 dist | ✅ 已修 | `npm test` 改为先 build |
| P2 | 无 CI / 无 lint 门禁 | ✅ 已修 | `.github/workflows/ci.yml`（ubuntu+windows × 3.11/3.13） |
| P2 | wheel 不含 TUI 资产 | ✅ 已修 | wheel 91 → 167 entries |
| P2 | README 数字失真 | ✅ 已修 | 814→828、96.7%→66.3%、删「超越原厂」 |
| P2 | 压缩缓存用裸下标定位历史 | ✅ 已修 | 改用消息 key + 整段尾部校验 |

**修复率**：11/14 已修（79%），3 项遗留（P0-3 需重跑实验、P1-4、P1-6）。

### 5.2 测试基线的变化

| 时点 | Python 测试 | TUI 测试 |
| --- | --- | --- |
| 评审前 | 828 | 80 |
| 评审后 | **937** | **97 passed / 1 skipped** |
| 当前（2026-11-07 实测） | **956** | 未测 |

### 5.3 新增实验

`experiments/runtime_ab/` —— 40 例故障注入 A/B：
- 会话可继续率：**0% → 100%**
- 对照组：零误删

---

## 6. 待核实项（已知的不确定）

| 项 | 问题 | 核计划 |
| --- | --- | --- |
| KV cache 命中率 96.7% vs 66.3% | REVIEW 说 README 曾写 96.7%，实际 66.3% | 需重跑实验确认当前值 |
| LongMemEval 数字 | 上下文占用 -88.6%，准确率 71.9%→69.8% | 需找到原始日志 |
| Terminal-Bench 71/89 vs 72/89 | 与 pi 原厂对比 | 需找 Harbor 原始日志（P0-3 就是这条） |
| TUI 测试当前数 | 未测 | 需 `npm test` |

---

## 7. 本阶段新增文档计划

| 文件 | 状态 |
| --- | --- |
| `docs/PLAN.md` | ✅ 已写 |
| `docs/00_FACTS.md` | ✅ 本文件 |
| `docs/01_PI_DIFF.md` | ⏳ 等子代理 |
| `docs/02_LANDSCAPE.md` | ⏳ 等子代理 |
| `docs/03_ACADEMIC.md` | ⏳ 待启动 |
| `docs/04_MODULES.md` | ⏳ 待写 |
| `docs/05_VALIDATION.md` | ⏳ 待写 |
| `docs/06_INTERROGATION.md` | ⏳ 待启动 |
| `docs/07_INNOVATION.md` | ⏳ 待启动 |

---

## 8. Terminal-Bench 2.1 完整实测数据（2026-11-07 复核）

**数据源**：`my-pi-eval/results/{my_agent_harness,official_pi}_progress.json`（各 89 条任务记录）

### 8.1 总体对比

| 指标 | MyPi（本实现） | Official Pi | 差异 |
| --- | --- | --- | --- |
| 通过 / 总数 | **71 / 89 (79.8%)** | **72 / 89 (80.9%)** | -1 题 |
| Token 总量 | **41,737,271** | 122,295,809 | **省 65.9%** |
| 费用 | **$4.62** | $18.84 | **省 75.5%** |
| 耗时 | 22.0 h | 16.9 h | 慢 30.2% |
| cache_read 记录 | 82,273,152 | 0 | — |

### 8.2 逐任务差异（不是简单复刻的证据）

**我们通过、官方失败（6 个）**：
`cancel-async-tasks`、`git-multibranch`、`make-mips-interpreter`、
`model-extraction-relu-logits`、`polyglot-c-py`、（另 1 个）

**官方通过、我们失败（7 个）**：
`gcode-to-text`、`large-scale-text-editing`、`polyglot-rust-c`、
`raman-fitting`、`train-fasttext`、（另 2 个）

**结论**：存在双向差异，说明两边是**独立实现**，不是数据复制。

### 8.3 一处数据自相矛盾（已核实）

`official_pi_progress.json` 文件头记录 `passed_count=74`，
但逐条 `tasks` 统计是 **72 PASSED / 17 FAILED**。

**判定**：文件头的 74 是过期值，逐条统计的 72 为准。
**简历写的 72/89 是正确的。**

### 8.4 cache 指标的正确读法

`cache_read_tokens` 累计 82,273,152，大于 `tokens` 41,737,271（197%）。

**原因**：多轮对话中同一段前缀被重复读取，
`cache_read` 是「累计读取量」，不是「唯一 token 量」。

**要算命中率，需用公式**：
```
命中率 = cache_read / (cache_read + 未命中输入 token)
```
而不仅是 `cache_read / tokens`。

> ⚠️ 简历若写「KV cache 命中率 66.3%」，需说明分母口径。
> 当前无法从该文件直接反推，需重跑实验或查原始日志。

---

## 9. 测试覆盖率（2026-11-07 实测）

```
$ uv run pytest tests/ -q --cov=src --cov-report=term
TOTAL    9697    1449    85%
```

| 指标 | 数值 |
| --- | --- |
| 语句总数 | 9,697 |
| 未覆盖 | 1,449 |
| **覆盖率** | **85%** |

**命令**：`uv run pytest tests/ -q --cov=src --cov-report=term -p no:cacheprovider`

覆盖率较高的模块（抽样）：
- `my_coding_agent/tools/__init__.py` 100%
- `my_coding_agent/settings.py` 97%
- `my_coding_agent/tools/read.py` 94%
- `my_agent_core/rpc/session_handlers.py` 93%

---

## 10. 作者博客核对的关键结论（详见 07_AUTHOR_BLOG.md）

### 10.1 解开「96.7% vs 66.3%」的矛盾

**这两个数字量的不是同一件事**：

| 指标 | 含义 | 数值 | 出处 |
| --- | --- | --- | --- |
| **前缀不变性** | 压缩后 prefix **字节**未变的比例 | **96.7%** | 作者博客 |
| **KV cache 命中率** | 实际命中缓存的 **token** 比例 | **66.3%** | 外部评现实测 |

**两者可以同时成立**：prefix 没变，但 provider 端未必命中。

### 10.2 自研胜率的分母问题

作者博客写「**71 胜 / 85 题（83.53%）**」，
但同一篇文章的失败分析写「**全量 89 题中 71 胜后的剩余 18 题**」
（71 + 18 = 89）。

**判定**：实际跑的是 89 题，83.53% 的分母 85 是剔除了 4 道题。
**我们采用 71/89 = 79.8%，这是正确的。**

### 10.3 历史最低点 8.1%

> 「评测过程经历了从最初受制于环境暗坑的 **8.1% 低谷**，
> 一路通过底层环境工程攻坚，逆袭至 72 胜 / 80.90%」

**这是真实的踩坑-修复距离**，比只报最终成绩更有说服力。

### 10.4 博客记录的环境难题（可用于面试）

| 问题 | 影响 |
| --- | --- |
| Docker 网络崩溃 | 早期题目全挂 |
| 编码错乱 | — |
| astral.sh 被 GFW 拦截 | `gpt2-codegolf`（1614 万 tokens）等大题无法跑 |
| Windows WSL2 无 `/dev/kvm` | 3 道虚拟机题纯 CPU 模拟，慢数十倍 |
| 3000 万 Token 缓存雪崩 | 触发缓存架构重构 |

### 10.5 博客 vs 代码的两处差异

| 项 | 博客 | 代码实测 | 判定 |
| --- | --- | --- | --- |
| `run_agent_loop` 行数 | 约 110 行 | **246 行** | 博客是估计值，以 246 为准 |
| 工具流水线阶段 | 7 阶段 | docstring 写 5 阶段 | 划分粒度不同，都对 |

---

## 11. KV cache 真实 API 实测（2026-11-07）

**这把「96.7% vs 66.3%」的口径问题彻底解决了。**

### 11.1 provider 字段含义（实测标定）

```
prompt_tokens = 3490
tokens.input  = 1314    ← 未命中
tokens.cache_read = 2176 ← 命中
1314 + 2176 = 3490  ✅
```

**正确公式**：`命中率 = cache_read / prompt_tokens`

### 11.2 三组实测数据

| 实验 | 场景 | 结果 |
| --- | --- | --- |
| **API-1** | 冷启动 vs 重复 | **0% → 90.1%** |
| **API-2** | prefix 篡改中段 | 97.0% → **83.1%**（丢 1088 token） |
| **API-3** | Append-Only vs 滑动窗口 | **53.5% vs 17.9%**（差 35.6pp） |

### 11.3 缓存最小阈值（精确测量）

| prompt_tokens | 命中 |
| --- | --- |
| 1,082 | ❌ |
| **1,093** | ✅ |

**阈值锁定在 1,082–1,093 token（推定 1,088 ≈ 1024 + 64）**

**`cache_read` 恒为 1,088** —— 说明缓存按固定块计算，不是按实际长度。

**最实用的结论**：**短对话（<1,088 token）做 cache 优化毫无意义。**

### 11.4 三个设计洞察

1. **缓存键是内容前缀，不是 session** — 换 session 仍命中
2. **最小长度阈值 ~1000-2000 token** — 617 token 时 0%，2416 token 时 90%
3. **丢弃最旧消息 = 全缓存失效** — 滑动窗口从第 4 轮起归零

### 11.5 三个数字的关系（不再矛盾）

| 指标 | 数值 | 量的是什么 |
| --- | --- | --- |
| 前缀不变性 | 96.7% | prefix **字节**未变比例（设计侧） |
| 历史评审值 | 66.3% | token 命中（运行时，口径未记录） |
| **本次实测** | **53.5%** | 6 轮短对话的 token 命中（有完整方法） |

**结论**：**简历应使用 53.5% vs 17.9% 这组有完整方法的数字**，
不再使用口径不明的 66.3% 或误用的 96.7%。

详见 [`10_API_CACHE.md`](10_API_CACHE.md)
