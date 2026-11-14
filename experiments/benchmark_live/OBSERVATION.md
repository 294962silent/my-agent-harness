# 实验 Langfuse owner 与单次真实验证

本入口用于将来新建的付费实验。它先把既有 ObservationRuntime 订阅到 raw LLM，再通过现有 GuardedLLM 和持久账本发出请求；请求结算后关闭 exporter、flush、SDK shutdown 和拥有的 provider clients。未更改 BudgetLedger、GuardedLLM 或已冻结的 Memory/RSI 源码。

SessionScopeSink 在 exporter 工作线程调用官方 propagate_attributes。campaign_id 映射稳定 session_id；root/run ID 映射 trace；每条记录仅携带 campaign、arm、stage、匿名 task_id。调用者必须提供匿名标识，不得把问题、答案、gold、history、凭据放入标识或 metadata。关闭时保留导出错误/丢弃/线程状态；导出失败不会重试付费请求。

配置入口是用户目录 .my-pi-agent/settings.json 的 observation 块：enabled=true、backend=langfuse、base_url=https://cloud.langfuse.com，public_key_env/secret_key_env 使用环境变量名称。项目内设置不能覆盖这个特权块。凭据在本机环境中配置；代码不读取认证文件或自动加载 .env。--windows-user-env 只把四个 LANGFUSE 变量从已授权的 Windows User/Machine 环境载入子进程内存，不输出值。模型调用必须已配置 LIVE_BENCH_API_KEY；无凭据时在发出请求前停止。

单次付费命令必须显式带 --single-paid-probe。请使用全新空目录、全新 campaign/run ID；旧输出会拒绝执行，未知用量不自动重试。占位参数需替换为自己的本机路径和匿名标识：

~~~text
uv run --no-sync python -m experiments.benchmark_live.observation_probe --single-paid-probe --windows-user-env --output NEW_EMPTY_PRIVATE_DIRECTORY --campaign-id NEW_OPAQUE_CAMPAIGN --run-id NEW_OPAQUE_RUN --project-id YOUR_PROJECT_ID
~~~

此命令上限为 1 次 provider 请求、128 output tokens、120 秒、0 retry，持久 TokenLedger 额外限制 10000 tokens。账本与收据必须在不可变 plan 目录以外；金额代理上限不是实际费用，费用无法核验时保持 unavailable。CLI 只保存状态、usage、匿名标识、source SHA 与生命周期计数，不保存模型内容。

将来 Memory 接线使用一个 owner 包裹原来创建的 raw LLM；已有 ledger.register_run 与持久账本保持原有语义：

~~~python
owner = ExperimentLLMOwner.from_environment(
    raw, campaign_id=campaign_id, enabled=observation.enabled,
    public_key_env=observation.public_key_env,
    secret_key_env=observation.secret_key_env,
    base_url=observation.base_url,
)
try:
    response = await owner.achat(
        ledger, run_id, messages, arm=arm, stage=stage,
        task_id=anonymous_task_id, max_tokens=1024, temperature=0,
    )
finally:
    close_receipt = await owner.aclose()
~~~

这段接线只能用于新 protocol/source pins。现有已完成 Memory 入口只有 observation_context，未创建云 runtime；仅开启全局开关不会补齐这些旧调用。已运行 RSI 请求也没有追溯为 live。历史导入若另行进行，必须标为 backfill。

2026-11-14 验证先用既有 runtime 真实请求 57 tokens，再用本公开 owner/CLI 真实请求 56 tokens，合计 2 请求/113 tokens。两次均结清，0 unknown/held/retry。新入口远端 sessionId、四项 metadata、模型、38 input/18 output/56 total 全部匹配；source SHA 见公开 verification.json。原 probe 不含 sessionId。新入口只验收单 generation 携带 session，不声称已实测多轮或跨 trace 分组。

在已设置 Langfuse 环境的本机读回新入口记录：

~~~text
npx langfuse-cli api observations list --trace-id a6249bf9c98cadede781b034e58ea58d --fields core,basic,model,usage,trace_context --json
~~~

CLI 响应为 status/headers/body envelope，记录在 body.data。不要用当前 snapshot 不支持的 traces get。若需读取 metadata，先做应用字段白名单过滤，排除 SDK resource/scope 附加属性；不要把完整响应直接写入报告。远端 totalCost=null，故只报告真实 tokens，不报实际金额。

metadata-only 故意省略 prompt/response，不能从云端完整回放内容或直接做依赖内容的云端评测。完整 agent/retriever 树与实际多轮分组尚未验证。

离线验证（不调用模型或网络）：

~~~text
uv run --no-sync pytest -q tests/experiments/test_experiment_observation.py tests/integrations/test_langfuse_sdk.py tests/llm/test_observation_runtime.py
uv run --no-sync ruff check experiments/benchmark_live/observation.py experiments/benchmark_live/observation_probe.py tests/experiments/test_experiment_observation.py
~~~

本批结果为 17 passed（10 个新 owner/CLI 测试加 7 个既有 SDK/runtime 测试），Ruff All checks passed。包含真实 SDK 5.2.0 + in-memory transport 的 session 传播验证，禁止 socket 网络；unknown usage 保留账本、已结算请求遇导出失败不重试、重复 output 拒绝、显式付费开关及 owned close 均有定向覆盖。
