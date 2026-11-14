# 主张与证据边界

初版日期：2026-11-12；当前证据更新：2026-11-14。用于检查项目介绍 / 简历 / 面试回答是否有可追溯依据。下方历史批次数字按各自版本解释，不代表新增实验后的当前总量。

## 2026-11-14 代码实验首版：可用工程证据

- 已实现固定CSV任务、受限Docker预测、宿主RMSE评分、基线保留、持久预算与完整round检查点。真实DeepSeek red-wine已演示第一轮暂停、新进程恢复第二轮、独立final；v2为24请求106826tokens、8dev+1final评分，final valid=true/RMSE0.7554124335，最终保留均值baseline。
- v1失败另保留1请求1252tokens和1评分，合计25请求108078tokens/10评分。两次调用用量均已结算、held=0；金额未核保持null。v1 MemoryError具体根因未证实，v2是已登记的事后运维修订，不是原失败被修成完成。
- 两round各2个候选触轮次上限、1个正常结束，没有完整可导出的评分树；未形成合法train/holdout优化数据，optimize阻断、无晋升。不能把本次写成RSI学习提升、Agent训练优于均值或长程benchmark成绩。Airfoil/white及三组同预算质量消融未启动。
- Mem0本地CPU第二轮真实检索到1个有效语义候选、3次embedding调用、无词法回退，支持该单经验功能接线；不支持优于词法或改善质量。观测保留本地调用记录；Langfuse云上传仍未验证。
- 稳妥项目表述：“围绕多轮代码实验构建Agent执行框架，将容器内候选执行与宿主可信评分分离，实现累计预算记账和完成边界跨进程恢复，并保留失败与独立最终评价证据。”个人负责模块、时间和掌握程度须据本人实际参与确认，见[简历稿](RESUME_READY.md)与[事实账本](research-career-claims.json)。

本批报告见[真实流程记录](../experiments/results/research-agent-20261114/LIVE_REPORT.md)，来源/预检/失败/成本各自留档。完整CI按实际提交的run核验，定向测试、真实Docker八项、实际SDK和真实模型结果不互换。

## 2026-11-14 真实RSI校准的证据更新

- 已用真实 `deepseek-v4.1-flash` 跑通 CodingAgent 工具调用、分支评分树、实际 Dream SDK 离线搜索和冻结在线参数消费。任务为固定官方 HumanEval 短代码子集，不是长程任务或完整SWE成绩。
- collection完整交付4/5，val32未形成合法trace；事后、首test前登记available-tree探索性恢复。SDK在完整3个train上选择baseline，未学出新参数；holdout仅1/2有trace，完整验证拒绝、未晋升。C为预先规定的固定reference fallback，与B同参数，**学习贡献不可识别**，不能写成“学习提升0%”或“RSI优于固定策略”。
- 首轮test停止态为10完成、1权限失败、37未启动，科学比较未齐，不能排名。实际新campaign用量完整：298请求870205tokens；连同v1和连通准备共303请求878756tokens，币价未核。退出文字误报unknown已按journal更正，旧账本unknown仍单独保留。[逐槽停止态及限制](../../experiments/context_rsi/reports/live-v2-analysis-stopped-20261114-r1/REPORT.md)。后续若续跑另行记录，不覆盖此时快照。
- 最终执行覆盖已齐：精确运维例外续原37未启动槽，全部完成；48个test为47正常交付且final通过、1原权限失败未执行final。A/B/random各12通过，C为11通过+1未评分；不是pass@1或长程成绩。含所有准备成本974请求2875023tokens，全结算、0unknown/held、金额null。原失败schema指纹缺失，严格完整比较未生成排名；C仍与B同参数，学习贡献不可识别。[最终报告](../experiments/results/rsi-live-20261114/REPORT.md)给出原分母、成本与26项独立审计。
- 可用表述：“完成真实模型驱动的受控策略优化链路校准，建立任务内容身份、独立评分进程、付款前持久化与完整失败分母；实验发现验证覆盖不足和baseline选择导致学习干预缺失，未据此宣称策略收益。”具体问题见[真实RSI数据与评分深挖](12-真实RSI实验的数据与评分可信度.md)。

## 2026-11-13 当前结论

