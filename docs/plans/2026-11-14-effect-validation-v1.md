# Mem0 / RSI / Observation 效果验证 v1

用户在工程首版之后明确授权开始实验；日期2026-11-14。沿用本机、DeepSeek V4.1 Flash、已有API授权与两位Sol/medium助手，不迁L20、不恢复DS/DSH。起点0b63386，工作树干净；既有25请求108078tokens的red演示和所有旧负结果不可覆盖。

## 已批准的目标

1. 校准候选轮次预算，先解决此前6次候选中4次未正常结束且两轮无完整trace的问题。
2. Mem0：none / lexical / semantic同上下文预算，测证据召回、任务回答质量及真实成本；避免仅报告会话命中。
3. RSI：固定 / 随机 / 训练后冻结参数，同模型、工具、权限及预算，train选参、独立holdout决定发布，test不得重选。若baseline获选或未通过晋升，如实报告干预不可识别，不强制nonbaseline。
4. Langfuse：验证调用覆盖、Token对账、故障隔离；远端服务上传/回读是独立验收，没有凭据不得声称已通过。

## 第0阶段：调查与校准（优先执行）

Wine-red原final已见，只用于预算校准，不作新独立test。助手先从旧session工具名/拒绝次数/stop_reason/输出token与limit定位耗时原因，禁止输出提示、思考、答案正文或密钥。根据证据决定最小提示/入口修正，若无代码原因仅调整实验配置。

旧日志调查发现24调用中14次finish_reason=length，其中13次completion实打满4096；6个候选均未改train.py，只有ls/read/find，没有write/edit，2次路径拒绝不涉及/data。新首个付费前将配置定为每候选8迭代、16384输出token、64000上下文预算、provider timeout180秒、零重试，一轮最多2候选（width1/refine1）、父20请求/400000tokens/1200活跃秒/4dev评估，memory=none；仅dev，不读final，保持生产goal不改。若仍不能完成，保留全部失败和用量，先定位再另行登记提示/12迭代校准；未知usage/在途工作立刻停止，不隐式重试。正常end_turn、预测有效、确实修改代码、完整trace分别报告；结束文本不能代替数值有效性。此为训练侧预算校准，不用来声称相比旧版提高质量。

所有新产物用独立目录/新ID，不能reset或resume旧final-closed campaign。模型由主代理启动；助手在主代理明确交接前不运行真实模型或本地embedding。测试与本地模型串行，不再并行全库pytest造成资源峰值。

## 后续小样本协议

校准通过后，在首次正式任务之前另存冻结manifest：来源/许可/数据SHA、任务identity与train/holdout/test归属、arm/seed/预算、记忆快照、策略hash、评分器与模型配置。数据和准确样本数由第0阶段只读调查后补齐，再执行，不根据test表现修改。

RSI优先少量CPU公开回归任务，任务内部train/dev/final与跨任务策略划分分开；已看过的red final不能冒充新独立测试。所有测试条件共享合法动作、预算及baseline保留；记录真实消耗，不要求花光ceiling。可增加fixed-greedy小基线，防止把少搜索的收益全归RSI。报告训练/SDK搜索成本及回放评分调用代理与真实tokens的区别。

Mem0优先复用既有开源固定历史评测入口，选与旧30题不重叠的新小样本，按问题类型选题、先定ID与hash再运行；不根据召回结果选题。明确embedding backend、top-k/字符预算、回退策略，保留none/lexical对照。若新模型/数据改变，分别标记，不能把全部收益归Mem0算法。

## 分工 / 文件所有权

- SOL-1 (/root/sol_runtime_recovery)：第0阶段只读诊断；研究任务study/calibration runner与直接测试（experiments/research_validation/）；必要research_task/CLI提示修正需先报告证据再改；RSI数据集与策略对照。不要修改Mem0 runner或共享实验日志。
- SOL-2 (/root/sol_eval_recovery)：既有memory实验入口1–3文件调查、新样本选择及memory runner（experiments/memory_validation/）与直接测试；Langfuse配置presence/验证入口；不改SOL-1研究源。
- 主代理：实验设计与冻结、数据下载/许可、付费模型启动、集成验收、统一日志/简历/提交。两个助手共享目录，保留他人改动，不派生，不自行提交推送。后续交叉只读review与主代理验收。

