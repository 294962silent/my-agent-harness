# PHASE-03 · Snip 与 projection plan

## 状态

- 状态：已完成
- 会话：当前实现会话
- 开始 / 结束：2026-11-20 / 2026-11-20

## 输入

- `docs/phases/PHASE-02-handoff.md`
- `src/my_agent_core/context_compression.py`
- `src/my_agent_core/session/session.py`
- `src/my_agent_core/session/memory.py`

## 要做

1. 按稳定消息 ID 选择完整的旧交互组。
2. 保护 system、首个任务组、当前请求和 recent tail。
3. 将 projection plan 追加持久化并校验来源。
4. 保证原始 Session 不变。

## 验收

| # | 命令 | 期望结果 |
| --- | --- | --- |
| 1 | `.\.venv\Scripts\python.exe -m pytest tests/core/test_context_compression.py tests/core/test_session.py tests/core/test_session_memory_and_storage.py -q -o addopts= --import-mode=importlib` | 全部通过 |
| 2 | `.\.venv\Scripts\ruff.exe check src/my_agent_core/context_compression.py src/my_agent_core/context.py src/my_agent_core/session` | exit 0 |

## 文件所有权

- 只能改：五层压缩实现计划 Task 3 列出的文件
- 只读：其余源码
- 禁止改：RSI、Harbor、benchmark

## 明确不做

- 不实现 Microcompact 正文替换
- 不物理删除消息
