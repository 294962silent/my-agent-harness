# 多租户沙盒运行调度器

实现入口：`src/my_agent_core/sandbox_scheduler.py`

## 目标

在有限内存下接收大量长任务，同时保证：

- 不为每个排队任务创建 asyncio Task；
- 全局活跃任务、队列、租户、沙盒和逻辑内存都有硬上限；
- 不同租户轮转调度，避免一个租户占满全部 worker；
- 同一 Session 同时最多一个运行 owner；
- CPU、子进程和线程槽位只在工具/沙盒命令阶段持有；
- 取消、超时和关闭最终都会释放资源；
- 每个运行获得单调 fencing token 和独立 job identity。

## 容量模型

`SandboxSchedulerLimits` 定义单个 scheduler 实例的全局上限：

```text
max_active_jobs           同时运行的 Job 数
max_pending_jobs          全局排队上限
max_active_per_tenant     单租户活跃上限
max_pending_per_tenant    单租户排队上限
max_sandboxes             同时持有的沙盒槽
max_memory_mb             同时预留的沙盒内存
max_cpu_slots             工具阶段 CPU 槽
max_process_slots         工具阶段子进程槽
max_thread_slots          工具阶段线程槽
```

排队中的 Job 只包含 ticket、runner、资源声明和状态，不创建协程 Task。只有取得全局、租户、沙盒、Session 和内存容量后，才进入运行集合。

队列有界只能限制 scheduler 自己持有的对象。Runner closure 也应只捕获任务 ID、
路径和小型配置，大输入、大输出与轨迹应放 artifact/磁盘，避免 256 个排队 closure
各自持有一份大对象。

## 基本用法

```python
from my_agent_core.sandbox_scheduler import (
    SandboxJobResources,
    SandboxScheduler,
    SandboxSchedulerLimits,
)

scheduler = SandboxScheduler(
    SandboxSchedulerLimits(
        max_active_jobs=8,
        max_pending_jobs=256,
        max_active_per_tenant=2,
        max_sandboxes=8,
        max_memory_mb=8_192,
        max_process_slots=8,
        max_thread_slots=16,
    )
)

async def run_sandbox(execution):
    # 模型等待阶段不申请工具槽。
    async with execution.lease(
        cpu_slots=1,
        process_slots=1,
        thread_slots=0,
    ):
        return await launch_container(
            memory_limit_mb=execution.memory_limit_mb,
            fencing_token=execution.fencing_token,
        )

ticket = scheduler.submit(
    run_sandbox,
    tenant_id="tenant-a",
    session_id="session-17",
    resources=SandboxJobResources(
        sandbox_slots=1,
        memory_mb=512,
    ),
    timeout_seconds=900,
)
result = await ticket
```

`SandboxTicket` 可取消并可等待。取消排队任务会立即移除队列记录；取消运行任务会向 Runner 传播 `CancelledError`，清理完成后才释放槽位。

## 调度与公平性

1. 按提交顺序建立租户轮转环。
2. 每轮优先选择租户中第一个 Session 未运行、资源可容纳的 Job。
3. 单租户达到活跃上限后，本轮跳过该租户。
4. 同一租户中的 Job 不会仅因队首 Session 正在运行而阻塞其他可用 Session。
5. 所有 Job 完成且资源释放后 dispatcher 自行退出，不保留轮询任务。

## 两阶段资源

每个 Job 在整个生命周期内占用：

- `sandbox_slots`
- `memory_mb`
- 一个全局 active job 槽
- 一个租户 active 槽
- 一个 Session owner

CPU、process 和 thread 槽则由 Runner 按阶段申请：

```python
async with execution.lease(process_slots=1, thread_slots=1):
    ...
```

因此模型网络等待不会固定占用工具线程或子进程槽。调度器保证逻辑配额，Runner 必须把 `execution.memory_limit_mb` 传给 Docker/cgroup 才能真正限制容器物理内存；仅设置该字段不会自动修改第三方沙盒。

## Session 与 fencing

当前 `InMemoryLeaseRegistry` 保证单进程内：

- 同一 Session 不并发运行；
- fencing token 单调增加；
- 旧 Job 不能释放新 Job 的 lease。

跨进程调度还需要共享租约存储和全局容量配额。不要仅复制进程内 registry 就宣称多 worker exactly-once。

## 观测

`scheduler.stats()` 返回：

- accepted / rejected；
- started / completed / failed / cancelled / timed out；
- pending / active；
- 每租户 active；
- sandbox 与逻辑内存使用和峰值；
- CPU/process/thread 实时使用与峰值。

## 100-job 压测

`tests/core/test_sandbox_scheduler.py` 提交 100 个 Job：

- 全局活跃上限 8；
- 92 个任务保持排队；
- 实际只存在 8 个 `sandbox-job:*` asyncio Task；
- 逻辑内存峰值 8 MB；
- 全部完成后 active、pending、sandbox 和 memory 均归零。

该测试验证调度与资源账本，不启动 100 个真实 Docker 容器，也不把逻辑内存字段冒充实际 RSS。

可复现的本机 Python 堆实验：

```powershell
.\.venv\Scripts\python.exe scripts\stress_sandbox_scheduler.py --jobs 100 --active 8 --tenants 5 --payload-mb 1
```

本机结果：100 Job、8 active、92 pending、8 个 sandbox Task、活跃 payload 8 MiB，
`tracemalloc` 峰值约 8.53 MiB；全部结束后 active/pending/逻辑内存归零。该数字是当前
Python 进程追踪堆，不是 Docker 容器 RSS。