## 持久进度

- [x] 用户批准路线与实验执行；起点干净/功能CI已通过。
- [ ] SOL-1旧候选失败原因与8轮校准入口核验。
- [ ] SOL-2新Memory小样本/成本/观测验证入口核验。
- [ ] 主代理新8轮真实校准，记录全部结果。
- [ ] 冻结正式Mem0/RSI小样本协议，执行并复核。
- [ ] 更新统一实验记录、真实结果及简历主张，精确暂存推origin，不自动合并。

## 2026-11-14 追加状态：执行者更换、Memory 完成与 RSI 独立验证

本文件前文SOL／禁止助手启动真实调用等为当时计划与授权边界，保留历史；用户后续明确停用旧SOL／GLM，由DS-1、DS-2以DeepSeek V4.1 Flash／原生max持续执行，已核运行时full-write。根负责设计、交接与验收；DS-1拥有RSI源码，DS-2拥有Memory runner／专项及本批共享文档追加，均不自行派生或提交。

- [x] Memory固定新12题（6类型各2、seed24、与旧30题无重合），none／lexical／mem0同800字符chunk／top8／6000字符；固定中文BGE，Mem0 infer=False。
- [x] 中断接管先核进程与零付费，复用5题原字节，仅恢复7题免费检索；36/36检索valid且close_complete。
- [x] 根批准v2→v3仅把ledger_path移出immutable plan，24个result/state原字节复制，无重建索引，旧失败保留。
- [x] 先三臂probe核settle，再完成36回答＋36盲judge；72settled、41852actual tokens，0held／unknown／retry，length／invalid均0。
- [x] 实际correct分别4/12、11/12、9/12；四题拒答均4/4，八题非拒答分别0/8、7/8、5/8；lexical对Mem0配对2胜0负10平，未证明Mem0胜过词法。
- [x] 根独立读回36行／12题、重算correct／tokens／answer SHA和公开文件hash；属于读回重算，不是人工重判。
- [x] 研究后prepare布局守卫：初次22项后补真实.tokens.lock两例，最终24专项和Ruff作者／根均通过；旧study源码与结果不改。
- [x] 三例匿名诊断、报告／简历事实边界及最小公开源码快照完成：13pins中8项Git可得，5项精确快照。
- [ ] Langfuse远端上传与读回仍pending；本地21项SDK验证不替代云验收。
- [ ] RSI v2 STOP保留；v3 artifact_budget_v1增量独立review通过，实际运行与晋升结果由DS-1及根另记，当前不宣称学习提升。
- [ ] 根核准精确allowlist后才暂存／提交；本批尚未stage、commit或push。

证据入口：[Memory完整报告](../experiments/reports/effect-v1-sol2.md)、[效果清单](../experiments/results/memory-effect-20261114/effect-metadata-manifest.json)、[诊断清单](../experiments/results/memory-effect-20261114/diagnostics-manifest.json)、[源码快照清单](../experiments/results/memory-effect-20261114/source-snapshot/manifest.json)。新长程／多轮Memory lane只做无API、无embedding加载的可行性调查，待根选择并冻结新协议后执行；旧十二题结果保持。

### 2026-11-14 追加状态：Langfuse 实际云验证完成

- [x] 既有 runtime 真实 57-token 请求 → flush/shutdown → 官方 CLI HTTP 200 读回，root 独立复核。
- [x] 新独立实验 owner/显式单次付费 CLI，工作线程稳定 session_id + 安全 campaign/arm/stage/task_id；新版本再以获准唯一追加请求 56 tokens 验收并远端读回。
- [x] 公开 verification.json、17 项离线回归、Ruff；两次合计 2 请求/113 tokens 独立记账，旧 Memory 72/41852 保持不变。
- [ ] 旧 Memory/已运行 RSI 请求未追溯为 live；将来 Memory 必须冻结新 source pins 后采用 owner 接线。完整多轮/跨 trace 分组与内容回放未验证；metadata-only 明确不上传 gold/history/prompt/response。

本追加状态更正此前的云端 pending，不覆盖历史 SDK-only 结论与旧研究记录。公开证据：docs/experiments/results/langfuse-live-20261114/verification.json；复用说明：experiments/benchmark_live/OBSERVATION.md。
