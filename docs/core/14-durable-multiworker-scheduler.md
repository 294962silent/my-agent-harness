# SQLite 持久化多 Worker 调度

实现：

- `src/my_agent_core/durable_scheduler.py`
- `src/my_agent_core/docker_executor.py`
- `scripts/stress_durable_scheduler.py`

## 与单进程调度器的关系

`SandboxScheduler` 仍适合单进程低延迟调度。`DurableSandboxScheduler` 在其上补齐多 worker 控制面：

- SQLite 持久队列；
- 全局 active / sandbox / memory 容量；
- worker 心跳、TTL lease、fencing token；
- worker 崩溃后的任务重排；
- 幂等提交、重试与速率限制；
- Docker CGroup 限额、挂起检测和确认式清理。

## SQLite 状态

数据库默认开启 WAL 和 `synchronous=FULL`。事务使用 `BEGIN IMMEDIATE`，让全局容量检查、任务选择和 lease 写入处于同一个串行化写事务中。

主要表：

| 表 | 作用 |
| --- | --- |
| `jobs` | 持久任务、状态、attempt、资源、lease、fencing、结果 |
| `tenant_state` | 最近服务时间，用于租户轮转 |
| `workers` | worker 注册和最近心跳 |
| `rate_buckets` | 全局/租户每分钟 RPM、TPM |
| `meta` | 配置一致性、单调 fencing counter |

`idempotency_key` 有唯一约束。相同 key、相同请求返回原 job；相同 key 但 payload 或资源不同会抛 `DurableIdempotencyConflict`。

## Job 生命周期

```text
queued
  -> leased
  -> running
  -> succeeded | failed | timed_out | cancelled
```

`leased/running` 任务计入全局和租户资源。lease 到期后：

1. 删除旧 lease；
2. attempt 小于 `max_attempts` 时按指数退避回到 `queued`；
3. attempt 耗尽时进入 `failed`；
4. 新 claim 获得更大的 fencing token；
5. 旧 worker 之后的 heartbeat 或 finish 因 owner/token 不匹配而失败。

## Worker 用法

```python
scheduler = DurableSandboxScheduler(
    "scheduler.sqlite",
    DurableSchedulerLimits(
        max_active_jobs=8,
        max_sandboxes=8,
        max_memory_mb=8_192,
        global_rpm=600,
        global_tpm=1_000_000,
    ),
)

job = scheduler.submit(
    DurableJobRequest(
        tenant_id="team-a",
        session_id="session-17",
        idempotency_key="task-17-attempt-1",
        payload={"task_path": "/bench/task-17"},
        resources=SandboxJobResources(memory_mb=512),
        timeout_seconds=900,
        max_attempts=3,
    )
)

worker = DurableSandboxWorker(
    scheduler,
    worker_id="host-a-worker-3",
)
result = await worker.run_once(runner)
```

多个 worker 必须使用指向同一 SQLite 文件的 scheduler。每个 worker 使用唯一 `worker_id`。

## 全局 RPM/TPM

Runner 在模型调用前执行：

```python
decision = execution.reserve_model_request(estimated_tokens=4_000)
```

如果超过全球或租户 RPM/TPM，会抛 `DurableRateLimited`。Worker 根据 `retry_after_seconds` 将 job 重新入队，不消耗成功的 attempt 结果。

这是调用前预算。Provider 实际 usage 若明显高于估计值，Runner 应在下一次预留中修正估计，不能把本地逻辑配额当作 provider 最终计费回执。

## Docker 资源与清理

```python
executor = DockerSandboxExecutor()
result = await executor.run(
    execution,
    DockerRunSpec(
        image="python:3.11-slim",
        command=("python", "run_benchmark.py"),
        cpus=1.5,
        pids_limit=64,
        mounts=(
            DockerMount(Path("/bench/task-17"), "/workspace"),
        ),
    ),
)
```

`docker create` 会显式加入：

- `--memory` 和 `--memory-swap`，均取 `execution.memory_limit_mb`；
- `--cpus`；
- `--pids-limit`；
- `--network none`；
- `--read-only`；
- `--cap-drop ALL`；
- `--security-opt no-new-privileges`；
- 非 root user；
- 只读 bind mount。

执行生命周期为：

```text
create -> start -> wait -> logs -> stop -> rm -f -> ps verify
```

创建镜像遇到 Docker registry 429 会指数退避重试。start/wait/logs 超时会进入清理；清理后必须用 `docker ps -a` 确认容器不存在，否则抛 `DockerCleanupError`。

## 已验证故障

- 两个 scheduler 连接同一 SQLite 文件，20 个并发 claim 时全局 active 严格不超过 2；
- 独立子进程领取 Job 后直接退出，parent worker 在 lease 过期后成功接管并取得新 fence；
- 同一 Session 永远只有一个 active job；
- 旧 worker lease 过期后，新 worker 获得新 fencing token，旧 worker finish 被拒绝；
- 全局与租户 RPM/TPM 原子扣减；
- job 429 后进入 retry，下一分钟重新执行；
- worker 取消后运行任务被取消并释放资源；
- Docker registry 429 重试；
- Docker 操作 hang 后执行 stop/rm/verify；
- 容器删除后仍可查询到时 fail-closed。

工作区 core/coding/llm/eval 回归为 `1197 passed, 1 deselected`；从 Git index
导出的干净 staged tree 为 `1037 passed, 1 deselected in 52.91s`。`ruff check src tests scripts`
通过。

## 100-job 多 worker 压测

```powershell
.\.venv\Scripts\python.exe scripts\stress_durable_scheduler.py --jobs 100 --workers 8 --active 8 --tenants 5 --payload-mb 1 --run-seconds 0.2
```

本机结果：

- 100 succeeded；
- failed / queued / active 均为 0；
- 峰值 8 个并发 worker；
- 8 MiB 活跃 payload；
- Python traced heap 峰值约 8.15 MiB（8,546,024 bytes）；
- 最终 sandbox / memory 配额为 0。

## 边界

- SQLite 适合单主机或可靠本地共享文件系统，不等同于跨数据中心分库调度。
- 当前 worker 使用轮询 claim；要降低空轮询需增加事件通知或 broker。
- Provider 的实际 token usage 仍需回填校准，RPM/TPM 预留不是最终账单。
- Docker 测试使用可注入命令 Runner，没有在本机启动 100 个真实容器。
- 容器物理内存由 Docker/cgroup 执行；唯一可靠证据是 Docker 配置和宿主监控，不是 scheduler 的 `memory_mb` 字段。

## 真实 Harbor 接线

`my_eval.durable_harbor` 已把 SQLite worker 接到官方 Harbor Trial。每个 job 运行一个
Harbor Trial；自定义 `LongArcHarborEnvironment` 通过 `DockerSandboxExecutor`
管理 Linux 单容器生命周期，Agent 工具和 verifier 仍走 Harbor 的正式接口。

100 个合成任务的本机实验为 100/100 reward=1，调度峰值 8 active、8 sandbox、4096 MiB；
真实 Docker 容器采样峰值为 4，完成后 managed 容器为 0。两组峰值口径不同，不能混写。
该实验验证调度、隔离与恢复链，不是 Terminal-Bench 分数。详见
[真实 Harbor 压测报告](../experiments/reports/2026-11-21-durable-harbor-live-stress.md)。
