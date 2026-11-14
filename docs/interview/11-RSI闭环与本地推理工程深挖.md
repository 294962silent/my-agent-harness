# RSI闭环与本地推理：可追问的工程证据

2026-11-13。当前完成代码与功能闭环；scripted模型演练不是模型质量成绩。用户确定目标，主代理设计/集成/验收，SOL-1/SOL-2实现并交叉审查。个人表述限于自己能复现、解释的内容。

## P45：怎样把“离线回放”变成真正影响Agent的闭环？

1. **背景**：原active仅被TraceEnv消费，参数搜索通过也不会改变CodingAgent下一步。
2. **决策**：保留现有Agent loop，在外层增加串行portfolio；只学习width/refine，不让候选生成评分器或任意Python。
3. **动作**：LocalBranchFactory创建独立workspace/Session/ArtifactStore；实际工具修改文件，可见命令评分并记录parent/branch/attempt；合法trace导出train/holdout。FrozenPolicy在下一episode读取registry或复制已验参数，控制真实分支宽深。
4. **交付**：生产API与CLI合成演练完成，最终RSI专项44项通过，主代理独立复跑SDK dry-run链一致。TUI流式暂不支持，不默默绕过portfolio。
5. **范围**：每次显式启动一个episode，策略运行中不热更新；没有后台持续自改，也没有模型权重训练。
6. **证据**：四臂相同12请求/300000tokens/90秒/6评估上限，C消费6次脚本请求，其余12次。质量未测，不能把脚本安排的分支结果称为学习泛化。
7. **责任**：项目实现执行/数据/策略接缝；Dream SDK策略类、模型和测试任务并非自研算法。
8. **缺口**：真实模型train/holdout/test、预算匹配固定/random控制、独立未见任务分数，见[实验协议](../experiments/RSI_CLOSED_LOOP_EVALUATION.md)。
9. **追问**：为何会话树不等于搜索树？消息父子关系没有可信状态、动作成本和评分。如何防reward hacking？host固定可见评分，final后置且反馈不入训练；仍需真正隔离隐藏测试与运行权限。为何不用test选参？那会把泛化评价变成优化目标。

可练表达：先找出“参数有没有被真实执行器消费”的缺口，再用真实文件工具和评分命令接通两端，最后把机制证据与未见任务收益分开验证。

## P46：为什么有了每分支预算，整体仍可能失控？

1. **背景**：N个branch各领完整额度，摘要、评分或失败重试可在主统计之外继续消耗。
2. **决策**：ParentBudget共享请求/token/deadline/评估池，SharedLLM覆盖子Agent与摘要；禁止同步provider裸入口和通用属性穿透。
3. **动作**：派发前保守预留输入bytes与输出额度，已知usage结算；取消/缺usage保留reservation并阻止继续调用。输入优先total-completion，兼容缓存另列。
4. **交付**：覆盖辅助调用、stream facade、取消/abort、清理失败和unknown边界。stream facade当前缓冲批响应，不冒充增量流。
5. **范围**：保守预留是协议假设，非精确tokenizer保证；同步copy/snapshot可阻塞eventloop，下一边界拒派发不等于抢占所有磁盘IO。
6. **证据**：962+195与另列缓存256的usage反例按总量算输入1218、总1413；四臂记录实际支出及相同ceilings，不用dummy请求凑齐消费。
7. **责任**：控制可见调用与资源生命周期，不保证供应商计费或任意工具外部支出。
8. **缺口**：真实provider成本核账、工具级费用、强进程隔离以及长运行磁盘操作取消需另做。
9. **追问**：为什么unknown不补0？请求可能已经执行，0会制造额外额度。为何相同预算不要求消费相同？预算是机会约束，效率就是是否更早成功；但训练费用也必须另计。为何final也入池？否则学习策略通过额外评分获得免费机会。

## P47：Windows分支隔离与补丁为什么需要原生反例？

