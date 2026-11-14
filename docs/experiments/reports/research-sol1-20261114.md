# SOL-1 CPU CSV 研究任务实施记录

日期：2026-11-14。工作区：interview-redesign。未调用真实模型 API，未提交或推送。

## 已实现

- `research_task.py`：新目录 prepare/load/verify，CSV固定种子 train/dev/final，source仅std库均值基线train.py；dev/final标签保留宿主，数据与manifest SHA校验。goal仅描述schema和容器路径。
- `research_evaluator.py`：校验预测CSV完整唯一ID、有限数，宿主重算RMSE和utility=1/(1+RMSE)；不读stdout分数、不反序列化pickle。非法预测score=None。final独立单次方法，调用方负责禁止回流。
- Docker固定镜像ID/摘要、禁止拉取、无网络、RO根、非root、cap-drop/no-new-privileges、CPU/内存/pids/时限。三份RO单文件输入，仅predictions.csv可写并限16MiB。原生Docker优先；Windows可用本机WSL前缀，bind/cidfile转/mnt盘路径。容器带唯一owner label，按CID清理；create取消时仅核实owned label后兜底清理，清理不确定明确阻断。
- `rsi_branch.py`：可选五工具白名单和候选文件allowlist，在工具调用、评估、检查点、导出核验，仅允许train.py；默认分支行为保留。
- `rsi_runtime.py`：显式TrustedEvaluator协议、有限连续utility校验；连续指标不生成二元成功数量，非法候选不伪0；保留默认退出码评分和final_success=None。

## 验证

`uv run --no-sync python -m pytest tests/coding/test_research_task.py tests/coding/test_research_evaluator.py tests/test_rsi_runtime.py -q`：58通过。

`uv run --no-sync ruff check src/my_coding_agent/research_task.py src/my_coding_agent/research_evaluator.py src/my_coding_agent/rsi_runtime.py src/my_coding_agent/rsi_branch.py tests/coding/test_research_task.py tests/coding/test_research_evaluator.py tests/test_rsi_runtime.py`：通过。

这些是离线协议/安全边界测试，Docker命令部分使用替身，不代表真实容器已验证。真实固定镜像smoke等待主代理提供镜像。checkpoint仍是树/session快照，不代表可靠恢复；跨轮账本与恢复由SOL-2负责。完整云观测、Mem0效果和真实模型改进未验证。

## 追加：真实本机容器验收

随后主代理提供官方Python固定镜像：`sha256:896edfbcbf0c29ad15681617a8886cb9871b7f24c5fde4606114c8ee6bfe4c70`，linux/amd64 manifest为`sha256:b701198cb708b68d9413ca67d6311bd97c9389a2c5fe4da0511fc652642366cd`。执行前缀为`wsl.exe -d Ubuntu-20.04 -- docker`，这是本机WSL，不是L20；全程标准库、零模型API。

实际命令：在工作区设`PYTHONPATH=src`，运行`uv run --no-sync python <common-git>/codex-delivery/research-runtime-20261114/sol1-smoke/smoke.py`。40行合成线性数据用于边界验证：均值RMSE22.0705938097、utility0.0433452216；合法线性RMSE0、utility1；stdout伪报RMSE0/utility1，实际宿主仍给RMSE48.8159809898、utility0.0200738795；NaN和错误ID均invalid且score=None；容器只见两份公开输入、私有labels不可见、额外output文件写入被拒；0.5秒超时和2秒取消均清理。脚本逐个读取自己的cidfile，并用Docker inspect确认所有容器不存在，8场景断言通过。

第一次临时脚本写入把worktree `.git`误当目录而失败；查明它是gitdir指针后，改用common-git私有目录，未影响生产文件。初始日志保留在私有证据目录。

## 追加：真实UCI数据场景预检

另运行`uv run --no-sync python <common-git>/codex-delivery/research-runtime-20261114/sol1-uci/smoke.py`。输入为主代理下载的UCI red wine规范化CSV，1599行、target=quality、seed23；961训练/319开发/319最终，未读取最终评分。数据SHA256：`d22f4f11db0456ff21745d2fa96d26fa348a8cdb889efda65bde802085be45fd`。

- 均值基线：开发RMSE **0.8467093297**，utility **0.5415037353**；执行代码字节SHA `cba011056a67daf4847579e4452675a8f42063cf3ca3178abc5e55c7187fa94f`。
- 手写标准库ridge（训练均值/标准差归一化、alpha1、Gauss-Jordan求解）：开发RMSE **0.6841778884**，utility **0.5937615064**；代码字节SHA `b25c63989c1aa48468c65ce852eb1aafe8331e1b9519abba592c74dfd27c2978`。

这证明固定真实数据上存在非饱和、可区分的连续反馈，不是Agent学习效果或真实性能提升。完整task/features/verifier/image/代码pins及预测留在独立私有目录`<common-git>/codex-delivery/research-runtime-20261114/sol1-uci`。私有结果的`pins.code_sha256`是执行字节摘要；最初`script_sha256`是换行转换前文本摘要，两者在Windows下不同，不应混用。

## 追加：优化器与观测接缝

`dream_rsi_optimizer.py`改为宽深交错候选，按全部训练任务及预算上的实际节点序列签名去重，包含与baseline等效的去重，然后才截上限；不查看holdout形状/分数。`max_candidates`为非baseline候选上限，去重后可少于上限甚至为零。baseline始终参加，分数并列仍优先baseline。

