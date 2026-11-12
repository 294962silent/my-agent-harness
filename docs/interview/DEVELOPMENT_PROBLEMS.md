# 开发问题与解决过程

日期：2026-11-12。记录实际复现，不把预期故障写成已经发生，也不把开发中间版本缺陷归到干净基线。

## P01：运行取消破坏工具调用配对

触发：provider 已交付 ToolCallDone，但尚未交付 StreamDone，此时 abort / deadline / 消费者关闭。第一次直接取消迭代任务后，既有核心契约测试失败，Session 续跑可能带孤立工具调用。

原因：运行归属和预算开始覆盖更多异步等待，原先循环内部补结果的收尾来不及执行。

解决：集中终止收尾，只保存已经观察到的 assistant 工具意图，补恰一条 is_error 中断结果，不把纯 partial 文本当完整回答。增加三种终止后的 Session.load 回归；不放宽 deadline 来掩盖问题。

证据：`tests/core/test_run_lifecycle.py`、`test_loop_run_limits.py`；主代理整套回归 1105 passed、1 skipped、1 deselected。局限：工具已经产生的外部副作用不能回滚。

## P02：观测 exporter 线程退出不代表资源清理成功

触发：Langfuse sink 的 flush 抛异常。独立假 client 探针发现线程已退出，但 own_client.shutdown 未被调用。

解决：flush / shutdown 使用 try/finally，测试失败路径；分开表达 close_errors、is_alive 与 close 返回值。transport 永久阻塞时 timeout 返回失败，不宣称已回收线程。

证据：`tests/llm/test_observation_export.py`，本地 fake client；未验证真实 Langfuse 服务。

## P03：流暂停时错误继承 parent

风险与验证：ContextVar 若跨生成器 yield 保持激活，会让消费者的 sibling 请求误变成前一请求子调用。

解决：把 observation scope 限定在每次底层迭代内部，yield 前恢复消费者上下文。并发 r1/r2 探针四个 start 对应四个 end，根隔离；暂停流后的 sibling parent 为 None。

证据：`tests/llm/test_observation.py`。默认不记录正文、参数或异常消息，observer 抛错不能改变任务结果。

## P04：消融指标把“无回读工具”误写成“无证据”

发现：初版 Artifact off 直接发送完整原文，artifact_readback=0 只能说明未提供外置工具，不能推断信息丢失。

解决：分别记录 view_contains_full_source、artifact_readback_supported、artifact_readback_matches_source、evidence_recoverable 与磁盘开销。最终两组 evidence_recoverable 都是 1，视图从 120019B 变成 7041B，on 另外写盘 120361B。

证据：离线运行 `20261112T095857-2381042b458f` 的 raw.jsonl；不能换算成真实 token / 费用 / KV 命中收益。

## P05：开发依赖掩盖运行时缺依赖

触发：独立 Linux 环境安装框架基础依赖后导入失败 `ModuleNotFoundError: PIL`。开发环境因 matplotlib 的间接依赖而未暴露。

解决：确认 Session assets 与 image_processing 顶层使用 PIL，显式增加 `Pillow>=10` 并同步 uv.lock。所有 61 个已锁包版本保持不变，Pillow 仍锁 12.3.0；有少量锁格式 / marker 变化。Linux 安装 Pillow 13.0.0 后实际导入 Agent、Session、CodingAgent 成功。

证据：pyproject.toml / uv.lock、`uv lock --check` 与独立环境导入；不是靠开发全量环境再次导入证明修复。

## P06：预算耗尽被误记为正常完成

发现：live adapter 只捕获异常，Agent 正常返回的 budget_exhausted 终态会被写成 completed，外部 CancelledError 也会遗漏。

解决：订阅 AgentEnd 保存 stop_reason / limit_details，显式捕获取消并保留账本预留。账本 guard 放在调用前，不依赖可能被隔离的 observer 异常。

证据：`tests/experiments/test_benchmark_live_budget.py`、`test_benchmark_live_harbor.py`。预算是客户端保守预留，不是服务端不可突破的账单保证。

## P07：参考解通过必须与任务绑定

风险：任意 reward=1 JSON 或别的任务的 oracle 结果不能解锁收费模型调用。

