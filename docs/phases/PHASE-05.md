# PHASE-05 · Context Collapse

## 状态

- 状态：待开始
- 会话：当前实现会话
- 开始 / 结束：2026-11-20 / —

## 输入

- `docs/phases/PHASE-04-handoff.md`
- `src/my_agent_core/context_compression.py`
- `src/my_agent_core/context.py`
- `src/my_agent_core/session/session.py`

## 要做

1. 选择受保护头尾之间的完整中间范围。
2. 只摘要该范围并构造局部投影。
3. 持久化来源和摘要，原始历史保持不变。

## 验收

| # | 命令 | 期望结果 |
| --- | --- | --- |
| 1 | `.\.venv\Scripts\python.exe -m pytest tests/core/test_context_compression.py tests/core/test_context_boundary.py -q -o addopts= --import-mode=importlib` | 全部通过 |
| 2 | `.\.venv\Scripts\ruff.exe check src/my_agent_core/context_compression.py src/my_agent_core/context.py src/my_agent_core/session` | exit 0 |

## 文件所有权

- 只能改：五层压缩实现计划 Task 5 列出的文件
- 只读：其余源码
- 禁止改：RSI、Harbor、benchmark

## 明确不做

- 不压缩 system 或 recent tail
- 不替代 Auto-Compact
