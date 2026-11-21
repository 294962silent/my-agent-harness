# PHASE-04 · Microcompact 与显式缓存失效

## 状态

- 状态：已完成
- 会话：当前实现会话
- 开始 / 结束：2026-11-20 / 2026-11-20

## 输入

- `docs/phases/PHASE-03-handoff.md`
- `src/my_agent_core/context_compression.py`
- `src/my_agent_core/agent.py`
- `src/my_agent_core/loop.py`

## 要做

1. 仅在显式缓存失效 epoch 中运行。
2. 清除旧工具正文并保留调用结构。
3. 接通模型、分支、resume、摘要替换和 provider miss 事件。

## 验收

| # | 命令 | 期望结果 |
| --- | --- | --- |
| 1 | `.\.venv\Scripts\python.exe -m pytest tests/core/test_context_compression.py tests/core/test_agent.py tests/llm/test_request_projection.py -q -o addopts= --import-mode=importlib` | 全部通过 |
| 2 | `.\.venv\Scripts\ruff.exe check src/my_agent_core/context_compression.py src/my_agent_core/context.py src/my_agent_core/agent.py src/my_agent_core/loop.py` | exit 0 |

## 文件所有权

- 只能改：五层压缩实现计划 Task 4 列出的文件
- 只读：其余源码
- 禁止改：RSI、Harbor、benchmark

## 明确不做

- 不使用时间猜测 cache miss
- 不删除 tool call 或 result 消息
