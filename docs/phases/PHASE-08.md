# PHASE-08 · SQLite 多 worker 与 Docker 资源执行

## 状态

- 状态：已完成
- 开始 / 结束：2026-11-21 / 2026-11-21

## 交付

1. SQLite 持久 job、幂等 key、队列、attempt 和结果。
2. owner heartbeat、TTL lease、monotonic fencing 和崩溃恢复。
3. 跨 scheduler 实例共享 active/sandbox/memory 与 RPM/TPM。
4. Docker CGroup 参数、429 retry、hang 清理和存在性确认。
5. 多 worker 100-job 压测。

## 验收

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core/test_durable_scheduler.py tests/core/test_docker_executor.py -q -o addopts= --import-mode=importlib
```

实际：14 passed。

```powershell
.\.venv\Scripts\python.exe scripts\stress_durable_scheduler.py --jobs 100 --workers 8 --active 8 --tenants 5 --payload-mb 1 --run-seconds 0.2
```

实际：100 succeeded，峰值 8 active，traced heap 约 8.15 MiB。

## 明确不做

- 不宣称真实 100 Docker 容器已运行。
- 不把 SQLite 控制面说成跨地域调度系统。
