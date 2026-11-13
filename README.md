# my-agent-harness

**面向长程任务的 Coding Agent Harness：长期记忆、可观测执行、受控策略自优化。**

以长程代码任务为目标，补强多轮执行中的运行控制、上下文保真、跨会话事实记忆和可复核证据。CodingAgent、RPC 和终端界面是应用入口；自优化层自动选择并验证受限的探索策略参数，不训练模型权重、不任意重写执行器。

项目用于 Agent / 大模型应用开发实习的工程实践：保留可阅读的模块接口，用实际故障、取舍、回归和成对实验解释技术价值。框架仍有明确限制，不把它描述为已经成熟的生产平台。

## 从哪里开始

- [框架重设计](docs/architecture/HARNESS_VNEXT.md)：模块职责、不变量、依赖方向和本批边界。
- [面试证据入口](docs/interview/README.md)：八模块讲解、开发问题与主张边界。
- [统一离线实验](experiments/harness_eval/README.md)：脚本 provider / 临时文件验证真实工程路径。
- [本轮原始结果](docs/experiments/results/redesign/20261112T095857-2381042b458f/report.md)：manifest、raw.jsonl 和 summary 同目录。
- [持续实验记录](docs/experiments/HARNESS_EXPERIMENT_LOG.md)：历史、失败、更正及交付状态。

## 框架组织

`my-tui → my_coding_agent → my_agent_core → my_agent_llm`

- Runtime：单 Agent 同时仅有一个直接运行 owner，可选轮次 / 总时长上限，明确取消、预算和正常完成。
- Model：多 provider 门面，统一同步 / 异步 / 流式调用和流关闭；生命周期观测覆盖主请求、摘要与记忆抽取。
- Tools：注册表、schema、安全并发声明、依赖批次门控、超时与中断结果。
- Session：JSONL 会话与分支路径恢复；摘要绑定 canonical 覆盖来源。
- Context / Artifact：原始历史与发送视图区分，大输出保留全文并通过有界工具回读。
- Memory：精确事实、替换历史、删除、脱敏与检索；可用本地CPU中文语义候选，不需embedding API，自动抽取默认关闭。
- Permission：副作用前授权，默认 fail-closed；不等同完整信息流安全。
- Observation：默认关闭的元数据记录、有界导出与可选 Langfuse 适配，导出错误不改变模型结果。

本批实现Runtime生命周期、统一观测和三个适配。Mem0支持本地CPU中文profile；Langfuse配置与生命周期完整；Dream支持候选参数搜索、独立验证、active实际消费和回滚。外部云服务分发默认关闭；本机已安装并启用本地记忆，自优化入口因无真实评分树明确needs_dataset。

## 安装与本地验证

需要 Python >= 3.11、Node.js；新源码应从当前分支构建，旧 npm 发布包不自动包含这些变更。

```sh
uv sync --frozen
npm ci
npm run build
uv run python -m pytest tests -q
npm test
```

Windows 缺少 Git Bash 时，既有 pipefail 测试会因 cmd 不支持 cat 而失败；本轮本地报告明确排除这一环境项，GitHub 跨平台检查结果单独记录。真实模型调用需要单独配置供应商与凭据；离线实验不读取凭据或产生模型费用。

## 有预算的 Agent 调用

Python 接口保持 run / prompt_stream / abort，新增可选 RunLimits：

```python
import asyncio
import os
from pathlib import Path

from my_agent_core.agent import Agent
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_llm import Config, LLM
from my_agent_llm.observation import InMemoryObserver

async def main():
    recorder = InMemoryObserver()
    llm = LLM(Config(
        provider="openai",
        model="deepseek-v4.1-flash",
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        max_tokens=2048,
        max_retries=0,
    ), observer=recorder)
    agent = Agent(
        llm=llm, tools=[],
        session=Session(path=Path(".my-agent-harness/demo.jsonl")),
        memory_dir=False,
        run_limits=RunLimits(max_turns=6, max_seconds=120),
    )
    try:
        print(await agent.run("用一句话说明工具调用的作用"))
    finally:
        await agent.aclose()
    print("观测条数", len(recorder.records))

# 确认供应商配置后才运行；本例会产生模型调用费用。
# asyncio.run(main())
```

