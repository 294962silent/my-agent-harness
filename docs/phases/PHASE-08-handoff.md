# PHASE-08 handoff

## 一句话结论

已补齐 SQLite 多 worker 控制面和 Docker 资源执行器；并发、lease 恢复、fencing、RPM/TPM、429、hang 与确认式清理均有自动化验证。

## 改了哪些文件

- `src/my_agent_core/durable_scheduler.py`
- `src/my_agent_core/docker_executor.py`
- `src/my_agent_core/__init__.py`
- `tests/core/test_durable_scheduler.py`
- `tests/core/test_docker_executor.py`
- `scripts/stress_durable_scheduler.py`
- `docs/core/14-durable-multiworker-scheduler.md`
- `docs/experiments/HARNESS_EXPERIMENT_LOG.md`

## 验收对照

| # | 命令 | 实际结果 |
| --- | --- | --- |
| 1 | durable + Docker tests | 15 passed |
| 2 | 8-worker / 100-job stress | 100 succeeded, peak active 8 |
| 3 | traced heap | about 8.15 MiB while 8 active |
| 4 | 干净 staged tree 回归 | 1037 passed, 1 deselected |
| 5 | Ruff | passed |
| 6 | GitHub Actions CI | 4/4 jobs success |

## 遗留问题 / 坑

- SQLite 只适合单主机或可靠本地共享文件。
- worker 采用 poll，没有 broker 通知。
- 真实容器压测和 Provider usage 回填尚未执行。

## 下阶段输入

- 本文件
- `docs/core/14-durable-multiworker-scheduler.md`
- `src/my_agent_core/durable_scheduler.py`
- `src/my_agent_core/docker_executor.py`
