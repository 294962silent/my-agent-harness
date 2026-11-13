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
- 后续实际复核：47a89aa的GitHub run43228948231四组全部成功，包含原失败的Ubuntu/Python3.11。原失败run链接与复现仍保留。
- 再次更正：文档head f0d12c1的run43229586859暴露同文件另一项30ms模型等待假设：模型可能尚未进入，不能无条件断言closed。整份15项随后统一审查；model_wait、四stage、已交付工具意图的deadline都改用共用deadline_at_stage fixture，工具意图需等消费者实际收到MessageUpdate后才arm。唯一保留真实30ms的early-deadline用例只断预算、模型未启动与owner释放，允许在任何前置位置过期；0.5s close/5s收集限制均是同步后的防挂watchdog。
- 最终该文件本地验证：主代理Windows15 passed in1.21s，作者Linux同版15 passed in1.59s，Ruff及整份独立复核无阻断。没有只靠增加timeout或反复重跑掩盖失败；最新提交CI通过状态以PR checks和PR正文实际记录为准，历史47通过不替代新head验证。

## P19：回读能力成功，不等于最终任务完成

- 复现：两份隐藏中段nonce日志，各做投影on/off。on均由生产工具回读到目标，第四次响应却仍在调用工具，四请求上限结束后未交付正确最终答案；off两次均在第二次正常答对。
- 处理：把隐藏预览、handle、真实回读、目标进入模型输入和最终精确答案分别记录。on能力链路2/2、端到端0/2；不把前者当成功率，也不追加请求消除失败。
- 证据：四trial/12个已结算请求/$0.009106，schema一致、无提前泄漏；独立助手核audit/result及Agent.run预算终态。审计只在SDK前Provider入口，正文/推理/finish reason未保存，不能解释继续读的动机。
- 代价：回读引入额外请求；“少发工具正文”只有在完整任务质量、总请求和费用一起测量后才有价值。该探针不是SWE或KV缓存排名。

## P20：Windows worktree的Git指针不能直接交给Linux Git

- 发现：运维脚本在WSL执行git rev-parse时，`.git`中Windows绝对路径`C:/...`被当成Linux相对路径，模型启动前失败；消费未增加。
- 处理：在Windows宿主核完整提交与源码无脏改，Linux核真实包导入路径和冻结输入/源码hash。保留共享Git文件原样，避免破坏Windows工作树。
- 边界：提交名不能替代实际导入检查，文件hash也不代表服务端模型权重版本。本轮生成版本固定c69ff37，文档和事后分析版本单独标注。

## P21：Mem0 SDK close不等于全部存储客户端已关闭

- 发现：真实固定2.2.1 SDK的Memory.close仅关SQLite，Qdrant本地锁仍在，关闭后同路径重建失败。
- 修复：适配器只关闭自己拥有的SQLite、向量和HTTP客户端；显式注入默认caller-owned。SDK工作失败时也走finally清理，完成与request_close共锁；工作失败和资源关闭状态分开，避免已清理后永久False阻断会话替换。
- 验证：生产适配器→MemoryStore→真实本地Qdrant/SQLite，假embedding、LLM拒绝调用、socket拒绝网络；关闭后同路径重建成功。单worker在途超时仍False，不声称杀掉SDK请求、内部重试或做到exactly-once。

## P22：外发集成配置不能被项目文件覆盖

- 发现：新增Mem0/观测配置包含服务地址和凭据环境引用，原项目级合并规则只保护proxy/trust，新块如果直接合并会越过既有特权配置边界。
- 修复：memory_index/observation加入既有全局专属键，项目整块剥离；普通theme覆盖与受信任Python显式注入保留。示例配置放可信全局目录。
- 验证：新增4个有/无全局×有效/畸形项目块参数例先红后绿，Settings全13项通过；主代理最终整套也通过。不新增交互审批流程。

## P23：Dream名为OptimalPolicy的模板没有实现

- 发现：固定官方SHA的模板solve仅reset并返回空SimResult，不能根据类名宣称实际策略已运行。
- 修复：首版只接真实可执行的官方baseline，标记fixed_reference；删除无效beta控制项，不加载任意候选Python文件。输入必须有真实评分树，Session不能编成评分树。
- 验证：生产CLI→隔离worker→真实固定SDK的合成三节点契约，校验决策/预算/来源/manifest；未启动在线学习、不报告策略收益。

