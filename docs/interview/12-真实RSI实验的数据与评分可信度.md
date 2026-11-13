# 真实RSI实验：数据、评分与失败归因

2026-11-14。阶段为官方HumanEval短代码真实模型校准，不是长程能力证明。用户确定方向，主代理设计/集成/验收，两位Sol执行与交叉审查；个人叙述限于能复现的部分。数字对应具体版本，不把不同批次测试相加。

## P52：不同题都拿满分，为什么会被误判数据泄漏？

1. **背景**：旧loader用评分树语义身份防train/holdout重复。官方HumanEval/7与/32的输入不同，构造同样全1曲线后仍碰撞。仅改task_id本就应被拒，但不同题恰好同分也被拒。
2. **取舍**：保留旧数据的保守规则，为有公开输入内容证据的新数据增加另一种身份。没有通过删题、改分或加噪让数据通过。
3. **动作**：host提供公开输入/可见规格原文及可重算SHA；runtime在开始时冻结，核单文件baseline与字节内容，trace/report/export绑定同一digest；loader分别校输入身份和曲线身份。
4. **状态**：不同已验输入可共享曲线；同输入跨split改ID、路径、visible spec、评分仍拒；legacy/mixed缺证据时保留曲线拒绝。主代理相关组合94项通过，包含旧规则与真实SDK离线契约。
5. **边界**：只支持单个UTF8公开输入文件，精确字节身份不是语义等价判定；loader不能证明来源官方或checker真正执行。官方出处由预检adapter负责，host仍是信任边界。
6. **证据**：两个官方public prompts的反例分数明确为synthetic；后续真实API trace另存，不把协议测试当实绩。私有canonical/final内容不进入proof或优化器。
7. **个人贡献**：任务协议、运行绑定与防误去重；没有发明Dream策略或训练模型权重。
8. **待补**：多文件任务需要明确内容契约，不能塞一个自填hash冒充已核输入；跨仓库泛化另需独立任务集合。
9. **追问**：为何不能直接信task_id？改名即可绕过。为何有hash还要内容？只有自报hash无法重算一致性。为何同输入改分仍拒？评分波动不能把重复任务变成新样本。为何保留mixed保守规则？缺来源的一侧仍无法证实独立。

可练表达：把任务输入身份和观察到的结果曲线分开；去重应防止重复输入，而不是禁止不同题产生相同结果。

## P53：测试文件放在host，为何候选代码仍能篡改评分？

1. **背景**：第一版host evaluator把private task读入`t`，再同进程执行candidate。实证stub可读`__main__.t['test']`，也可改`doctest.DocTestRunner`伪造visible通过；44次普通empty/gold预检没覆盖这种错误。
2. **取舍**：预期值和比较器留在host；candidate只做函数求值。选持久worker减少每个assert重新启动解释器的开销，不引入pickle。
3. **动作**：visible/final物理分JSON；host只执行固定官方doctest/check(proxy)，worker只加载solution.py。有界typed JSON仅传args/kwargs与primitive/list/tuple结果，host安全解码，expected/final源码不发worker；host random seed0。
4. **状态**：两个原漏洞先红后绿，发现者在v3独立复验：stub visible exit1、final对象不可达。11题重做44次checker+44worker，visible/final均empty0/gold1，任务专项22项通过。旧v2准备材料禁止付款使用并保留。
5. **边界**：进程分离限制Python对象/模块污染；同用户OS文件访问仍不隔离，不能称恶意代码安全沙箱或官方榜单复现。
6. **证据**：新v3报告绑定checker/worker/payload hash，异常只返回固定类型与布尔结果。实际模型任务的最终评分在selected副本后置，不进训练数据。
7. **个人贡献**：发现评分可信度缺口、建立数据协议和生命周期清理；官方题目/check逻辑不是自研成果。
8. **待补**：长程阶段需要容器/低权限环境接缝，并检查隐藏测试、镜像历史和网络旁路；当前结论限协议校准。
9. **追问**：为何普通empty/gold都过仍不可靠？它们没有尝试改比较器。为何分进程还不能称安全？OS权限未降低。为何不用pickle？候选返回对象可能在host反序列化执行代码。为何保留tuple类型？返回类型变化会把正确程序误判为错。

