# PHASE-03 handoff

## 一句话结论

Snip 按完整交互组删除旧请求视图消息；projection-only 快照可持久化、可恢复、可校验，并且不影响 summary lookup、Floor 或 canonical folding。

## 改了哪些文件

- `src/my_agent_core/context_compression.py`
- `src/my_agent_core/context.py`
- `src/my_agent_core/session/session.py`
- `src/my_agent_core/session/memory.py`
- `src/my_agent_core/agent.py`
- `tests/core/test_context_compression.py`

## 验收对照

| # | 命令 | 实际结果 |
| --- | --- | --- |
| 1 | projection snapshot/recovery pytest | 通过 |
| 2 | branch/floor/session folding 回归 | 通过 |

## 遗留问题 / 坑

- Snip 若删掉所有可删组，会令 Context Collapse 不可达；生产编排为 L4 预留最新一个完整中间组。

## 下阶段输入

- 本文件
- `docs/phases/PHASE-04.md`
- `src/my_agent_core/context.py`
- `src/my_agent_core/agent.py`

## 明确不要做

- 不要让 projection-only 条目更新 `current_id` 或 `compaction_floor`
- 不要回退 projection 结构上限和分支绑定
