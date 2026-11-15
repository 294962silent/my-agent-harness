# 公开长程代码三题三臂 Pilot：v2 故障定位与 v3 启动门控

日期：2026-11-15
工作树：`C:\Users\MLL\Desktop\my-agent-harness\.worktrees\interview-redesign`
分支：`codex/interview-harness-redesign`

## 结论

v2 首笔真实 trial 已发生并完整结算 **1 request**：input 662、output 64、客户端计费 `$0.000276`。随后首个 `shell` 调用在真正进入容器前被标记为 `Tool call interrupted by user`，没有 verifier/reward；后八槽未启动。该失败与费用原样保留在 `long-horizon-code-20261115-v2`，未重试、未修复历史。

最终运行总账见 [长程代码 Harness 六小时证据包](long-horizon-code-6h-results.md)。v3 已完成：

- `fix-git` 三臂均为官方 reward=1；17/10/12 requests，cost 分别为 `$0.033823/$0.012753/$0.021009`。
- `large-scale-text-editing/enhanced` 在 1,080 秒上限取消；22 requests 完整结算，官方 verifier 实际执行且 5/5 失败，不构成 reward。
- Langfuse 云侧回读到 4 个 trace、61 个 generation observation，和 v3 的 61 个真实请求完全对应。
- `custom-memory-heap-crash` sidecar 首次启动因 host 未注入 API key 而 0 request 停止；run ID 保留 consumed，不重试。

根因已定位并修复：`Agent.prompt_stream` 的 `RunState.next_event()` 每次用新的 asyncio Task 执行 `anext(stream)`；旧 `ObservedLLM` 将 `owner.observe()` 的 ContextVar token 跨多个 `yield` 持有，退出时在不同 Task 中 reset，触发 `ValueError: Token was created in a different Context`。Agent 随后取消信号、清理未执行 tool，因此 session 只留下 interrupted tool result，旧 adapter 又把它包装成通用 `RuntimeError`。

修复后使用真实 `my_agent_core.Agent`、实际执行 shell tool、fake provider/fake Langfuse owner 和强制 ContextVar scope 回归；模型先返回 shell call，环境确实执行 `pwd`，再返回文本完成。预算 ledger 两个模型回合各结算一次，共 2 条 settled request，没有双计费。

新 v3 campaign 已完成零模型门控：三 oracle reward=1、强 transport pass、Langfuse four-env gate pass、preflight=ready。门控冻结时 ledger 为 0 requests；之后主代理已独立启动 v3。只读观察首个 `fix-git/baseline` 已完成：17 requests、input 85,025、output 6,924、known cost `$0.033823`、Langfuse ready、官方 reward=1，证明跨 Task 修复在真实链路生效。

## 冻结身份

- source：`https://github.com/harbor-framework/terminal-bench-2-1`
- revision：`12b7565679ce5006230b820666353673a133db93`
- campaign：`lhpp-20261115-v3`
- plan SHA-256：`f15d6ade822a7dcc582e15d4df9d63d382387395f0f4578c334fccf5e254008b`
- `harbor_agent.py` source SHA-256：`2ca41493e2edb2621dc5a08239ae5428fe17f0d39fe4f2133784d12ccddf1938`
- `public_pilot.py` source SHA-256：`486f5be7138a2826731e031d51b6ca1015a508bf238643bfec5fc47f067913ec`
- source drift：通过
- ledger：`C:\Users\MLL\Desktop\my-agent-harness\.git\codex-delivery\long-horizon-code-20261115-v3\budget.json`
- ledger：root ceiling `$30`、9 个 run；v3 门控快照为 0 requests，随后由主代理消费

v1 的 0-request source-drift campaign 和 v2 的单请求失败 campaign 均原样保留。旧共享账本未修改。v3 使用新目录、campaign 和 run IDs，不复用任何已开始 run。

## 修复内容