- 固定历史Memory三臂30题已完成：none4/30、Mem015/30、词法22/29有效（另1判分不可用）。英文历史+中文BGE，不支持当前语义配置优于词法。八道词法对/Mem0错中三题标注会话覆盖不足、五题覆盖完整；抽查其中两题证实Mem0漏答案chunk。27517条重建索引文本最长463tokens，无512截断证据。见[误差分解](../experiments/results/long-horizon-20261113/memory-error-analysis/REPORT.md)。
- Dream-RSI 已补齐显式串行 runtime：真实 CodingAgent 产出评分分支树，冻结参数控制下一轮分支，可从 registry 读取 active。真实工具与本地测试 + SDK dry-run 的四臂闭环已跑；模型 scripted、quality=unmeasured、未生产晋升。接线缺口已修，真实模型收益未测；TUI 流式 RSI 暂不支持。
- 本机新增固定 Qwen3-Embedding-0.6B GPU profile。中英文两事实检索、禁词法回退、1024维、资源回收与配置装配已验证。旧 BGE 的可选 BM25/NLP 是否在30题触发未记录，不能无条件称旧臂纯稠密；新 profile 的 dense-only 行为须作为比较变量。
- 长程代码质量尚未证明：本批Terminal首组科学pair0，off有验证器基础设施故障，on测试原reward1但Agent超时/末请求用量未知；EVO仅完成预检/接缝契约，无模型生成成绩。当前支持工程机制与停止保护的证据，不支持长程成功率优于基线或RSI带来提升。
- 付费阶段已按未知用量协议停止。最新原账本533请求、已知$1.218608、另未知预留$0.045131；不是完整实际费用。原始失败与历史账本不覆盖。

## 当前可支持的说法

- 为 Python Agent Harness 实现单运行归属、可选轮次 / 时长预算、取消与流关闭清理；用中断后 Session 重载验证工具调用闭合。证据：Runtime 源码与针对性回归。
- 在统一模型门面增加五入口生命周期观测，关联主请求与辅助调用，提供有界导出与可选 Langfuse 装配；订阅保留已有observer，真实5.2 SDK内存导出已验证，远端上传未验证。证据：本地记录、真实SDK离线契约及失败回归。
- 为八模块建立统一离线机制评测协议：12 个独立场景、五次重复共 100 条原始观测、断言失败 0。确定性重复不是新增任务，off 预期暴露缺陷也可以满足夹具断言。
- 在 120019B 固定工具输出夹具中，外置产物把发送视图变为 7041B，两组都能恢复全文；磁盘开销和回读成本另计。只支持该夹具的体积结论。
- 用真实注册表与权限模块复现独立工具并发 1→3、读写依赖违例 1→0、未授权写副作用 1→0。单场景不能泛化成所有工具或安全攻击通过率。
- 框架重设计首轮Windows整套测试1112 passed、2 skipped、1 deselected，终端96 passed、2 skipped；后续SWE扩展/统计修复的实验集回归80 passed、2 skipped、5 subtests passed。批次有重叠不累加，也不是模型任务完成率；跨平台CI按对应提交记录。

- 已完成v2固定小样本：6题中5题gold可用，on2/5、off1/5，7次空补丁、3次实际测试通过。160请求保守账本$0.503462；on11个artifact、回读0，cache_read仅158/160覆盖。支持真实投影/预算/官方评分链路，不证明稳定质量、费用或KV提升。详见[报告和原始证据](../experiments/results/swe-v2-pilot-20261112/REPORT.md)；历史v1单例另保留。该SWE批次没有观测到真实回读。
- 后续[隔离日志探针](../experiments/results/artifact-capability-20261112/README.md)观察到两次真实回读：隐藏中段进入后续Provider输入；但四请求上限内on最终答对0/2，off为2/2。共12请求/$0.009106，SDK前元数据审计，不是wire抓包。支持回读链路可调用，不支持端到端收益，也不能替代SWE任务成绩。
- [三题预算提示对照](../experiments/results/swe-budget-visibility-20261112/REPORT.md)两组均1/3通过、共4空补丁，96请求/$0.307190；两组投影恒on，100/100响应后工作树观察available，read/search仍0。静态提示保留为可选且默认关闭，面板没有提供成功率改善证据。累计消费$0.888355，后续benchmark依用户新指令暂停。
- [三项可选集成](../integrations/README.md)已具备安装/配置/调用入口：Mem0候选索引经本地有效事实重验、超时熔断及owned客户端清理；Langfuse全局装配、原observer保留及缓存用量映射；Dream真实固定SDK的参考策略回放与进程树清理。适配初版ec0fcdc的本地all-extras回归1254 passed、2 skipped、5 subtests passed；后续Linux CI修订的定点24项通过，最终跨平台状态见PR checks。SDK契约不是效果消融，重叠批次不累加。
- 后续本地记忆与受控参数闭环：真实BGE CPU模型经生产Mem0Index检索，临时禁词法回退并拒绝socket仍通过；参数优化固定候选、train选择、独立holdout验收，晋升严格胜baseline及active并用CAS防覆盖。真实SDK合成CLI演练completed、promoted=false；当前无实绩数据，状态needs_dataset。主代理本地整套1302 passed、2 skipped、5 subtests passed，不是语义/任务质量成绩。
- 发现并修复官方empty_patch被误标为环境异常的统计问题；保留原报告和旧state，另存派生分类并提供可移植离线重算。生成版本765与事后分析修复版本分开，不通过重试或改题消除失败。
- 本轮为探索、摘要和评分建立共享父预算，缺 usage 保留 reservation 并停止派发。独立 final 反馈不进训练/选择；同 ceiling 四臂 scripted 复跑通过，A/B/random 各12次脚本调用，C为6次，仅为夹具行为，不能写成节省50%真实成本。首次Windows all-extras1424通过后，CI抓到默认可选依赖/ Linux清理问题；修后最终整库1425 passed、11 skipped、5 subtests passed，干净默认记忆协议7通过/3skip。不是任务成功率，详见[修复记录](../experiments/results/context-rsi-20261113/ci-repair/REPORT.md)。
- 简历可用表述：“为 Coding Agent 实现受控策略闭环，将分支执行、独立评分、评分树导出和冻结策略消费接通，统一请求/token/评估预算，并建立固定与随机策略对照及可复查实验档案。”责任、取舍与追问见[闭环工程深挖](11-RSI闭环与本地推理工程深挖.md)。

