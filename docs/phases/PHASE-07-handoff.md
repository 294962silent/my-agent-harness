# PHASE-07 handoff

## 一句话结论

已完成单进程、多租户、100-job 有界沙盒调度器；调度正确性和资源清理通过测试，真实容器内存限制仍需 Runner 接入 Docker/cgroup。

## 改了哪些文件

- `src/my_agent_core/sandbox_scheduler.py`
- `src/my_agent_core/__init__.py`
- `tests/core/test_sandbox_scheduler.py`
- `scripts/stress_sandbox_scheduler.py`
- `docs/core/13-sandbox-run-scheduler.md`
- `docs/experiments/HARNESS_EXPERIMENT_LOG.md`

## 验收对照

| # | 命令 | 实际结果 |
| --- | --- | --- |
| 1 | scheduler/coordinator/limits 回归 | 84 passed |
| 2 | 工作区 core/coding/llm/eval 集成回归 | 1182 passed, 1 deselected |
| 3 | 干净 staged tree 全量回归 | 1022 passed, 1 deselected |
| 4 | 100-job stress | 8 active, 92 pending, 8 Task, traced peak 8.13 MiB |
| 5 | staged tree Ruff | All checks passed |
| 6 | GitHub Actions CI | 4/4 jobs success |

## 遗留问题 / 坑

- 当前 `InMemoryLeaseRegistry` 只保护单进程。
- 跨进程需要共享 lease store、全局容量 broker 和 heartbeat/fencing 回收。
- `memory_mb` 必须由实际 Runner 传给 Docker/cgroup。

## 下阶段输入

- 本文件
- `docs/core/13-sandbox-run-scheduler.md`
- `src/my_agent_core/sandbox_scheduler.py`

## 明确不要做

- 不把假 Runner 压测写成真实容器吞吐。
- 不在未验证 Docker 环境的情况下宣称 100 容器并发。