1. `ObservedLLM.stream`、`achat_stream`、`astream_events` 在单个执行器调用内完整缓冲底层事件，`owner.observe()` 在同一 Task 进入并退出后才逐个 yield；“顺序不变、上下文 token 不跨 Task”是修复核心。
2. `BudgetedHarborAgent` 保留 Agent 原始异常类型、截断到 1000 字符的脱敏消息和最多 8000 字符的 traceback，写入私有 `live_run.json.exception` 与 `exception-metadata.json`。已知 API/Langfuse secret、Bearer 和 `sk-*` 模式会替换为 `[REDACTED]`。
3. required Langfuse 使用 `ExperimentLLMOwner.from_environment(raw, enabled=True, required=True)`；每次 Agent 模型调用都包裹 `owner.observe(run_id, arm=<B/E/L>, stage="final", task_id=<task slug>)`。Budget guard 仍是唯一请求预留/结算层。
4. trial 结束或异常后执行 `aflush_and_check()` 和 `owner.aclose()`；flush、export、drop、close、client-close 任一异常均使 required observation fail closed，并将状态写入 `live_run.json`。

## 三题三臂

| 题 | 覆盖 | B | E | L |
|---|---|---|---|---|
| `fix-git` | Git/reflog 恢复 | baseline + artifact off | layered + artifact on | layered + artifact off |
| `large-scale-text-editing` | 百万行长输出/上下文 | baseline + artifact off | layered + artifact on | layered + artifact off |
| `custom-memory-heap-crash` | C++ 调试/heap/Valgrind | baseline + artifact off | layered + artifact on | layered + artifact off |

固定顺序：`fix-git B→E→L`、`large-scale E→L→B`、`custom-memory-heap L→B→E`。`E-L` 是 artifact projection 的机制 LOO。每 trial 最多 64 requests、context 32,000、output 4,096、shell timeout 120 秒；agent deadline 统一预留 120 秒排空窗口（780/1080/1680 秒）。

## v3 零模型门控

Langfuse 配置冻结为：

- enabled/required：true/true
- stage：`final`
- base URL：`https://cloud.langfuse.com`
- 环境变量名：`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`、`LANGFUSE_HOST`
- plan 中只记录名称、presence 和 URL 匹配结果；不记录值

本机实际 presence 全为 true，BASE_URL/HOST 均匹配 cloud endpoint。Harbor 私有 venv 已安装锁定版本 `langfuse==5.2.0`。

最终 transport 对三个官方镜像分别验证 `github.com:443` 与 `release-assets.githubusercontent.com:443`，六条 TLS 握手全部通过。fresh oracle：

| 题 | reward | task checksum | result SHA-256 |
|---|---:|---|---|
| `fix-git` | 1 | `e89f54f2fc73ac576f976c1cd09758e4c9ccf152e53c2dcd91da0c178b89abc7` | `3d51fafb014630f0312ef2dff0f2296cce8426121afa250da367bba5a02878c2` |
| `large-scale-text-editing` | 1 | `c02f82f532c630aeddd73cf9fd663ab77b07fb31517cbbca248450131e63ad71` | `2d40a0ec159531bce764b541e12dbc276e37a3172c3a04ec2590d00d463c85d6` |
| `custom-memory-heap-crash` | 1 | `949f44d01f9411e5858fdfd0861e2c35e9622cbfe91542b761039a7a16286381` | `2f0d833baeeb666588559395e1fce743056e2a111eecfc3f8b9511b3a74fd30d` |

最终零模型 preflight：credential 来源 `production_auth_manager`、proxy reachable、transport ready、oracle ready、Langfuse ready、`status=ready`。该 receipt 是 v3 首笔付费 trial 启动前的状态。

## 测试

- WSL 定向 pytest：**155 passed**。
- Windows 定向测试：**14 skipped / exit 0**；WSL/Harbor-only 模块在 Windows 收集阶段跳过，Linux 断言未削弱。
- Ruff：本次四项文件 `All checks passed`。
- 新回归使用真实 Agent 的 Task-per-event 路径，旧跨 Task ContextVar 实现会触发 `ValueError`，修复后 shell 实际执行并通过。
- fake owner 验证每个模型回合各进入一次 `stage=final` scope、required factory 参数、flush/close 各一次；fake provider 的两个 request 在 ledger 中各结算一次。

## 精确命令

先在 Windows 独立终端启动唯一 scoped proxy，并保持运行：