## 尚不能支持的说法

- “SWE-bench / Terminal-Bench 全量达到某成功率”：需要固定数据版本、环境、官方结果、费用及完整失败记录；本轮先做小样本和环境验证。
- “消融证明整体 Agent 提升 X%”：必须说明任务、对照变量、随机性、样本与费用；脚本 provider 无法证明真实模型质量。
- “记忆召回达到 100%”：原2条标注查询只是smoke；后续30题固定历史实验中26题证据会话Recall@8词法0.86538、Mem00.73077，且会话命中不等于具体答案chunk命中。不能借论文成绩宣传本项目。
- “KV cache 命中提高 X%”：请求字节与本地前缀指纹不是服务端缓存读 token；缺失字段为 unavailable。
- “后台任务保证持久恢复、exactly-once 或强制抢占”：当前进程任务与协作取消不具备这些能力。
- “Langfuse 已真实接通”：已完成配置装配、fake client及真实SDK内存导出，不能替代真实服务上传后回读确认。
- “Dream-RSI已经学出更优Agent策略”：执行与参数闭环已实现；后续真实模型HumanEval校准选择了baseline，C回退为固定reference，不提供学习贡献或真实长程收益证据。未做任意代码自改或权重训练。

## 本批与既有工作的区别

Session 树、工具并发门控、权限、canonical 摘要、产物回读和基础长期记忆已有实现；本批重点是生命周期与观测的统一、缺口修复以及评测 / 证据协议。历史 README 中的旧基准与性能数字不自动适用于新框架，必须链接原始记录并标注版本。

## 面试使用

介绍时先给一个实际失败及修复，再展示公开接口、测试或原始记录。个人贡献按实际设计、实现、复核与实验分工描述；不以阅读 AI 输出代替源码理解，不编造自己做过的线上运维或独立实现。

## 2026-11-14 场景收敛提案（尚未实现）

GitHub选型后，推荐以AIDE式多轮代码实验为主场景，借鉴autoresearch固定实验预算、Ralph/Anthropic外部状态协议与Hermes经验复用，保持既有Harness内核。提案、固定版本及核验边界见[场景研究](../research/2026-11-14-scenario-selection.md)。

拟补数值评分、受限任务adapter和场景checkpoint；Mem0场景收益、真实Langfuse服务回读、RSI在该任务的质量/成本收益均待验证，不进入已证实简历数字。可先使用既有分支执行、预算/记账、上下文/产物、记忆/观测适配的真实工程事实；个人职责仍按实际参与及源码掌握程度确认。

## 2026-11-14 追加｜新十二题 Memory 事实账本