## P24：共享LLM的订阅与清理所有权

- 问题：覆盖llm.observer会破坏原观察者，会话重建若保留旧sink会重复导出或向关闭的client写入。
- 修复：每调用快照base与订阅，逐观察者隔离异常；runtime只detach/关自身。RPC四种session变更先await旧Agent关闭，未完成就不假装替换成功；直接core与CodingAgent异常/取消路径均保留清理。
- 验证：定点生命周期/所有权回归及真实Langfuse5.2内存SpanExporter；缺字段不补零，cached输入按total-completion映射。没有真实服务上传回读证据。

## P25：发送SIGKILL早于进程组真正退出

- 发现：ec0fcdc的Linux CI在normal-exit清理测试中观察到后代仍为R，Windows同head通过。Linux本地原样复现，返回时ShdPnd=0x100（SIGKILL排队），约1.25ms后进程消失；原实现只wait根进程。
- 修复：生产Unix清理增加有界整组静默确认，Z/X视为不再运行，无法确认则报错；不用固定sleep延长测试，不引入全局subreaper。Windows已有Job Object active-count等待保持。
- 验证：受控True/True/False存活序列、永不确认失败、/proc comm及Z/R解析和真实子进程回归；Linux、Windows各20项Dream测试通过，互审无阻断。

## P26：后台任务可能在首轮内合法完成

- 发现：同次Ubuntu/Python3.11 E2E首答为Completed而非Waiting；旧测试用0.1秒任务sleep假定首轮结束早于通知。让第2次假模型调用显式等待真实job后，Linux稳定复现旧断言失败；生产loop会在阶段末收割followup并继续同轮，是合法行为。
- 修复：仅测试改为release文件gate，先保证后台仍running，再释放并await真实job，下一轮验证完成通知、输出与任务状态；finally取消/回收并关闭Agent。取消固定0.1/0.3秒阶段sleep，也不假定通知出现在第二轮第一次provider调用。
- 验证：Linux/Windows定点各1项通过；生产后台调度未修改。主代理修订后24项定点（含三个真实SDK契约）通过，不是新增benchmark。

## P27：Mem0原FastEmbed provider忽略CPU/缓存控制

- 发现：固定Mem0 SDK只把model_name传给TextEmbedding，cache/threads/providers配置未转发；FastEmbed默认AUTO不能假称受控CPU。
- 修复：固定SDK版本的局部装配层显式CPUExecutionProvider/cuda=False/2线程/本地路径，NeverLLM不构造OpenAI；7个官方固定权重文件逐项独立验hash，运行期不下载。可写候选缓存按adapter独占，关闭释放强引用和自身目录。
- 验证：真实512维中文BGE经生产链路，禁词法fallback、拒绝socket仍检索成功，实际ORT provider仅CPU；模型弱引用在关闭后释放。功能smoke不是召回率实验，冷构造也不在search等待超时内。

## P28：官方模型下载失败与匿名代理边界

- 发现：直接urllib/curl TLS EOF或超时，WSL该域不可达；官方元数据可核不等于本机权重已下载。
- 解决：仅显式采用Windows系统匿名代理，禁curlrc/netrc/HF token，TLS不降级，官方固定revision下载；ONNX按官方LFS SHA256，其余按官方Gitblob SHA1及字节数复验，再原子入缓存。空用户名但带口令的代理也拒绝。
- 结果：7文件95332206B准备成功，不用镜像；缓存manifest哈希可复核。未把假向量当下载成功，模型留用户缓存不进Git。

## P29：自动晋升必须比较当前active并防并发覆盖

- 发现：候选胜过baseline仍可能弱于已有active；仅FileLock原子写不能避免训练期间另一个晋升/rollback后被过期报告覆盖。
- 修复：冻结incumbent版本/参数/完整指针token；holdout只额外验证固定incumbent，候选须严格胜两者。锁内重核proof和CAS，revision nonce防ABA；同参数already_active不重发。先写中性prepared产物、指针最后提交，提交状态以registry为准，不宣称故障下exactly-once。
- 验证：临时fixture/注入评估器覆盖低于active、prepare后active变化、ABA、损坏/重封hash、合法rollback；真实SDK合成闭环永不生产晋升。没有实绩策略收益证据。