可练表达：评测器必须持有可信预期和比较逻辑；程序执行结果是一份不可信数据，不应变成评测器进程里的可执行对象。

## P54：有journal和source hash，为什么仍可能多付一次？

1. **背景**：observer会吞异常，不能充当付款边界；初版只在trial入口verify任务，每请求只核实现。另有已failed后换ID还能start的实证反例，环境模型凭据也可能被shell继承。
2. **取舍**：保持真实LLM类型以保留observed来源，用ParentBudget公共reserve/settle接缝做持久化；不套旧GuardedLLM，也不复用旧prompt+cache统计。
3. **动作**：付款前fsync reservation，hash-chain JSONL+FileLock；每请求和评分前核代码/任务/checker/输入。低层统一block与预注册slot元组，已开始槽不重试；CLI内存取key后清环境，库入口拒脏环境。
4. **状态**：unknown保留reservation并停整个新campaign，旧533请求账本不改。主代理独立v2 runner45项通过；低层换ID、漂移、写盘失败provider0与恢复均有回归。
5. **边界**：单host串行协议，不保证外部副作用exactly-once，不抵御恶意host重写所有hash；来源observed只识别实际客户端类型，不是服务器签名。
6. **证据**：连通1调用55tokens；v1首train4调用8496tokens、无pending/unknown/held，start/end与usage结算可关联。输入按total-completion，cache缺失不补0，币价未核currency=null。
7. **个人贡献**：把调用前约束、存储与恢复状态接起来，避免观测记录被误当执行控制。
8. **待补**：并发campaign需lease/归属设计，当前不能让第二进程接管正在运行的槽。真实账单、缓存完整覆盖和服务商余额尚未核验。
9. **追问**：为什么写盘失败要在API前停？否则产生无法关联的消费。为什么不能先把APIkey放env后只给评分器清？Agent shell也会继承。为什么是total减completion？prompt字段可能排除单列cache。为什么unknown不补零？请求可能已经计费。

## P55：测试已过，为什么首个真实任务还算未完成？

1. **背景**：v1每attempt只有4轮。模型实际ls/find、read、edit、bash测试，可见测试1，但4轮结束时尚无endturn；runtime无selected/final/trace。
2. **取舍**：保留四轮负结果，区分代码可见状态、Agent交付和host评测执行。不把visible1改成整个任务成功，也不通过同campaign另ID补跑。
3. **动作**：按先跑首slot再校准的计划，在任何test数据出现前统一改v2：12轮、父64请求/1Mtokens/1800秒/6评估；全部四臂同上限，dataset/模型/工具/完成规则不变。
4. **状态**：v1只跑1个train，52槽未开始；4请求8496tokens的成本与原源码快照保留。v2首train有4个有效endturn尝试、独立final通过、16请求39208tokens，已导出真实评分树。
5. **边界**：这是训练阶段额度校准，不是对test看分后加预算；HumanEval短题不能证明长程能力。v2没有把quota终态重新定义为成功。
6. **证据**：五轮实际工具/模型契约在12轮成功，4轮control仍budget_exhausted；正常score0或已知干净quota保留失败分母，未知/漂移/cleanup则停止。
7. **个人贡献**：实验先导、失败分类与协议修订；不是模型能力突然提高，不能写“0→100%提升”。
8. **待补**：后续四臂成功率与总token需完整统计，包含校准/训练/验证开销。当前首train通过不是比较结论。
9. **追问**：为何不直接丢掉v1？会隐藏选择执行额度的成本和依据。为何不只给失败臂加轮数？会破坏机会匹配。为什么测试没跑也记非成功？按时完成交付是协议目标，但最终测试分数仍保留unavailable而非伪造0。

可练表达：先用训练侧先导检验预算是否允许一次完整工具工作流，再冻结全部对照额度；把修改前后的协议与成本都保留下来，让数字能解释。

固定协议、开始快照与后续完整结果见[真实校准目录](../experiments/results/rsi-live-20261114/)和[执行交接](../handoffs/2026-11-14-live-rsi-evaluation.md)。未完成的比较保持未完成，不提前写质量或节费收益。