新增`search_order=fixed/random`与固定seed；random仅打乱唯一参数候选的搜索顺序，不是随机Agent探索策略。固定和随机使用相同非baseline上限及预算。默认`ratio`保留原评分目标；可显式使用`ratio_cost`：宿主验证的ratio减`cost_weight × evaluator_calls/budget`。调用次数仅为重放成本代理，不能当真实token、金额或墙钟时间。旧plan的候选重建分支保留；原实现SHA完整性校验仍在，旧快照不能被新代码静默解释为新实验。

直接fixture回归中，同budget2、完整两份唯一候选池，fixed/random均选宽1深1，训练ratio1，baseline ratio2/3；成本权重1时选宽1深0，体现质量与调用数的取舍。这是离线fixture机制验证，不是真实研究效果。原来依赖旧网格第3项必为深4的promotion测试显式扩大候选上限，以继续测试真实的registry回滚/竞争边界，而不是固定旧候选序号。

`rsi_branch.py`在restricted模式显式关闭重复observation装配；`SharedLLM`在原始provider调用外提供budget root_id及task/round/stage/branch/attempt安全scope，保留原始provider类型和原purpose。没有把代理类替身伪装成真实provider。

合并验证命令：`uv run --no-sync python -m pytest tests/coding/test_dream_rsi_optimizer.py tests/test_rsi_runtime.py tests/coding/test_research_task.py tests/coding/test_research_evaluator.py -q`：**80通过**。对应修改文件Ruff通过。源码冻结后由主代理启动真实API。

## SOL-2只读交叉审查

已将三处行动项交SOL-2处理：内存manifest原地变更需要同样校验固定摘要；恢复时除checkpoint/report还应验证选中预测产物；final应核对已冻结的实际imageID。另提醒round observation scope接线。已核请求reservation先持久化再外发、未知用量保留并阻断、累计预算恢复、final_started一次闭锁；未替SOL-2修改其文件。

后补全部训练任务/预算签名覆盖、零有效候选仍保留baseline、成本目标promotion/verifier闭环回归：相同合并命令现为**81通过**，Ruff通过。生产研究源码无进一步变动，等待SOL-2修复后复审。

SOL-2修复后已复审：内存manifest、全部预测产物、选中score/code pins与checkpoint metadata、final实际imageID、round observation scope均已补。研究campaign对平分/更劣Agent候选保留baseline，checkpoint与score来自实际chosen，原始Agent候选id/score另存；原trace不改写，没有把保底冒称Agent收益。累计请求/评估状态与预算保持，未知用量/清理失败/未完成评估仍阻断。未见新增阻断项；评估前预算耗尽导致baseline=None时建议转受控完整性错误，现状也会fail-closed不提交。全库最终回归与真实模型执行由主代理负责。


## 追加：资源故障后 SOL-1 只读边界核验（2026-11-14）

本轮未修改源码、未运行 pytest、embedding 或模型 API。局部核验 rsi_runtime.py、rsi_branch.py、dream_rsi_optimizer.py 及对应测试选择，未发现新增阻断：有效连续 utility 要求有限数，非法评估保持 score=None，连续分数不折算成功计数；存在非法 attempt 时不导出优化器 trace。受限分支关闭观察装配，仅保留文件工具，调用前拒绝非 allowlist 文件、越界和 reparse 路径，导出时再次校验文件清单。这是工具权限边界，不能称为 OS 沙箱。

候选签名使用全部训练 trace 和预算的执行节点，不读取 holdout 形状或分数；先排除 baseline 等价签名并去重，再截断或按种子打乱。baseline 始终参评；holdout 仅验证已选政策及冻结参照/现任。旧 plan 缺省 candidate_selection 时重建 legacy grid，但 _derive 仍要求 implementation SHA 一致；兼容旧候选编号，不承诺旧产物跨源码版本可晋升。未单独执行历史 plan 验证。

建议主代理串行、每组独立进程落盘 stdout/stderr 和退出码：①恢复后重跑此前 81 项四文件定向回归；②SOL-2 runtime/CLI/checkpoint/策略证据与 closed-loop 回归；③已有安装 SDK 的离线契约单独跑 test_dream_rsi_sdk.py、test_langfuse_sdk.py、test_mem0_sdk.py 和 test_mem0_local.py::test_pinned_local_assembly_real_sdk_no_keys_or_network，明确记录 importorskip，不选择真实权重 smoke。未完成整库 pytest 不视为通过。

打包需 npm ci、npm run build 后 uv build --wheel，核对 wheel 含研究 task/evaluator/runtime 模块及 CLI entrypoint；既有 CI 的 wheel 显式清单尚未列出新增研究模块，可在本次产物验收追加核对。CI 配置矩阵为 Ubuntu/Windows × Python 3.11/3.13，SDK 专项仅 Ubuntu 3.13；配置存在不等于当前分支 CI 已通过。本轮仅审配置，未打包或跑 CI。

前文已留存 8 项真实 Docker smoke 与 UCI baseline/ridge 预检，不证明 Agent 学习效果。真实 red run 据主代理记录为 1 次已结算 API、1252 tokens、1 次 baseline evaluator 后 MemoryError，0 round commit；本助手未重跑且未复核全部 live 原始产物，不报告真实链路成功。资源故障后的整库结果不可用，最终状态待主代理串行回归补齐。
