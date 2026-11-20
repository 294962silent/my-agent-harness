# 五层上下文压缩与请求视图

- 定位：长任务上下文治理、请求投影、可恢复压缩快照
- 核心实现：`src/my_agent_core/context.py`、`src/my_agent_core/context_compression.py`
- 持久化：Session append-only JSONL
- 默认入口：`CodingAgent` 构造的 `ContextManager`

## 1. 目标与不变量

完整 Session 历史是事实来源。压缩只生成下一次模型请求的投影，不删除、不改写 canonical 消息。

实现遵守以下不变量：

1. `System` 消息和当前分支由 Session 管理。
2. Tool call 与对应 tool result 不拆开。
3. 未超预算时，消息顺序和内容保持确定性。
4. Snip、Microcompact、Context Collapse 均使用稳定 projection ID。
5. 相同历史重复执行 `prepare()` 必须得到相同视图。
6. Auto-Compact 只作为最后兜底，并继续使用原有摘要、Floor 与恢复护栏。
7. 投影计划写盘时不移动 Session `current_id`，也不更新 `compaction_floor`。

这些行为参考 Claude Code 可观察到的五层职责，但本项目没有 Claude Code 内部源码，不能宣称逐行复刻。

## 2. 固定执行顺序

```text
canonical Session messages
  -> 稳定 projection ID
  -> L1 Tool Result Budget
  -> L2 Snip
  -> L3 Microcompact
  -> L4 Context Collapse
  -> L5 Auto-Compact
  -> provider request view
```

每层完成后重新估算 token。L2-L5 仅在视图仍高于触发水位时继续。L1 在预算门控前执行，因为它只把可恢复的大工具结果替换为稳定预览。

## 3. 五层职责

### L1 Tool Result Budget

- 单条工具结果默认超过 `6000` 字符时写入 `ArtifactStore`。
- 同一轮全部工具结果默认超过 `24000` 字符时，按“最大结果优先、原位置优先”继续外置。
- 模型看到头尾预览和稳定 artifact 引用；Session 保留完整正文。
- `artifact_read`、`artifact_search` 等可信有界结果不会再次外置。
- 没有 `ArtifactStore` 时保留旧 `results_dir` 落盘兼容路径。

该层不依赖模型调用，不改变 provider 的工具调用结构。

### L2 Snip

Snip 只删除完整旧交互组：

- 保留 system；
- 保留首个任务/约束组；
- 保留当前请求和 recent tail；
- 不切割 assistant tool call 与其结果；
- 只选择当前分支上的完整组。

生产管线会为 Context Collapse 预留最新一个可折叠中间组。否则 Snip 会先删掉全部可删组，使 L4 在正常完整消息结构下不可达。纯 `select_snip_ids()` 默认仍可删除全部候选，调用方通过 `reserve_groups_for_collapse` 控制。

### L3 Microcompact

Microcompact 只清除旧工具结果的正文，保留 assistant tool call、tool result、`tool_call_id` 和顺序。

它只在显式 cache-invalidated epoch 中运行。失效来源包括：

- 模型或 provider 变化；
- 分支、Session 或 resume 恢复；
- Auto-Compact 摘要替换；
- provider 明确报告 cache miss。

provider 没有明确报告时，不使用时间猜测。DeepSeek 的 `prompt_cache_miss_tokens` 会进入 usage；OpenAI 兼容路径也保留该字段。

### L4 Context Collapse

Collapse 选择受保护任务头和 recent tail 之间的完整中间区段，只把该区段交给摘要模型。摘要保留：

- 决策与理由；
- 失败尝试；
- 证据与文件操作；
- 未解决问题；
- 后续动作。

摘要失败、超时或为空时保留原中间区段并进入 Auto-Compact。摘要成功时写入 projection-only 快照；原始中间消息仍保留在 Session。

### L5 Auto-Compact

Auto-Compact 保留结构化交接摘要：

- Goal
- Constraints & Preferences
- Progress
- Key Decisions
- Next Steps
- Critical Context

它继续记录覆盖消息身份、最近尾部、摘要模型与 usage，并更新 `compaction_floor`。恢复、分支切换和改写前缀时会重新校验覆盖身份。

