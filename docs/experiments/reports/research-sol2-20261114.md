# SOL-2 runtime 验收与 live-v1 恢复诊断

日期：2026-11-14。范围：`research_runtime.py`、`research_cli.py`、`research_integrations.py` 及直接回归；恢复诊断只读主仓库 `.git/codex-delivery/research-runtime-20261114/live-v1/red`。本次恢复未修改生产源码、未改冻结 manifest/pins、未恢复执行原 campaign，未调用付费 API 或真实 embedding。

## 当前实现与验证边界

已实现 hash-chain journal、请求前 token reservation、完整 usage settlement、跨进程预算恢复、round 边界 checkpoint、baseline 保留、独立 final、受限经验写入及 CLI client/observer 关闭。未知请求 usage、未完成 round/evaluation、manifest/checkpoint/report/prediction/image 漂移均应 fail closed。没有请求级 exactly-once；已开始但未知状态的工作不自动重放。

恢复后重新执行：`uv run pytest tests/coding/test_research_runtime.py tests/coding/test_research_integrations.py -q`，27 项定向回归，进程 exit 0。包括真实 CodingAgent + 脚本 LLM + 假 evaluator 的 round 暂停/新对象恢复/独立 final；这些结果不是 Docker 或真实 API 全链路成绩。本次未重跑整库；此前因宿主资源问题丢失的整库结果不可引用。

final 测试验证其独立调用、后续 run 阻断、经验文件不变；无法由这些离线测试证明真实 final 已运行。现有真实 campaign 尚无 committed round，更未触及 final。

## live-v1 原始证据

- journal 顺序：memory_finished → round_started → evaluation_started/finished → request_reserved/settled → round_boundary → observation_closed → provider_clients_closed。
- `calls.jsonl` 有一次真实 `deepseek-v4.1-flash` 请求，status `ok`；prompt/completion/total 分别 1181/71/1252 tokens，已结算。它不是 0 API 运行。
- `result.json` 为 invalid/MemoryError；baseline 评估一次，b0_a0 无 terminal/visible 分数，cleanup_errors 为空，未选候选、未 commit round。
- session 已记录该 assistant 及 ls/find 两条工具消息；没有下一次 request reservation。故失败定位在第一 Agent attempt 内，首响应和两条工具消息之后、下一次付费请求之前。原报告未持久化 traceback，不能恢复具体异常行。
- Mem0 初始化/关闭及 memory_finished 已完成，空库不能称为成功语义召回。该失败不能归因为 Mem0 初始化。

## 非付费最小复现

私有诊断脚本位于主仓库 `.git/codex-delivery/research-runtime-20261114/sol2_replay.py`；命令：`uv run python C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/research-runtime-20261114/sol2_replay.py`。脚本读取已结算 assistant 的工具调用，复制 527-byte baseline 到临时 workspace，通过 SharedLLM → CodingAgent → 同一文件/工具 allowlist 重放 ls/find，第二条脚本响应正常结束。结果：offline_replay passed，2 scripted responses，0 paid requests；未复现 MemoryError。

该最小复现排除了相同工具参数在当前轻负载条件下必然触发错误的假设；它没有重建当时全部上下文、Mem0 常驻内存和全库 pytest 并发。结合先前宿主 STATUS_COMMITMENT_LIMIT，可将瞬时资源压力列为有支持的解释，但不能宣称已确认具体根因或已经修复。当前没有证据支持修改生产逻辑。若未来新增安全 frame(file basename,line,function) 诊断，应作为新源码版本、新 pins 的 instrumentation，不能悄悄继续冻结的 live-v1。

## 1200 字符经验窗口

使用 record_experience 写入单条合成已验证经验（1200 字符 code_excerpt），再实际调用 aretrieve_experience lexical；只报告布尔值/长度：返回 1238 chars，含 method_context=true，代码字符 782。使用有效 identity/scope/revision 的假 semantic index 调用 MemoryStore.aretrieve(..., budget_chars=1200)：返回 1239 chars，含 method_context=true，代码字符 842。后者仅验证候选校验/截断契约，没有运行真实 embedding，不是 Mem0 效果测试。

1200 是 body 字符预算，外层 retrieved_memory 标签额外占长度；method_context 在该单条样例可见，但 code_excerpt 被截断，不能承诺多条经验或不同字段长度下完整保留。原 live-v1 首轮空库没有可验证 method_context。

## 未完成与交接

原 campaign 必须继续保持阻断；真实重试只能由主代理在资源确认和新计划/新输出目录下启动。没有新 API 实验、优化收益、语义召回收益或 final 成绩。共享实验日志由主代理统一维护。
