# 空补丁与回读：诊断后实施计划

日期：2026-11-12。用户要求继续查明空补丁与未回读原因，并顺便核查CI邮件。基线为干净 `d437650`，仅两名GPT-6.1 Sol/medium助手。原765生成的v2档案及manifest不覆盖。

## 已有事实与取舍

- CI邮件针对旧f0d12c1；子代理已核当前d437650/run43231106780四组成功，测试阶段同步修复已覆盖旧失败，不为旧通知回退或重跑，也不更改通知设置。
- 7个empty轨迹最支持未进入源码编辑，未见回滚/提取丢失正证据；缺少逐步diff快照，不能绝对排除隐式修改。3个resolved首次写入在10/13/14次请求，是隐藏预算仍可成功的反例。当前system要求实际编辑，但未告知16请求上限。
- 11个artifact与原工具结果一致，生产回读均可匹配。重建预览有handle与正确调用方式，未见schema错配；原Session为canonical，没有原始Provider请求抓包，不能把重建当作实际传输证明。0回读不自动等于缺陷，原任务未标注隐藏中段必须读取。
- 动态剩余预算需要安全边界追加并保留旧快照，否则尾部临时替换会破坏前缀。首实验选更小的静态可见性：初始system只增加真实API请求上限信息，不加“更早编辑”或强制回读指令，不改core。

## 总预算与真实实验

同一全局30美元账本，当前已记$0.572059，不重置。新增客户端准入总额至多$2，整体绝对上界$2.572059；先probe阶段新增至多$0.25，再SWE阶段新增至多$1.75。两个阶段串行、逐请求同锁预留，未知用量/超计量停止全批，不自动重试；保守计价不等于服务端账单。

1. **隔离回读能力探针**：2份冻结约14KB日志×projection on/off，共4trial；每trial最多4主轮/4请求、1024输出、60秒、memory关闭、无摘要。日志目标nonce只在隐藏中段；fetch_log和生产artifact_read/search是全部工具，无任意shell取巧。on必须fetch后目标不可见/handle可见、实际read/search后目标可见、最终答对；off从全文答对。自然模型决策不硬编码，失败也保留。这不是SWE质量成绩。
2. **预算静态可见性配对**：3个预先固定任务，每题提示开/关各一次，共6trial。artifact_projection固定on；请求模型/温度0.7/16请求/4096输出/600秒及工具不变。只在开启组初始system增加“本run最多16次API请求（包含辅助）”；不增加步数或其它策略指令。种子只安排先后，不控制服务端采样。预选面板见独立SELECTION。

## SOL-1：预算可见性及观察

仅拥有 `experiments/benchmark_live/swe_agent.py`、`swe_campaign.py`、必要的 `swe_results.py`元数据透传及其对应测试；不改core、probe或共享日志。

- [ ] 新增 `budget_guidance=none|static`，默认none兼容；静态文案取实际请求cap，整个run不改写system。run.json记录明确模式与artifact_projection。
- [ ] campaign新增明确实验factor（默认artifact_projection，新增budget_visibility）；后者trial.arm为可见性on/off，solver的projection恒on。plan/trial/output区分条件与投影，不能把对照组错关产物或混用模型名标签。旧计划/归档保持解释兼容。
- [ ] 在每次shell返回后做只读观察：base diff numstat与untracked计数，记录到宿主sidecar，不返回模型。不得调用git add-N改变index；失败记录unavailable，不掩盖工具原结果。指标叫“响应后可见工作树差异”，不把任意新文件自动称源码编辑。
- [ ] 定点回归验证提示单一差异/同run稳定前缀、factor映射与旧接口、观察不修改Git/index、真实上限包含辅助请求；不把字符串存在测试当质量证明。
- [ ] 交给SOL-2只读复核，源码冻结后才允许真实调用，不提交Git。

## SOL-2：真实回读探针

仅拥有新 `experiments/benchmark_live/artifact_probe.py` 与独立测试；可在.git临时目录准备fixture/报告。其他生产文件只读。

- [ ] CLI使用fixture manifest+批准SHA、同一ledger/绝对ceiling、新输出目录。预先冻结两份不同nonce，目标不在用户prompt/工具schema/头尾预览。
- [ ] 装配生产Agent/Context/Artifact factory，on/off同schema，仅投影不同；每trial新Session/store，限制4次API且禁辅助调用，结束清理自己资源。
- [ ] 实际Provider方法入口只记录阶段、schema/hash、handle存在、目标可见布尔，不记录正文/auth。明确这是SDK序列化前边界，不冒充wire抓包。
- [ ] 根据实际回读响应与最终nonce判定on通过，提前泄漏或不回读猜中不能计通过；off的全文可见为预期。缺失用量保留unknown并停止。
- [ ] 离线覆盖真实投影隐藏/生产回读、泄漏拒绝、schema一致、4请求与费用边界、无正文审计；假Provider只证明机制，真实决策另记。
- [ ] 交给SOL-1独立复核，不提交Git、不自行发真实请求。

## 主代理：集成、实验与交付

- [ ] 记录诊断、设计、自变量与上限；源码/fixture/顺序在模型前冻结。实际代理核验优先于声明。
- [ ] 先离线集成和交叉review，再完成能力probe；协议缺陷先停收费修复，不为模型不成功追加重试。
- [ ] 继续冻结的三题可见性对照，官方评分独立、保持原gold成功绑定。记录空补丁、首次可见差异、实际完成原因、成本、目标任务级结果；不据三题泛化提升。
- [ ] 模型归档生成版本与分析版本分开；原v2原始档案不改，新增结果独立目录，公开材料不含原推理/认证/金标patch。
- [ ] 更新持续实验日志、面试主张与开发问题；明确路径提交自己的origin/PR2，检查最新head CI，不自动合并。