轮次 / 时长上限不等同费用上限。真实基准入口另用共享账本逐请求预留与结算；同步不协作代码及已经发生的外部副作用不能被强制回滚。

只需要本地观测时使用 InMemoryObserver 或 JsonlSink + BoundedExporter。Langfuse 适配延迟导入，已通过真实5.2 SDK的内存导出契约，尚未确认真实服务上传。

## Mem0、Langfuse、Dream-RSI

```sh
uv sync --frozen --extra mem0-local --extra langfuse --extra dream-rsi
uv run --extra dream-rsi my-agent-dream --help
```

[安装、全局配置与使用说明](docs/integrations/README.md)包含本地模型准备、JSON及合成参数搜索演练。Mem0保留本地事实权威、infer=False和NeverLLM防双抽取，失败回退词法；Langfuse保留已有observer；Dream只调白名单参数，晋升须胜过baseline和当前active，原子指针/CAS支持回滚。项目settings不能改三项集成的全局配置。

本轮只做代码/功能验证：Mem0真实BGE CPU语义链路、Langfuse内存SpanExporter、Dream合成数据dry-run与活跃参数消费。没有真实评分树，不晋升合成策略；未测召回质量、真实云端或策略收益。SWE及效果benchmark继续暂停。

## 逐模块实验

```sh
# 在仓库工作目录运行，确保 src 与仓库根可导入。
PYTHONPATH=src:. uv run python -m experiments.harness_eval   --suite all --repeats 5 --seed 23 --output docs/experiments/results/redesign
```

PowerShell 可先设置 `$env:PYTHONPATH='src;.'`。每次输出唯一目录，保留原始记录、环境、源码 hash、参数和失败，汇总从记录计算。真实 ablation、有限 comparison wrapper 和 contract 明确区分。

已保存这一轮：8 模块、12 独立场景、100 条原始观测，断言失败 0。固定大输出夹具的发送视图从 120019B 变为 7041B，两组全文证据均可恢复；工具独立并发峰值 1→3，依赖违例 1→0。上述结果只代表夹具内工程行为，不能声称真实模型成功率或 KV cache 提升。两条记忆查询只是 smoke；重复次数不能充当新增问题数量。

## 开源真实任务

[真实评测入口](experiments/benchmark_live/README.md)使用生产 Agent、ContextManager、Session 和容器 shell；先运行未修改官方任务的 oracle / gold，再开始模型 trial。测试 / solution 不进入 solver prompt，缺失环境单列，结果只代表预选小样本。

[预算方案](docs/experiments/LIVE_MODEL_BUDGET.md)采用用户授权的 DeepSeek V4.1 Flash 总上限30美元，同一账本逐请求预留与结算，缺失用量不当零，禁用隐式重试。[本轮v2真实结果](docs/experiments/results/swe-v2-pilot-20261112/REPORT.md)：固定6题、5题环境可用、10次模型trial，开启组通过2/5、关闭组1/5；7次空补丁。160请求保守账本$0.503462，非供应商核账。on产生11个外置产物但没有回读，on费用$0.252843、off$0.250619，不能宣称稳定质量、费用或KV收益。

通用入口使用批准的catalog和独立campaign，已归档结果可在离线核hash后重算。[历史v1单例](docs/experiments/results/live-swe-20261112/REPORT.md)的32请求/$0.068597另保留。[随后三题预算提示对照](docs/experiments/results/swe-budget-visibility-20261112/REPORT.md)双臂均1/3通过，未提供改善证据；加独立回读探针后累计保守账本$0.888355。Terminal-Bench仍没有模型成绩。上述是适配前历史结果，不能当作三个新适配器的消融；后续benchmark暂停。

## 限制与历史

进程内协作取消不保证强制抢占；后台记忆不是持久 worker；本地索引无跨进程写锁；权限不识别所有组合信息泄漏；有界摘要与抽取不能保证语义无损。详见[记忆说明](docs/memory.md)与各模块证据。

[基线 README](https://github.com/294962silent/my-agent-harness/blob/1f150e1a433c2b31ee83c8a2d11300e4a1fc3548/README.md)中的旧性能 / 评测数字属于历史记录，本轮没有重新复测，不能直接算成本批重设计收益。

代码按仓库 LICENSE 分发。外部基准数据、任务和第三方依赖遵循各自许可证，版本与可核验范围记入对应运行 manifest。
