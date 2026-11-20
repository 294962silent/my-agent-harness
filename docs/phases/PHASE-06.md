# PHASE-06 · Auto-Compact 集成与最终验收

## 状态

- 状态：待开始
- 会话：当前实现会话
- 开始 / 结束：2026-11-20 / —

## 输入

- `docs/phases/PHASE-05-handoff.md`
- `src/my_agent_core/context.py`
- `src/my_agent_core/events.py`
- `docs/core/05-context-compaction.md`

## 要做

1. 把 Auto-Compact 接为第五层和最终兜底。
2. 输出层顺序、缓存 epoch 和 `budget_exceeded`。
3. 更新文档和实验记录。
4. 运行完整相关回归。

## 验收

| # | 命令 | 期望结果 |
| --- | --- | --- |
| 1 | `.\.venv\Scripts\python.exe -m pytest tests/core tests/coding/test_settings.py tests/llm/test_request_projection.py tests/llm/test_openai_provider.py tests/llm/test_anthropic_provider.py -q -o addopts= --import-mode=importlib` | 全部通过 |
| 2 | `.\.venv\Scripts\ruff.exe check src tests` | exit 0 |
| 3 | `git diff --check` | exit 0 |

## 文件所有权

- 只能改：五层压缩实现计划 Task 6 列出的文件
- 只读：其余源码
- 禁止改：RSI、Harbor、benchmark

## 明确不做

- 不启动真实 API
- 不宣称缓存或质量收益
