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

## P13：SWE 入口只有两个硬编码实例

- 复现：任务批准、Docker初始化和报告直接读取同一个两题常量，新增第三题需要同时改多处查表。
- 修复：冻结官方catalog/TaskSpec，将安全metadata、base、原image/config与来源hash贯穿solver；旧两题fallback保持。
- 验证：第三题数据驱动回归、篡改输入拒绝、五个真实eligible任务的独立新容器probe通过；实际模型是否解决问题另看官方评分。
- 代价：数据批准和gold预检仍需宿主准备；catalog hash不等于第三方签名，通用入口不等于跑完300题。

## P14：取消评分时只停止等待，没有回收子进程

- 发现：另一助手交叉复核指出，取消await并不能结束评分器及Docker daemon中的容器。
- 修复：独立进程组、terminate/kill/wait，并按本次instance/run_id精确清理评分容器；不匹配其他任务资源。
- 验证：取消回归覆盖进程回收及容器匹配；增加同campaign跨进程排他锁，防止两个进程重复发起收费任务。
- 边界：已发生的供应商调用不能回滚；未知用量保留预留并停止，不能当作免费重试。

## P15：空补丁被自动汇总误标为评分执行错误

- 复现：v2首个真实配对中，两组各16次请求后都生成空model_patch。原官方报告均empty_patch_instances=1、completed_instances=0、error_instances=0；自动汇总却把所有非completed结果统一归为environment_or_execution_error。
- 更正：这两次是预算内没有产生修改，官方已知未解决，测试没有执行，不是评分环境故障。保留原报告、原state和错误分类作为历史证据，不重跑模型或评分来消除失败。
- 处理顺序：先停后续收费、核原报告及补丁字节长度，再按冻结顺序继续；汇总修复放在该批模型生成结束后，防止改源码破坏冻结校验。公共收集层可追加带来源的派生分类，不能覆盖原始记录。
- 边界：empty_patch、测试执行后unresolved、评分环境error与unknown须分开；已知结果数量与真正执行测试数量也要分开。
- 完成更正：在全部生成/评分/原始归档后新增swe_results单一parser，_score与summary共用；重算先核原report hash，旧缓存只审计不写回。实际复算10个报告=7empty/3resolved、3次测试、5个known pairs；同时覆盖错题ID、计数矛盾、缺字段、completed与error重叠及档案篡改拒绝。可移植archive CLI允许事后分析版本单独演进，run/score仍检查原source drift。

## P16：独立工作区可能导入原 checkout 的 editable 包

- 复现：Windows直接运行新CLI但不设PYTHONPATH时，导入了原checkout安装的旧包；只看当前目录或Git分支不能证明实际执行了新代码。
- 修复：Windows显式PYTHONPATH=src;.、Linux使用src:.；真实调用前核core/llm的__file__必须位于目标worktree/src，冻结输入与源码hash。
- 验证：主代理在正确路径下完成64项实验回归和12项分段执行定点回归；真实worker先核导入路径再允许进入Agent。
- 边界：源码hash可证明输入版本，不能证明供应商实际模型权重版本；模型与价格仍按请求/返回字段和供应商口径记录。

## P17：固定预算下空补丁较多，模型没有使用回读

- 本轮观察：160次请求、10个trial全部budget_exhausted；7次空补丁、3次官方测试通过。on外置11份，但read/search响应均0。两组均有空补丁，不能直接归因为Artifact。
- 当前处理：保持本批参数、题目和原始失败，不用增加步数或重试美化结果；保留生成版本与只读重算接口。
- 待验证：可以分别研究剩余预算提示、16/32请求上限、回读提示，每次只改变一个变量。尚未执行这些实验，不承诺它们能提高完成率。当前证据不支持直接扩成300题。

## P18：80毫秒整体预算测试依赖CI机器速度

- 复现：分析修复提交2857b9c的CI run43227265253，Ubuntu/Python3.11在prepare阶段的started断言失败。总0.08秒可能在初始化/文件I/O就耗尽，产品提前终止是合法行为；测试却假定目标阶段必然已经进入。
- 确定性证据：给测试前置输入hook增加120毫秒延迟，旧测试稳定复现started=False；没有修改生产deadline语义。
- 修复：只在参数化测试中注入RunBudget虚拟clock与run_lifecycle局部asyncio.wait代理，前置事件可完成，目标await进入后再推进原0.08秒deadline。仍走生产cancel/gather/finally，断言started、stopped、budget_exhausted/max_seconds和owner释放。
- 复核补充：独立审查指出前置阶段可能意外挂起，再加5秒测试watchdog及finally a.aclose；watchdog不是被测产品预算。
- 验证：主代理Windows定点15 passed in1.08s，执行助手Linux同版15 passed in1.43s，Ruff与独立review通过；本地未运行Python3.11，需GitHub最终矩阵验证。实验生成源码、raw报告和费用均不改，未重跑模型。
