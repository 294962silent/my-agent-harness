# Harness 重设计与面试证据执行计划

目标：保留公开入口兼容，为完整框架建立生命周期、观测和逐模块可复算证据。依据 `docs/architecture/HARNESS_VNEXT.md`。主代理负责设计 / 集成 / 实验日志；执行助手使用 GPT-6.1 Sol / medium。

## 全局约束

- Python >= 3.11，当前干净基线 `1f150e1`；不复制原工作区整个 Agent / RPC 文件。
- 每次只改变一个消融变量，脚本 provider 结果只证明机制；收费调用须先批准预算。本轮后续已获得 DeepSeek V4.1 Flash 总预算 $30 授权。
- 生产默认接口兼容，observer 默认关闭，不采集正文；真实 token 缺失不填零。
- 文件所有权分开；不回退别人的改动。新增测试先失败后修复，结果、失败与限制写入证据。

## A. Runtime：单 owner 与执行预算

责任 SOL-1。修改 `src/my_agent_core/agent.py`、`loop.py`，新增生命周期 / limits 小模块与独立测试。必要时更新 events 的终止原因；除此以外不改模型门面、实验或 MemoryStore。

- [x] 为并发归属、无预算循环与取消清理建立回归。未单独保存所有改前失败原始输出，不补称它们已形成完整红绿证据。
- [x] 建立可选 RunLimits / RunState，主模型轮开始前检查并计数；deadline 覆盖异步等待。
- [x] 异常、abort、外部取消、生成器关闭都释放 owner 与清理任务；结构化预算终止。
- [x] 为主模型调用、summary 和 memory_distill 标用途并关联 root，使用 B 提供的上下文接口。
- [x] 跑定点回归，汇报实际参数、失败测试及局限。

实际边界：owner 限于同一 Agent 的一个活动 run；默认不限预算，主循环重试不另计轮次。deadline / 取消不能强抢同步代码或回滚工具副作用，清理可能超过期限。RunLimits 不是请求数或费用上限。

## B. Observation：统一调用与可选 Langfuse 导出

接续负责人 SOL-1。新增 `src/my_agent_llm/observation.py`、导出适配器，修改 `client.py`，新增对应 tests/llm 测试。与 A 通过用途作用域协调。本轮最终遵循用户的两名执行助手限制，不新增 SOL-3。

- [x] 固定 CallStart / CallEnd 与 observation_context 接口；错误统一为 CallEnd(status="error", error_type=...)，不另设 CallError。其他终态为 ok / cancelled / closed。
- [x] 测试同步、异步、首次迭代、正常耗尽、close、取消、错误事件、observer 抛错。
- [x] 门面全入口接入，底层流正确关闭，用量保留 provider 已归一化口径。
- [x] 本地 recorder 支持离线分析；可选导出有界排队、flush、错误计数与无正文默认；没有进行真实 Langfuse 网络上传实验。
- [x] Langfuse 适配通过假 client 验证，依赖延迟导入；不宣称真实服务接通。

## C. Evaluation：逐模块成对实验

责任 SOL-2。只拥有 `experiments/harness_eval/` 与 `tests/experiments/test_harness_eval*.py`。不修改生产模块或共享日志。

- [x] 为每组定义场景、唯一干预、生产调用路径、通过条件与可观测指标。
- [x] 输出不可覆盖的 JSONL 原始记录、manifest 和汇总；源版本 / dirty 指纹 / seed / 环境可追溯。
- [x] 覆盖工具、Session、Context / Artifact、Memory、权限；A / B 接口就绪后追加 Runtime / Observation。
- [x] 实验参数校验、失败场景保留、null 用量与结果可复算测试。
- [x] 运行离线实验，给出命令与原始文件路径，不填预期提升数字。

## D. 集成与面试材料

责任主代理；独立复核由已结束本职工作的助手承担，避免自审。

- [x] 逐模块记录问题、替代方案、实际变化、收益 / 代价、证据与追问。
- [x] 更新开发问题账本：复现 → 原因 → 修复 → 验证 → 未解决边界。
- [x] 生成从原始数据读取的实验报告；主张区分历史、当次离线、当次真实模型及未验证。
- [ ] 全量适当回归、Ruff、TUI 和 wheel；明确路径提交 / 推送自己的 origin，更新 PR。
- [x] 真实实验预算获批后执行配对小样本并追加：官方 SWE 单题两次模型 trial；Artifact 未激活，属于 v1 链路 smoke。v2 仅离线回读验证，Terminal-Bench 环境预检失败且没有模型调用。

本地最终证据：Python 1112 passed / 2 skipped / 1 deselected，Ruff、TUI 构建、wheel 检查通过，TUI 96 passed / 2 skipped。GitHub 提交与 CI 以后续实验日志实际记录为准。
