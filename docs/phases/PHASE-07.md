# PHASE-07 · 100-job 有界沙盒调度

## 状态

- 状态：已完成
- 会话：当前实现会话
- 开始 / 结束：2026-11-21 / 2026-11-21

## 输入

- `src/my_agent_core/run_coordinator.py`
- `src/my_agent_core/run_limits.py`
- `src/my_coding_agent/rpc_scheduler.py`
- `src/my_agent_core/sandbox_scheduler.py`

## 交付

1. 有界准入和全局 active/pending/sandbox/memory 上限。
2. 租户 round-robin、单 Session owner 和单调 fencing token。
3. CPU/process/thread 按工具阶段动态租用。
4. timeout、取消、关闭和资源清理。
5. 100-job 调度压力测试与 Python 堆测量脚本。

## 验收

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core/test_sandbox_scheduler.py tests/core/test_run_coordinator.py tests/core/test_run_coordinator_maintenance.py tests/core/test_run_limits.py tests/core/test_execution_budget_guard.py tests/coding/test_rpc_scheduler.py tests/coding/test_rpc_scheduler_maintenance.py -q -o addopts= --import-mode=importlib
```

实际：84 passed。

```powershell
.\.venv\Scripts\python.exe scripts\stress_sandbox_scheduler.py --jobs 100 --active 8 --tenants 5 --payload-mb 1
```

实际：8 active、92 pending、8 sandbox Task、Python traced heap 峰值约 8.13 MiB。

## 明确不做

- 不启动 100 个真实 Docker 容器。
- 不把逻辑 `memory_mb` 冒充容器的实际 RSS。
- 不宣称当前内存租约已经支持跨进程全局配额。
