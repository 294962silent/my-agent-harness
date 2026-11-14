# 2026-11-14：真实模型RSI校准与后续长程评测

用户已明确要求继续测试，声明约20亿DeepSeek V4.1 Flash tokens可用；这是新一批执行授权，不再沿用上一轮“0新增API”的范围。余额为用户声明，未查询服务商。继续本机，复用SOL-1/SOL-2（GPT-6.1 Sol/medium），不启动DS/DSH、不迁L20。

当前执行协议为 `rsi-humaneval-calibration-v2-turn12`。下方16请求/4轮是首slot校准v1的原方案，历史保留；v2统一64请求、100万reservation tokens、1800秒、6评估，每attempt12轮，输出4096/请求180秒/零重试。原因和v1原始负结果见末节；正式test题在此修订前均未执行。

## 本批执行顺序

先把已完成的脚本模型闭环替换为真实LLM，验证真实请求/工具/评分/trace/选参/冻结策略的完整因果链。现有SWE/Terminal/EVO依赖容器环境，不能把任务目录直接传给host LocalBranchFactory，因此先采用官方HumanEval短代码校准。它不构成长程任务成绩，不替代后续容器接缝和长程实验。

1. 固定官方源码/数据commit、blob及内容hash，准备11个实例；付费前各题empty/gold与可见/最终评分门控。
2. 冻结真实模型配置、执行源码hash、任务与53个episode槽位；先运行首个训练episode，检查完整用量/真实来源/工具调用/终态后自动续跑，不重复已开始槽位。
3. train3、holdout2的真实分支树导出，实际Dream SDK dry-run选参；冻结train唯一候选并记录holdout验收。
4. test6题×2独立调度seed×A/B/C/random，共48个测试episode。执行顺序在看答案前固定，按题交错四臂，不能看test改策略。
5. 汇总全部失败/不可用/费用缺失、请求/token/时间、评分与策略状态，保留原始私有产物，提交安全报告和面试问题记录。

## 固定任务与评分

train：HumanEval/7、11、27；holdout：32、45；test：1、6、9、20、33、40。调度seed23、47用于重复与random调度，不声称服务端支持确定性采样。全部同一公开benchmark，只有ID与内容隔离，不宣称仓库外泛化。

source只有公开prompt的solution.py；goal可包含相同公开prompt。可见例精确取entry_point的官方docstring，不能误取第一个helper函数。gold与最终test只保留host，不能复制进branch。host固定评分器/原始例，不运行branch可篡改的测试文件。最终评分在选定产物副本上后置，反馈不进选择或训练。

当前目录隔离非OS沙箱，恶意shell仍可能读host；这是协议隔离的校准，不能称强对抗隐藏测试或官方榜单成绩。预检失败必须在付款前阻断，保留失败；不在观察模型结果后替题。

## 四臂与策略发布边界

A：基础上下文/无产物投影，固定width2/refine1。B：layered+产物投影，策略同A。C：B+train选出的冻结研究策略。random：B+固定合法边界的seeded调度。模型、工具schema/权限、采样配置和父额度相同；A→B是工程增强组合。

预注册研究规则：即使候选未达到holdout生产晋升门槛，也可在不发布条件下评估唯一train候选C，以检验其泛化失败；报告必须标rejected_for_publication，不能称已验证更优策略。不得再看holdout/test选择其他候选，不写global active，不改变已开始episode。

## 真实模型、记账与恢复

冻结逻辑模型ID deepseek-v4.1-flash、openai兼容provider及既有OpenCode Go端点。host只读现有可信凭据或明确LIVE_BENCH_API_KEY，APIkey仅留内存，不写任务/日志、不放进子进程环境。服务商权重revision不可得时如实记录，不把逻辑ID称为不可变模型权重版本。

同一episode父上限16请求、300000 reservation tokens、900秒、6评估；每attempt最多4轮，输出4096、请求timeout180秒、max_retries=0。额度用于机会匹配和运行可靠性，不以原$30限制阻止用户新授权。实际消费可不同，不用dummy调用凑齐。

新campaign使用独立持久journal，原累计账本只读：533请求、已知$1.218608、未知预留$0.045131；原文件SHA256 `495d9e656deccd64ff67ca5c4db9d4c391520700bd0bb672e8fad72a45018e3d`。旧unknown不补零、不重试旧trial；新授权与历史指针同时记录。新campaign出现pending/unknown时保留预留并停止派发，不能靠新ID重跑。

付款前持久reservation，settle保留provider规范字段、authoritative total-completion输入口径与依据。不得直接复用旧prompt+cache求和逻辑，也不包裹成破坏真实LLM来源识别的假客户端。价格/供应商账单未核时currency保持null；KV字段缺失保留unavailable。