| Claim | 当前可核验状态与证据 | 不能推出的结论 |
| --- | --- | --- |
| 受控 Mem0 索引与 gold 隔离 | 固定匿名 800 字符 chunk、top8 / 6000 字符；infer=False；WSL seccomp；36/36 valid / close_complete，见 [汇总](../experiments/results/memory-effect-20261114/effect-summary.json) | 完整自动抽取、更新、巩固质量已经验证 |
| 新样本真实三臂回答效果 | 与旧 30 题无重合的新 12 题；none 4/12、lexical 11/12、Mem0 9/12；各 12 个合法 yes/no judge；length/invalid/unavailable=0，见 [配对](../experiments/results/memory-effect-20261114/effect-pairs.json) | Mem0 优于词法或整个产品普遍提升；官方判分模型成绩 |
| 持久累计预算可审计 | 72/72 请求 settled，36,211 prompt + 5,641 completion = 41,852 actual tokens，held/unknown=0；先三请求核账后续跑，没有重置或重试 | USD30 是实际花费；未知 usage 可忽略或自动补跑 |
| 不可变 plan / 可变 ledger 分离与字节复用 | v2→v3 仅改 ledger_path，24 个 result/state 原字节；旧五题保持 hash，无重建索引，见 [修订证据](../experiments/results/memory-effect-20261114/bookkeeping-amendment.json) | 老 v2 布局原本安全；任意中断都自动恢复或实现 exactly-once |
| prepare-time 路径守卫已补 | 实验结束后拒绝 ledger、.lock、.study.lock 对 plan 的内含/相同/祖先冲突；七个失败回归转绿，专项 22 passed / Ruff；13 项旧源码私有封存 | 此守卫已经用于本次 72 请求；可改写旧 source pins 来让历史校验通过 |
| SessionRecall 与冷启动成本口径 | session recall 分母 8 题，四个 abstention unavailable；Mem0 0.84375、lexical 0.8125；平均冷检索 99.9319s / 4.3291s | session 命中就是 answer evidence；冷建库/索引时间就是 warm 查询性能 |
| 观测与 RSI 边界 | Langfuse 本地 SDK 21 项验证；云上传/读回 pending。RSI v2 STOP 保留，v3 artifact_budget_v1 开发/验证中 | 云端已验证；RSI 已完成训练、晋升或独立效果收益 |

根代理已独立读回/重算本批 36 行、12 题、准确计数、41852 tokens、answer→judge hash 和公开清单，属于完整性验收，不是新增人工重判。来源 [DS-2 报告](../experiments/reports/effect-v1-sol2.md)；个人职责与掌握程度须本人确认。原历史实验及失败记录不覆盖。

### Memory 最终验收补充（2026-11-14）

| 实现技术／主张 | 可核验证据 | 限制 |
| --- | --- | --- |
| 拒答与事实回答分开核对 | 冻结 references.is_abstention：三臂拒答4/4；非拒答 none0/8、lexical7/8、mem05/8；与本批官方suffix逐项一致 | none总计4/12不能作无历史事实问答能力 |
| 命中 session 后仍核对实际支持块 | 两题 lexical-only correct 内部抽查：Mem0命中session但漏支持chunk，均未达到6000字符上限；另有共同错例 | 三例定向人工检查，非全量证据召回率；没有保存全部pre-top8排序 |
| prepare 拒绝账本及真实锁侵入 plan | 首次22项后根发现漏 .tokens.lock，新增2红例修复；作者及根最终24专项通过，Ruff通过；真实随机临时文件写在ledger.parent并验证 | 工程修复发生在研究结束后，旧study source pins及科学结果未改 |
| 实验时未提交源码仍可审查 | 13旧pins逐项比对base0b63386，8由Git取得，5有公开source-only快照及SHA/bytes清单 | 核对本机remote-tracking历史，未联网fetch；不发布raw/gold/auth或声称一键免费复跑 |

最终结果仍为4/11/9（各12题）、72 settled／41852 actual tokens，length/invalid/unavailable均0；新增诊断、回归和归档不新增API或embedding。原22项是中间过程，当前最终数量24。source快照见 [manifest](../experiments/results/memory-effect-20261114/source-snapshot/manifest.json)，匿名诊断与分项见 [diagnostics-manifest](../experiments/results/memory-effect-20261114/diagnostics-manifest.json)。

### Langfuse 云端接通 Claim 修订（2026-11-14）

| 可说的技术点 | 可核验证据 | 必须保留的限制 |
| --- | --- | --- |
| 显式 owner 将 raw LLM observer、GuardedLLM 持久账本、single worker flush/owned shutdown 接通 | 新 observation.py/observation_probe.py；新入口真实 56 tokens，官方 CLI HTTP 200 按 trace ID 读回，source SHA 一致；17 项离线回归/Ruff 通过 | 不把开启全局配置等同于所有独立实验自动接线 |
| 稳定 campaign session 与匿名 arm/stage/task metadata | 新 generation 的 sessionId 与四项 metadata 远端匹配；公开 verification.json | 仅单 generation，不宣称已实测实际多轮/跨 trace 分组 |
| 云端实际 usage 已核验 | 原 runtime probe 57 + 新 owner probe 56 = 2 次/113 tokens，全部 settled，无 unknown/held/retry | 独立于旧 Memory 72 次/41852 tokens；旧 Memory/已运行 RSI 无历史追踪；totalCost=null 不编造费用 |

应用 metadata-only 不上传 prompt、response、history、gold 或凭据值，因此不声称云端内容回放或内容评测完整。
