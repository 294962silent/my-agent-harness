# PHASE-02 · 五层压缩基础与 Tool Result Budget

## 状态

- 状态：待开始
- 会话：当前实现会话
- 开始 / 结束：2026-11-20 / —

## 输入

- `docs/phases/specs/2026-11-20-five-layer-context-compression-design.md`
- `src/my_agent_core/context.py`
- `src/my_agent_core/artifacts.py`
- `src/my_coding_agent/settings.py`

## 要做

1. 新增压缩配置和稳定 projection ID。
2. 实现单结果与同轮总预算。
3. 接通 RPC 的 `CompactionSettings`。
4. 写请求投影原因 `layer1:tool_budget`。

## 验收

| # | 命令 | 期望结果 |
| --- | --- | --- |
| 1 | `.\.venv\Scripts\python.exe -m pytest tests/core/test_context_compression.py tests/core/test_artifacts.py tests/coding/test_settings.py -q -o addopts= --import-mode=importlib` | 全部通过 |
| 2 | `.\.venv\Scripts\ruff.exe check src/my_agent_core/context_compression.py src/my_agent_core/context.py src/my_coding_agent/settings.py src/my_coding_agent/agent.py` | exit 0 |

## 文件所有权

- 只能改：五层压缩实现计划 Task 1–2 列出的文件
- 只读：其余源码
- 禁止改：RSI、Harbor、benchmark

## 明确不做

- 不实现 Snip 之后的层
- 不运行真实模型