1. **背景**：is_symlink不可靠识别junction，可能把目录外文件复制进分支；无尾换行diff曾拼成`-0+42`；cleanup失败曾仍可导trace。
2. **决策**：resolve前检查lexical ancestors的lstat/reparse属性，校验resolved containment；明确拒绝drive/UNC/traversal。补丁按Git格式保持换行语义。
3. **动作**：真实Windows junction反例、普通copy正例；无尾换行/LF/CRLF的五组patch由实际git apply并逐bytes核；cleanup_failed禁止trace/优化数据导出，取消清理后继续传播。
4. **交付**：两个助手交叉复现并复核，最终专项和整库覆盖相关回归；没有抹去先前失败结果。
5. **范围**：目录隔离不是OS sandbox；工具保留host权限；检查不能消除所有TOCTOU，apply也非原子事务。
6. **证据**：源answer.txt保持0，选定tree/patch只供显式review/apply；合法路径可用、越界反例拒绝。
7. **责任**：负责输入路径与产物应用约束，不声称恶意代码全面隔离。
8. **缺口**：容器/低权限账号、文件句柄级操作、原子提交和大文件IO需独立设计。
9. **追问**：为何resolve后才检查不够？链接入口被折叠，失去lexical路径证据。为何选了候选还不能称成功？清理未确认或final未执行都有不同终态。为何不自动apply？实验应保留源快照和可审阅交付，避免污染后续臂。

## P48：装上embedding模型后，SDK为何仍访问网络？

1. **背景**：本机sm_120 GPU需相容Torch；原默认venv无Torch。真实Mem0路径另会懒下载BM25或spaCy，单验模型local_files_only不够。
2. **决策**：固定官方revision及10文件内容身份，独立cu128运行环境；Qwen-owned派生SDK对象禁BM25/NLP，不改SDK模块全局。
3. **动作**：校验Mem0版本/commit及10个原方法LF规范源码hash，再隔离函数globals和dense-only建库。worker离线、token=False、禁remote code并剥离凭据/代理。
4. **交付**：RTX5060真实生产factory/MemoryStore中英文检索通过；主应用无Torch，1024维新库与BGE512维旧库分开。
5. **范围**：Python socket guard不是OS防火墙；禁BM25/NLP改变SDK语义，不能宣称完整原版Mem0。默认最大1024tokens，过长报错，不静默截断。
6. **证据**：两事实smoke峰值allocator约1.208GB、冷构造16.45秒；词法回退替换为抛错仍成功。不外推至吞吐、最大上下文或召回率。
7. **责任**：负责可复现装配、隔离和失败边界，Qwen模型与Mem0算法不是自主训练。
8. **缺口**：旧BGE臂BM25是否实际触发未记录；未来单因素对照要统一dense/hybrid、chunk/rerank与索引预算。旧负结果保持不变。
9. **追问**：为何只pin包版本不够？同版本源码仍可能改变，且lazy路径要实际执行才暴露。为何选独立进程？隔离Torch依赖、可回收GPU、避免默认应用膨胀；代价是冷启动、IPC和额外协议。

## P49：为什么设了response timeout，调用仍可能永远卡住？

1. **背景**：worker ready后停止读stdin，write/flush可先阻塞，代码根本到不了等待response的timeout。Windows独立Python还曾按GBK解中文stdio。
2. **决策**：发送/flush/接收共用一个deadline，用有界JSON帧和唯一request ID；显式`-I -X utf8 -u`。
3. **动作**：sender线程、1MiB frame cap、超时熔断不重试；终止owned进程树并确认sender/reader退出。记录venv launcher PID与真实CUDA worker，native handle确认实际退出。
4. **交付**：CPU“ready但不读”反例先失败后修复，独立send_deadline回归1项通过；GPU路径也验证close_complete。
5. **范围**：请求deadline之后仍有独立有界OS清理窗口，不保证瞬时退出；不是任意子进程安全沙箱。
6. **证据**：主代理两次GPU装配的launcher/worker均回收；owned termination returncode1如实保留，不包装为正常0退出。
7. **责任**：管理本适配器创建的进程与线程，不关闭其他会话服务。
8. **缺口**：协议并发、worker池、拥塞及生产进程监控尚未评测，当前串行单worker。
9. **追问**：为何只cancel asyncio任务不够？同步pipe写可能在另一个线程继续阻塞。杀launcher为何不够？Windows venv会启动不同PID的真实worker。如何避免幽灵重试？首次协议失败熔断，并保留资源关闭状态。

