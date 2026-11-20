# PHASE-05 handoff

## 一句话结论

Context Collapse 只摘要受保护头尾之间的完整中间区域，支持超时/失败降级，并把起止 ID 与摘要写入持久化计划。

## 改了哪些文件

- `src/my_agent_core/context_compression.py`
- `src/my_agent_core/context.py`
- `tests/core/test_context_compression.py`

## 验收对照

| # | 命令 | 实际结果 |
| --- | --- | --- |
| 1 | `test_five_layer_pipeline_makes_context_collapse_reachable` | 通过 |
| 2 | `test_context_collapse_targets_only_middle_history` | 通过 |

## 遗留问题 / 坑

- Snip 必须保留一个中间组给 Collapse，否则五层名义存在但 L4 不可达。

## 下阶段输入

- 本文件
- `docs/phases/PHASE-06.md`
- `src/my_agent_core/context.py`
- `src/my_agent_core/events.py`

## 明确不要做

- 不压缩 system 或 recent tail
- 不用 Collapse 替代 Auto-Compact