## 分工与验收

SOL-1拥有experiments/context_rsi/live_runner.py及其回归：journal、真实LLM、父预算、恢复、53槽调度、SDK优化与冻结。SOL-2拥有live_tasks.py及其回归：固定公开数据、准备器、不可篡改host评分、逐题门控与来源。两者按PreparedTask/load_tasks/verify接口协调，不覆盖他人改动。主代理负责方案、集成、真实启动、结果重算、统一日志和GitHub交付。

先分别验证无SDK默认环境/真实SDK离线契约与有针对性回归，再启动实际API。原脚本闭环、GPU及失败档案保持。若短代码没有触发压缩/产物回读，只能报告未发生，不能据此推断上下文增强收益。

## 付款前修复与校准修订

- 两个不同真实题可能有相同分数曲线。新增可重算公开输入/可见规格证据；同输入改ID/分数仍拒，不同已验输入可共享曲线，legacy/mixed仍保守。来源由trusted host绑定，不证明恶意host或远端执行。
- 原checker与candidate同进程，实证可读final对象或替换doctest伪造通过。v3改host checker+独立candidate worker的typed JSON RPC，visible/final物理分开，host seed0，11题重做44次checker+44worker预检；v2准备器报告保留且禁止付费用。最终任务manifest SHA为`cc28f20d6303100f4fe8d5b550937643fdb9e88239b0601c1f144f9356763339`。
- 每次reserve与评分前复核代码/任务/evaluator/输入；固定slot与fatal状态在低层统一阻断；CLI内存取key后清理模型凭据环境，库调用拒脏环境。正常score0/可信干净额度耗尽保留失败分母，unknown/漂移/清理异常仍阻断。
- 单独API连通55tokens结算；首v1训练4请求8496tokens已真实ls/find/read/edit/bash、visible1，但4轮耗尽未endturn，无selected/final/trace，作为额度校准负结果。53槽只跑首train，52未开始、0test；保留原journal/源码快照，未重置unknown（确实为0）。
- 正式v2重新冻结全部53槽，同11题、同模型/工具/评分规则，所有臂统一12轮/64请求/1M/1800秒/6评估。训练7此前校准暴露单列，成本计入准备阶段；不将旧试跑混入正式对照、不重复旧campaign槽、不根据test结果调预算。源码/任务快照与start-v2.json在付款前保存。

## 观察collection失败后、首test前的探索性恢复r1

v2五个collection共84请求/237239tokens：train3/3有完整评分树，holdout仅1/2；/32在12轮额度耗尽，无可导出trace或独立final，其负结果与58011tokens保留。完整验证不可用，旧export在SDK运行前拒绝；原目录/journal/error不覆盖，0test数据已出现。

这是明确的test-blind探索性修订，不声称原完整holdout协议通过。另存五题full coverage和全成本，再为实际SDK构造独立available-tree投影（全部3train+可用val45）；选择/平局只由train决定，partial holdout仅raw诊断。整体gain均null、强制拒生产晋升。唯一train候选可冻结作研究测试；若SDK选择baseline/null，保留原身份，在线C用预先规定的2/1 reference fallback，不能称新学习干预或精确SDK默认映射。

新恢复脚本不改冻结117执行源、不做模型调用、不重采collection，执行前核0test/0unknown/所有封印；以独占输出与增补operator事件记录，封印后只允许完成同一提交，不能重选或覆盖。提交后的原runner只继续48个未开始test槽。细节、限制和命令见[恢复协议](../rsi-live-recovery.md)。

## 实际终态：测试与运维续跑均已结束

48个test全部终态：47正常交付且独立final通过，1权限失败未执行final；5个collection保留val32无合法trace，完整交付4/5。SDK选择baseline，C使用预登记reference2/1，学习贡献不可识别、未晋升。

原runner在6-seed23-C权限异常后停止，实际298请求全结算而非unknown。按[独立运维例外](../rsi-live-continuation.md)续原37未started槽，先1后36，全部完成且原失败不变；原634行prefix与117源保留，controller新增封印。此次操作不是权限根因已修复或原协议无修订完成。

v2为969请求2866472tokens，计入v1四请求8496tokens和连通55tokens共974请求2875023tokens，0pending/unknown/held，金额null。原失败的第三attempt尚未请求模型，schema指纹为null；最终analyzer明确allow-incomplete发布完整coverage，comparisons.status=schema_mismatch，只含该证据缺失组，不生成排名。最终[报告与审计](../experiments/results/rsi-live-20261114/REPORT.md)记录全部原分母、成本和限制。相关运行进程已退出；不可再启动本campaign补采或重试。