## P50：压缩失败为何不能直接清空所有缓存？

1. **背景**：新摘要不fit时，曾删除当前分支仍合法旧摘要；失败路径损失恢复状态。
2. **决策**：摘要更新按事务快照处理；只恢复对当前canonical/分支仍适用的旧缓存，失配分支绝不复用。
3. **动作**：auto/manual两个反例先红后绿，失败恢复旧cache、pending=None，并拒绝超限请求；canonical不改，cut保留原坐标。
4. **交付**：layered先外置/micro/显式前缀fold，最后有界结构化摘要；保留首user契约、当前turn、host保护文本与错误/未闭合tool组。
5. **范围**：有损摘要不能保证全部语义；不支持LLM任意中段精裁，不发送provider cache_edits。before_compact记忆钩子可能另调LLM，demo禁memory不代表普通配置零辅助调用。
6. **证据**：29984B夹具中baseline假摘要1次后4108B，layered0次后12044B，6份产物回读成功。baseline结果反而更小；只证明微压缩避免一次合成摘要。
7. **责任**：借鉴Claude分层处理顺序与现有Harness产物/分支摘要机制，不把截图当官方源码，不宣称复刻全部机制。
8. **缺口**：真实任务的信息保留、回读成本及KV缓存字段完整对照未测。工具结果改写可能使服务端对应前缀失效。
9. **追问**：为何小视图不必更好？可能丢证据并增加回读。为何不用micro结果算摘要身份？动态投影会随时间变化，身份应来自canonical历史。为何失败仍拒绝请求？恢复旧cache不代表预算足够，继续派发会掩盖安全边界。

当前可写的简历句：为Coding Agent实现受控探索、评分树导出和冻结策略执行闭环，统一父预算与取消/资源回收，建立A/B/C及随机策略对照；完成跨平台回归与可复查实验档案。真实成功率和成本收益留待独立实验，不以测试数量代替模型能力。

## P51：为什么Windows all-extras全绿，CI仍抓到实际缺陷？

1. **背景**：24aeca1的Windows全量1424项通过，但四矩阵默认依赖阶段失败。三项SDK测试无条件import可选Mem0；Linux另有BrokenPipe覆盖timeout，all-extras环境不能代表默认安装或Linux的管道行为。
2. **决策**：分别复现依赖维度和OS维度；不强制默认安装全部SDK、不跳过整份协议测试，也不放宽“应为TimeoutError”的断言。
3. **动作**：主代理建立独立干净默认依赖Py3.13环境，旧版3失败/6通过；SOL-2在本机WSL复现超时后stdin.close二次flush抛BrokenPipe，而worker及sender/reader均已退出。
4. **交付**：三项真实SDK用例局部缺依赖跳过；生产清理只处理owned实际worker确认退出后的stdin BrokenPipe，其他流/异常和未确认退出仍报错。首失败[安全摘要](../experiments/results/context-rsi-20261113/ci-repair/failure-summary.json)保留，修后验证另存。
5. **范围**：修复异常因果与默认安装契约，不能证明所有OS管道/终止行为。请求deadline和清理等待仍是两个窗口。
6. **证据**：原严格deadline反例保留，新增已退出stdin BrokenPipe回归；其他close异常传播由窄范围源码审查确认，没有独立负例成绩。真实GPU与默认/all-extras环境分别复验，不把Windows绿色外推为Linux绿色。
7. **责任**：CI作为独立验证暴露了本机覆盖缺口，不能说首次就全平台通过；修复、复核和原失败的时间顺序公开。
8. **缺口**：更多OS/解释器/管道容量及退出race的组合仍需按风险扩展，不为漂亮数字重复同一确定性fixture。
9. **追问**：为何close也会写？TextIO会flush缓冲，读端关闭就可能EPIPE。为何不统一吞OSError？会掩盖真实关闭失败。为何default依赖测试仍有价值？可选集成应保持基础安装可用，但all-extras CI必须实际执行这些SDK用例，避免永远skip。