## 4. 投影计划与恢复

新生成的 L1-L4 计划包含：

- version；
- source count 与 canonical SHA-256；
- Snip projection IDs；
- Microcompact projection IDs；
- Collapse 起止 ID 与摘要；
- 已应用层；
- cache epoch；
- 是否因预算触发。

计划作为 `CompactionEntry` 的 metadata 追加保存：

```json
{
  "entry_kind": "request_projection",
  "projection_only": true,
  "source_path_id": "<current-entry>",
  "branch_id": "<current-entry>",
  "canonical_source_hash": "...",
  "projection_plan": {}
}
```

读取规则：

1. 父节点、来源和分支必须仍在当前路径。
2. plan 结构、ID 数量、图层名和摘要长度受上限校验。
3. prepare 时再次核对 canonical 前缀哈希。
4. 哈希不匹配则丢弃计划，从当前历史重建。
5. `get_latest_compaction_cache()` 忽略 projection-only 条目。
6. Session state folding 忽略 projection-only 条目。

因此分支回退或恢复不会复用另一分支的删除/折叠计划。

## 5. 配置与兼容

`CompactionSettings` 默认值：

```text
enabled=true
reserveTokens=16384
keepRecentTokens=20000
toolResultMaxChars=6000
toolRoundMaxChars=24000
contextTriggerRatio=0.8
microcompactMinChars=200
```

`CodingAgent` 始终把产品配置转换为五层 `CompressionConfig`，并显式设置：

```python
five_layers_enabled=True
```

直接构造 `ContextManager()` 出于既有嵌入 API 兼容，默认仍为 Auto-Compact-only；调用方可显式传入 `CompressionConfig(five_layers_enabled=True)`。旧的 Auto-Compact、摘要缓存、Floor 和边界测试因此继续验证原契约。

## 6. 可观测性

`ContextCompacted` 保留原有三个字段，并增加有默认值：

```text
layers_applied
layer_metrics
cache_epoch
budget_exceeded
projection_only
```

低层投影即使没有产生摘要，也会发送诊断事件。`projection_only=true` 表示该事件只记录请求视图变换，不表示创建了 Auto-Compact 摘要。

请求诊断会记录：

- `layer1:tool_budget`
- `layer2:snip`
- `layer3:microcompact`
- `layer4:collapse`
- `layer5:auto_compact`
- provider cache miss

诊断不保存 prompt 正文，也不能替代 provider 服务端的 cache hit 统计。

## 7. 已执行验证

本批最终相关回归为：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core tests/coding/test_settings.py tests/coding/test_five_layer_agent.py tests/llm/test_request_projection.py tests/llm/test_openai_provider.py tests/llm/test_anthropic_provider.py -q -o addopts= --import-mode=importlib
```

工作区定向回归为 **651 passed in 14.10s**。从 Git index 导出的干净交付树运行完整
Python 套件（按既有本机 Shell 差异排除 1 项 pipefail 用例）为
**1019 passed, 1 deselected in 52.68s**；TUI 的 `npm test` 同时通过 TypeScript build，
交付树结果为 **96 passed, 2 skipped, 0 failed**。完整记录见
`docs/experiments/HARNESS_EXPERIMENT_LOG.md`。

覆盖点包括：

- 相同输入重复投影字节一致；
- Snip 不拆 tool 配对，并为 Collapse 保留中间组；
- Microcompact 只替换旧工具正文；
- Collapse 只处理中间区段；
- Auto-Compact 作为第五层兜底；
- projection-only 条目不影响摘要查询、Floor 或 canonical folding；
- 分支切换后不复用旧投影；
- `CodingAgent` 默认启用五层且配置覆盖生效；
- DeepSeek cache miss usage 与请求诊断原因可传递。

## 8. 边界

- 单元测试证明确定性、结构安全与持久化契约，不证明真实模型质量提升。
- 本地请求诊断不证明服务端 KV cache 命中率。
- 五层压缩是有损或可恢复投影的组合，不保证摘要保留全部语义。
- `ArtifactStore` 保存失败时只能返回明确的不可用预览，不能保证完整回读。
- projection-only 快照只覆盖请求视图，不提供外部副作用的 exactly-once。