```bat
C:\Users\MLL\AppData\Roaming\uv\python\cpython-3.13.13-windows-x86_64-none\pythonw.exe C:\Users\MLL\Desktop\my-agent-harness\.git\codex-delivery\bench-proxy.py
```

从 Windows `cmd` 启动第一个 v3 付费 trial；该前缀只传播变量名，不包含密钥值：

```bat
set WSLENV=LANGFUSE_PUBLIC_KEY/u:LANGFUSE_SECRET_KEY/u:LANGFUSE_BASE_URL/u:LANGFUSE_HOST/u&& wsl.exe --cd /mnt/c/Users/MLL/Desktop/my-agent-harness/.worktrees/interview-redesign -e /usr/bin/env PYTHONPATH=src:. /home/mll/.cache/my-agent-bench/venv/bin/python -m experiments.long_horizon_code.public_pilot run --expected-sha f15d6ade822a7dcc582e15d4df9d63d382387395f0f4578c334fccf5e254008b --execute --max-new-trials 1
```

首槽有效后，同一命令改 `--max-new-trials 9` 可继续并跳过已完成槽。

## 证据边界

当前可声称：修复后的真实链路可完成 `fix-git` 三臂；`fix-git` 的 enhanced 臂使用 10 requests、baseline 使用 17 requests；Langfuse required 观测与 61 个结算请求完全对齐；长输出任务在 1,080 秒 cap 内未交付，且保留真实 verifier 失败。

当前不可声称：五层压缩或 artifact projection 带来显著或稳定收益；`large-scale-text-editing` 已通过；`custom-memory-heap-crash` 三臂已完成；三题或单题结果可推断 Terminal-Bench 2.1 总体能力；checkpoint resume/memory 已在公开代码任务上消融。

## custom-memory sidecar 零请求故障与修复

v3 sidecar 首次执行 `custom-memory-heap-crash/loo_artifact` 时，`run` 只信任先前写出的 `readiness/custom-preflight.json`，没有在自己的 host 进程重新调用 `public_pilot.install_live_credential()`。Harbor 子进程因此没有继承 `LIVE_BENCH_API_KEY`，在 `create_live_llm()` 创建首个客户端前即失败。`exception-metadata.json` 保留的原始类型为 `ValueError`，消息为 `LIVE_BENCH_API_KEY must be supplied to the host process`；trial 已有开始标记但模型 request 为 0，不能重试该 run ID。

修复后的 `custom_memory_supplement run` 在每次执行前重新走实时 gate：校验冻结 plan/source、三题 oracle receipt、transport receipt、Langfuse required 配置、production AuthManager credential、scoped proxy、v3 base preflight、ledger 与独立 custom journal。credential 通过 `install_live_credential()` 注入当前 host 进程；若旧 receipt 为 ready 但当前认证不可用，则在创建 trial、写 `trial_started` 或调用 executor 前失败。已有 custom journal 中任何非 `finished/scored` 状态，或已消费 run ID，均会使候选槽 blocked。

本轮定向验证：

- WSL sidecar tests：4 passed；sidecar + public-pilot：18 passed。
- Windows 收集：18 skipped / exit 0，只跳过依赖 WSL Harbor 与固定 Linux proxy helper 的测试。
- `ruff check`：两文件 All checks passed。
- 实时零模型 preflight：production credential、oracle、transport、Langfuse four-env、proxy 与 v3 base preflight 均通过；因旧 custom loo 已非 scored，最终状态正确写为 `blocked`。

当前 v3 ledger 保留 61 条已完整结算的模型 request，客户端已知成本合计 `$0.144249`；custom loo run `lhpp-f49f3a45da67234cff98a2c0948c3d39` 为 `trial_started=true`、0 request、`execution_or_verifier_error`，其 baseline/enhanced 仍为 not started。v3 plan SHA `f15d6ade822a7dcc582e15d4df9d63d382387395f0f4578c334fccf5e254008b` 未改，旧结果未回写或重试。后续若补跑 custom-memory 三臂，应新建 v4 campaign、ledger 与九个冻结 run ID，再 freeze、oracle、transport、preflight；v3 custom sidecar 保持 blocked。