解决：验证 agent=oracle、结束时间、无异常、reward=1、task_name；运行时再次匹配 Harbor Task.checksum。金标与测试字节仅参与 host 校验，不进入 solver prompt。模型 trial 使用新容器。

证据：C/D 单独预算与 Harbor 假环境测试；真实环境必须另外通过官方判分，fake 不能替代。

## P08：官方环境网络与依赖故障

实际过程：WSL 继承的 localhost 代理不可用；清理当前进程代理后安装 Harbor / SWE-bench 成功，但官方 DockerHub registry 直连超时。临时网络转发进程的启动被自动策略拒绝，未继续该方式。采用 Windows 标准 HTTPS 下载官方 crane 发布包并校验官方 SHA256，再下载原始官方任务镜像为 tar，WSL docker load 后核对 image config digest；没有替换任务或评分器。

后续参考解启动暴露未安装 Docker Compose v2，补装 Ubuntu 官方 docker-compose-v2 2.27.1。一次 CLI 退出码 0 的运行仍有 trial exception，必须读取官方 result.json，不能把命令成功当任务成功。

SWE 单例 gold 预检因镜像下载超时报告 completed=0、errors=1，属于环境阻塞。后续状态只追加，不抹去失败。

## P09：跨 Shell 参数影响环境准备

发现：WSL 内联 shell 的变量传递使任务复制未实际发生，首次 oracle 因任务路径不存在退出。

解决：环境准备改用显式 Python 参数列表、固定候选集合、每目录 task.toml 存在断言与持久 cache；不依赖跨 Shell 拼接变量。`/tmp` 在 WSL 重新启动后不可靠，实验使用持久目录并显式 TMPDIR。

证据：环境准备日志；这不是模型失败，也没有用修改评分器来规避。

## 后续维护

新增问题按触发、原因、修复、验证、剩余限制追加。没有原始失败日志的推测保留为风险，不能写成已解决事故；真实基准结果见独立官方报告。


## P10：官方镜像初始 HEAD 不等于题目 base commit

首次真实模型试验在初始化阶段被严格检查挡住，requests=0、无费用请求。原镜像HEAD为5efaed257...，题目指定cffd4e0...；官方评分脚本本来会先切回该base。

解决：在新建独立容器中先reset固定base，再核HEAD及clean状态，保持原image config绑定。新增偏移HEAD→reset→verify、reset失败证据和真实无模型Docker探针。之后两个真实trial各16请求，官方评分均resolved；不把零请求准备失败算模型成绩。

## P11：真实开关未激活与适配器缺少回读工具

实测后复核发现：两组真实SWE试验虽均通过，但on没有生成artifact文件，所以token差异不能证明Artifact收益。同时协议v1只给CoreAgent注册shell，却启用ArtifactStore，缺少模型侧artifact_read/search；生产CodingAgent已有工具，但这条独立评测适配器未复用。

解决：原始v1结果、源码与局限保留，标pipeline smoke；协议v2两组均注册相同生产受限工具schema，只切投影开关。新增14KB输出中部marker被预览隐藏→handle→artifact_read真正恢复中部/末尾的往返，off全文保留，回读不递归外置。SWE和Harbor两个适配均经fake provider验证，Linux全D17项通过；v2未重新发真实请求，不能挪用v1成绩。

证据：`docs/experiments/results/live-swe-20261112/`与`protocol-v2-offline/`。先确认机制确实激活，再判断数字是否有因果意义，是这次复核的实际改进。
### P12：原始评测输出与 Git 换行、空白检查冲突

- 复现：最终暂存后，官方 stdout 的终端回车和尾随空格触发 `git diff --cached --check`；Windows autocrlf 还可能使跨平台 checkout 的文件字节与 manifest 不同。
- 原因：原始实验材料被当成普通源码文本处理。
- 修复：用 `.gitattributes` 只对两组实验结果目录关闭文本换行转换，对官方 stdout 关闭 whitespace 检查，保留可查看的文本 diff；不清洗或改写原始输出。
- 验证：重新暂存后核对 index blob 与本地证据逐字节相同，再运行 staged diff check。manifest 继续绑定原始字节。
- 边界：不关闭项目其他源码或文档的空白检查；绝对路径和机器环境仍保留在原始记录中，不能当作可移植执行配置。
