# Harness 改进实验记录

> 本文件记录实际改动、与旧版本的差异、设计收益、参考来源和验证结果。最新状态可以更新，历史实验只追加或标注更正，不抹掉失败记录。

## 1. 实验范围与当前状态

- 实验开始：2026-11-11。
- 最近复核：2026-11-12（北京时间；框架重设计见 E-REDESIGN，真实小样本见 E-REDESIGN-LIVE；旧DSH/缓存对照保持取消）。
- 项目基线：`a04e940`。
- 当前重设计分支：`codex/interview-harness-redesign`（独立 worktree，基于已交付 `1f150e1`）；原 `codex/scheduler-reliability` 脏工作区保留，前一交付分支为 `codex/context-memory-reliability`。
- 最初目标：补强事件 / Hook / ReAct–Steering–Follow-up 双层循环的调度可靠性，再增加投递追踪与执行上限。
- 当前目标：面向 Agent / 大模型应用实习，形成清楚的模块职责、可复算消融、真实任务链路与开发问题证据。
- 事件、Hook、Steering / Follow-up 和双层 Loop 在基线已经存在。本批补强它们的运行边界，不把已有能力重新包装成新增功能。
- 当前交付状态：**Runtime / 观测 `9509262` 已推送；八模块离线证据与一题真实SWE配对已完成，本批后续提交 / CI 结果按尾部实际记录更新**。前一PR #1尚未合并，原调度账本 / RPC未完成项仍保留，不因本批完成而自动交付。
- L20 工作区副本：已将 E10 时的工作区快照传至 `/data/llma/my-agent` 并完成逐文件校验；后续本地变更不自动同步。未安装服务器依赖，未据此宣称服务已启动或服务器测试通过。
- 本批范围：进程内单执行器、唤醒与归属、运行期输入身份和撤回、轮次 / 时长预算。Redis、多进程恢复、模型请求 redirect、任意同步代码强制抢占均不在本批。

### 当前进度

| 部分 | 当前实际状态 | 尚未完成 |
| --- | --- | --- |
| 单执行器、合并唤醒、停止交接 | 协调器和 RPC 已接线；重复取消、清理等待、唤醒运行态等回归已通过 | 并发会话切换及失败交错、最终集成演示 |
| 会话维护与失败释放 | 真实 RPC compact 已接入同意图维护；指定 maintenance / fence 回归通过 | 不据此宣称多个会话切换请求之间具有原子性 |
| 后台结果归属 | 启动时冻结会话 / generation 和完成回调；旧结果可被拒绝注入 | 随最终集成演示复核 |
| 输入投递账本 | 独立账本完成，11 项测试通过 | 尚未接入真实 Queue / Loop / RPC 的完整投递链路 |
| `/queue` 与终端展示 | 客户端、通知、状态存储、命令与界面已用假内核测试 | 真实 Python `queue_list` / `queue_cancel` 尚未接通 |
| 轮次 / 时长预算 | `RunLimits`、共享 `RunBudget`、执行级 deadline guard 共 40 项测试通过 | 尚未接入实际 Loop、产品默认值和预算终态展示 |
| 上下文增强：动态快照与请求诊断 | 已接入每次模型请求；仅保留指纹与有界变化记录 | 本轮未评估真实 API 缓存收益 |
| 上下文增强：分支摘要来源 | 已校验 canonical 前缀、消息身份与指纹，接通切分支 / resume / Session 替换 | 不据此放宽有效 Floor 或声称支持未实现的原生摘要载荷 |
| 上下文增强：稳定产物与回读 | CodingAgent 已装配稳定引用、头尾预览、受限读取 / 搜索，真实假模型闭环通过 | 受大小 / 搜索扫描上限约束；超限或失败明确报告 |
| 长期记忆：脱敏、全文、删除、检索门控 | 本地存储与真实 Agent 装配经假 LLM 回归；跨平台 CI 通过 | 正则漏报 / 误报、旧备份未清洗、无跨进程锁 |
| 长期记忆：空闲与压缩前抽取 | 默认关闭；启用后有取消、超时、Session/分支身份与 evidence 校验 | 当前进程任务、有界采样、旧未标注历史不自动抽取 |
| 四个调度演示、GitHub PR | 上下文 / 记忆 PR #1 已创建并通过四组 CI；调度演示未完成 | 调度最终集成、演示及其独立交付 |

## 2. 为什么改：旧版本边界与本轮中间实现的回归

取消引用覆盖、无输入身份等来自基线；generation 筛选、协调器状态等是本轮新设计。后者的失败属于开发中间版本，不能反过来当作 `a04e940` 已有的机制或缺陷。

| 场景 | 修改前行为 / 本轮发现的风险 | 本批改法 | 收益与证据边界 |
| --- | --- | --- | --- |
| A 正在运行，B 普通 prompt 排队，此时 abort | RPC 在获取锁前改写 active task，B 可覆盖 A 的取消引用 | 一个协调器持有真正 owner，普通请求各自拥有结果 Future | 已复现旧实现取消错对象；新取消回归通过 |
| Loop 已结束，后台结果稍后到达 | 结果进入 follow-up 队列后可能没有人再次检查 | 完成回调校验归属，再合并唤醒唯一 driver | 已有调度器 / RPC 回归；最终真实链路演示待补 |
| stop 后立刻发新请求 | 旧清理不能吞掉新请求，也不能让新 owner 提前开始 | 停止受理点清旧输入；新请求等待旧清理；预留稳定 generation | Event 屏障回归已通过；没有依赖固定 sleep 猜时序 |
| 旧后台结果在 stop 或切会话后到达 | 不能在完成时把“现在的 generation”冒充任务归属 | owner 和完成回调都在任务启动时冻结 | 已验证 hook 重绑不改变旧任务的归属；结果仍保留在任务记录中 |
| 连续两次 stop | 第二次 `Task.cancel()` 可打断第一次取消的异步 finally | 对已处于 cancelling 的 owner 不重复注入取消；等待清理 | 主代理实际复现过 `cleanup_completed=False`；新增回归通过 |
| 两条文字完全相同的运行期输入 | 仅凭文字无法安全定位要撤回的那一条 | 每条输入独立 `input_id`；按 queued / claimed / appended 等状态推进 | 账本与前端组件验证通过；真实内核接线尚未完成 |
| 持续工具调用、不断追加 follow-up | 配置字段或提示词不等于本地硬上限 | 同一意图共享轮次计数与 monotonic 起始时间 | 预算基础已验证；当前不能宣称实际 Agent 已被强制限住 |

## 3. 已实际增加 / 修改的模块

以下路径描述当前工作区，阶段归属不等于已经提交。

| 文件 / 模块 | 改动 | 与原来相比 |
| --- | --- | --- |
| `src/my_agent_core/run_coordinator.py` | 新增通用 asyncio 运行协调器、`RunTicket`、执行身份、合并 wake、stop / await_idle / close | 从 RPC 请求任务引用中分离出单一运行所有权 |
| `src/my_coding_agent/rpc_scheduler.py` | 新增 RPC 与协调器、输入队列、后台通知之间的适配层 | 后台通知不再只有“入队”这一半动作 |
| `src/my_coding_agent/rpc_server.py` | 普通 prompt / steer / followup / abort 接入协调器；执行事件带身份 | 排队请求不再覆盖当前执行者的取消引用 |
| `src/my_coding_agent/rpc/session_handlers.py` | compact 经 `run_maintenance`；会话变更经 scheduler fence，失败后可重新绑定 | 手工维护复用当前意图；会话失败释放与正常结束分别处理 |
| `src/my_agent_core/agent.py` | 直接并发运行保护；固定后台取消快照；跟踪并等待已发起的 abort 清理 | 清理完成前不释放当前 Agent 的执行保护 |
| `src/my_agent_core/background.py` | 后台启动时捕获 owner 与完成回调，支持按快照取消 | hook 或当前会话后来变化，不会篡改旧任务归属 |
| `src/my_agent_core/message_queue.py` | 增加 owner 信息、原子单条领取、过期输入筛选和执行资格上下文 | 原 FIFO / one-at-a-time / all 能力保留；未来意图输入不由旧执行领取 |
| `src/my_agent_core/input_delivery.py` | 新增投递账本，区分领取与确认进入历史，最近终态 FIFO 最多 256 条 | 从“队列里有 / 没有”提升到可查询的本地投递生命周期；待接入 |
| `src/my_agent_core/run_limits.py` | 新增共享预算与每次执行的 deadline guard | 计数和时间不依赖 prompt；待接入实际执行链路 |
| `my-tui/src/client.ts`、`bridge/*`、`protocol.ts` | 队列 API / 独立通知、断连通知、执行身份透传 | 队列变化不冒充新增执行事实事件 |
| `my-tui/src/interactive/input-delivery.ts` 及界面接线 | 按 ID 展示、状态单调合并、`/queue`、过期执行结束隔离 | 不再用内容字符串猜测是哪条输入；断连显示状态未确认 |
| `my-tui/package.json` | `npm test` 增加 `test/bridge/*.test.js` | 常规前端测试包含 bridge 子目录，减少遗漏跨层适配回归的风险 |

## 4. 借鉴了哪些 Harness

本批依据交接方案中的固定代码快照借鉴机制，**没有整体搬入这些框架，也没有把任务书当作已完成实现**。已通过 GitHub 连接器获取固定版本片段，并分组核对机制；核验范围和未覆盖部分如下，不外推为上游全项目结论。

| Harness | 借鉴的机制 | 在本项目的落点 | 没有照搬的部分 |
| --- | --- | --- | --- |
| [OpenCode：run coordinator](https://github.com/anomalyco/opencode/blob/57719d7fd400721783a059fee78c7d33a21cd9b6/packages/core/src/session/run-coordinator.ts#L6) | 单 owner、忙时合并门铃、判空到 idle 的交接、停止受理与清理完成分离 | Python `RunCoordinator` + RPC 适配 | 没有引入 Effect.ts 或多 worker 服务 |
| [DeepSeek Harness：Inbox](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/inbox.ts#L102) | 输入身份、当前纠正与后续工作分开、领取与交付分开、入队与唤醒分离 | `DeliveryLedger`、Queue / Loop 接线设计 | 没有复制持久事件投影或跨进程恢复 |
| [Hermes：steer ticket / withdraw](https://github.com/NousResearch/hermes-agent/blob/4124b34ff9ddaeebd3af831ff0c3d22a464cec2f/agent/interrupt_control.py#L398) | 按投递身份撤回；已领取与仍排队的消息不同处理 | `input_id`、`queue_cancel`、`too_late` 语义 | 没有实现模型请求 redirect 或远端撤销 |
| [Pi：finishTurn / 继续决策](https://github.com/earendil-works/pi/blob/a05e3a07763c38d23d50207742f7e073e35cd0f8/packages/agent/src/agent-loop.ts#L289) | 显式区分本轮完成与是否继续 | 本地双层 Loop 的停止 / 继续边界设计 | 没有把 UI、全部 provider 或整个 runtime 换成 Pi |
| [oh-my-pi：deadline 边界](https://github.com/can1357/oh-my-pi/blob/f0200abf28e8d314125854600cf726dece495246/packages/agent/src/agent-loop.ts#L1370) | 轮次边界检查 deadline | monotonic 时长与轮次预算设计 | 不宣称能抢占任意同步代码 |
| [OpenCode：steps / toolChoice](https://github.com/anomalyco/opencode/blob/57719d7fd400721783a059fee78c7d33a21cd9b6/packages/core/src/session/runner/llm.ts#L256) | 参考达到 steps 后的请求层收束：追加提示并指定 `toolChoice: none` | 本项目另行设计 `RunLimits` 本地检查与耗尽终态 | 上游该片段不等于本地硬上限；不照搬 provider 特定行为 |

这些链接指向固定 commit，不表示执行当天的最新版本；本项目采用的是 Python / asyncio 的实现和自己的产品默认值。

### 固定快照核验：参考了什么，哪些是本项目自己的设计

- **OpenCode coordinator，原文件第 6–15、27–42、82–115、119–171 行**：核对到按 key 的单 owner、run 合流、wake 合并、清理期的后继唤醒与 `awaitIdle`。本项目把这些约束适配为 asyncio，并自行定义 generation / execution 身份与停止受理序号。
- **DeepSeek Inbox，第 80–113、133–226 行**：核对到 next-step / next-turn 队列、消息 ID 和边界领取；这段没有出现本项目的 steer / followup 命名。参考的是身份与领取机制，本项目原有的两类队列不是本轮从它新增的。
- **Hermes，第 398–432 行**：核对到 ticket 领取后标记已消费，未读缓冲可撤回；消费后或未知 ticket 返回 False。本项目把这类边界扩展为明确的 `too_late` / `not_found` 响应，不说上游已有相同 RPC 协议。
- **Pi，第 289–306、319–323 行**：核对到工具结果进入上下文后调用 `finishTurn`，再显式选择 end / continue。该片段不提供固定轮数；它启发继续决策的位置，不是本地硬上限的实现来源。
- **oh-my-pi，第 1353–1355、1372–1375 行**：核对到轮次结束 / 继续与下一轮边界的 deadline 检查。该片段不证明请求中抢占、具体默认值或本项目的 monotonic guard。
- **OpenCode steps，第 256、271–275 行**：核对到达到 steps 时追加提示并请求 `toolChoice: none`，工具列表仍保留。它属于请求层约束，不能直接等同“本地绝不开始下一轮”的硬限制；本项目选择本地检查与取消边界，实际 Loop 接线仍待完成。

`queued → claimed → appended` 的本地确认语义、256 条终态上限、断连的 `local_error` 展示、产品拟定 50 轮 / 1800 秒等，均属于本项目的组合设计或产品选择，不是上游原样能力或实测最优值。本批投递账本仍是进程内内存状态，不是持久 Inbox。

## 5. 实验过程与失败记录

### E01：取消引用被排队请求覆盖

- 旧交错：A 进入执行区 → B 在锁前改写取消引用 → B 等锁 → abort。
- 实际 RED：首 owner 仍 pending，排队请求反而结算；单条回归按预期失败。
- 修改：单一 coordinator owner；普通请求用独立 Future 排队，停止对准 owner。
- 后验：原 RPC 单文件 31 项通过；后续组合验收已覆盖该回归。

### E02：停止交接与未来输入资格

- 输入在 stopping 期间就需要稳定身份，因此 generation 在根请求受理时预留，取消排队请求可留下空号，不能重新编号。
- 旧运行只消费无归属的兼容输入或匹配本次 owner 的输入；未来 generation 保留到其 driver 启动。
- `discard_stale` 不能把所有“不等于当前”的输入都删掉：同一会话更高 generation 可能是合法的新请求。
- 实验中一条新测试曾自己造成死锁：先等新 driver 开始，再释放旧清理屏障，违反单 owner 的预期顺序。已更正为先接受新输入、释放旧清理，再等新 driver，不能把这个测试错误当成产品死锁。

### E03：重复停止与异步清理

- 主代理用 Event 屏障实际复现：重复 stop 后，旧 cleanup 未完成，新 driver 已观察到 `cleanup_completed=False`。
- 修改：已在 cancelling 的 owner 不再重复 cancel；等待者取消不误取消 owner；逻辑关闭意图与中断执行分开。
- 同时增加后台取消任务的跟踪、job-ID 去重与 `await_abort_cleanup()`，避免 detached 清理尚未结束就释放运行保护。
- 结果：新增相关回归通过；原复现与最终集成演示将继续按同一验收条件核对。

### E04：开发执行权限与测试命令纠偏

- 执行参数中的“完整权限”不等于 Windows 文件 ACL 已允许写入。实际写测试被拒绝后，核对身份与 ACL，仅对项目 `src` / `tests` / `my-tui` 补齐可继承修改权限，保留其余规则。
- 使用同一 DS 工具执行身份做 `r+` 打开立即关闭探针，实际返回 `WRITE_OPEN_OK`，没有改文件内容。
- 一次测试命令通过 `-o addopts=` 清掉了仓库原有 importlib 导入模式，引发同名测试模块冲突。最终组合命令显式保留 `--import-mode=importlib`；不把这个命令造成的问题列为仓库基线失败。

### E05：新增协调器的状态审计

- 本轮中间实现存在一次状态发布遗漏，不属于原项目已验证的基线缺陷。
- 主代理在自然 idle 后接受一个 Event 挂起的 wake，实际观察到 `state='idle'` 而 `active_identity` 非空。
- 修改：wake 受理 / 启动发布真实 running 状态，不能只靠 RPC 层临时拼接 `active_identity` 来掩盖协调器的 idle。
- 同一探针后验实际输出：`state='running'`、`active_identity=True`、`running_invariant_broken=False`。协调器及新维护接缝共 20 项测试通过。
- 会话维护 / 失败释放仍是下一条独立审计，不因该探针通过而一并宣称完成。

### E06：建立持续实验记录

- 根据用户要求新增本文件，记录代码变化、前后对比、收益、Harness 来源和真实验证。
- 新增根目录 `AGENTS.md` 作为本地后续协作入口，要求每批重要变更同步更新本记录，并由主代理统一维护共享日志。仓库现有规则忽略该文件，因此不强行改变其版本控制策略；实验记录本身随本批改动提交。
- 已获取并分组核验表中六处固定快照；明确区分了上游机制、本项目既有功能和本轮自定政策。未覆盖的部分保留边界，不用其他分支替换结论。

### E07：参考来源核验带来的更正

- 不把 OpenCode 的 `steps + toolChoice` 说成本地硬停止；它只提供了这里看到的请求层约束。
- 不把 DeepSeek 片段的 next-step / next-turn 名称写成已经核验到 steer / followup API。
- 不把 Hermes 的布尔撤回结果写成它已提供本项目的 `too_late` RPC 状态。
- 不把本项目内存账本写成持久状态机，也不将 50 / 1800 归为上游测得的最佳默认值。

### E08：把“同意图维护”与“新根请求”区分

- 发现的风险：若手工压缩上下文也直接 stop / close 旧调度器，会丢失同意图后台结果的自动续跑资格，后续还可能重置共享预算。
- 已实现的基础：`submit_continuation` 复用当前 generation、分配新 execution_id 并保留门铃；`run_maintenance` 让维护工作仍经唯一协调器执行，忙碌时明确拒绝，不中断当前模型工作。
- 当前模块证据：协调器维护用例 4 项通过；调度适配维护用例 3 项通过。它们分别包含在其更大的组合集中，不相加成额外总数。
- 真实 RPC compact 与异常 fence 的最后接线正在进行；尚不能将本条标成产品路径全部验收。

### E09：2026-11-11 / 实验记录复核与持续维护入口

- 基线与版本：`codex/scheduler-reliability`，HEAD `a04e940`；记录对象为未提交工作区，尚无阶段提交或 PR。
- 本批目的：根据用户要求，持续维护同一份独立 Markdown；补齐前后差异、优势与代价、Harness 来源、真实验证和待办，不另建重复日志。
- 文档修改：本文件增加本次验证批次、成本对照和后续追加模板；根 `README.md` 增加实验记录入口与同步维护约定。根 `AGENTS.md` 已有每批更新要求，本次保留。
- 前后变化：旧记录仍把 compact / fence 写为待接线；DS-1 核实 `rpc/session_handlers.py` → `rpc_scheduler.py` → `rpc_server.py` 的真实调用链后，更新为已接入同意图维护、指定回归通过。E08 保留当时状态，以本条和当前进度表说明后续变化。
- 关键证据：`session_handlers.py:212` 起的 compact 处理器调用 `run_maintenance`；`rpc_scheduler.py:126` 起在有效当前意图中使用 continuation；`rpc_server.py:177` 起的 fence 在 `finally` 中清除旧 scheduler 并释放绑定限制。
- 仍需审计：多个会话切换请求之间的互斥及失败交错，不能从单次失败路径测试外推为切换原子性。
- 未接通项复核：DS-2 检查 `input_delivery.py`、`run_limits.py`、`loop.py` 并定位 `src` 引用，确认账本与预算仍为独立基础模块；实际 Loop 尚无投递确认 / 共享预算接线，产品默认限额和预算终态未完成。
- 验证：本次后端指定 15 文件 172 项通过；另补跑会话 RPC / 会话树 RPC 两文件，21 项通过；前端构建通过，132 项通过、2 项跳过，当前 `npm test` 已覆盖 bridge 子目录。命令和退出码见第 6 节；保留此前 107 / 111 / 21 等历史结果，不累加为总量。
- 来源与收益：继续使用第 4 节已核验的固定 Harness 快照；本次未新增来源或上游能力主张。将 OpenCode steps 的表格表述也修正为请求层收束，与下文核验结论一致。记录现在可区分设计、实现、局部测试、真实接线和交付阶段；代价是后续每批都要维护证据。
- 交付边界：本条完成的是实验记录的维护及状态复核，完整调度改造、免费演示、提交与 PR 仍按当前进度推进。

### E10：2026-11-11 / 将项目快照传至 L20

- 目标与授权：用户指定 `/data/llma/my-agent`；通过已配置的 `L20` SSH 别名以 `llma` 账户传输。传输前目标目录存在且为空，可用空间约 561.43 GiB。
- 基线与范围：本地分支 `codex/scheduler-reliability`，HEAD `a04e94050855ac460ec400a911d5405186d2adba`；复制实际工作区，包含未提交和未跟踪的项目内容，不是只复制 HEAD。
- 前后对比：服务器空目录 → 包含源码、测试、TUI、文档及实验数据的项目副本。首批为 808 个项目文件、321,076,568 字节（约 306.2 MiB），服务器 `du -sh` 返回约 309 MiB；本条实验记录在初次验证后补充同步，最终逐文件清单以服务器 `.transfer-manifest.json` 为准。
- 排除内容：`.git`、`.venv` / `venv`、`node_modules`、Python 字节码和常见测试 / 工具缓存。打包器不跟随符号链接或目录联接，并排除真实 `.env` 与私钥类文件；本批额外跳过路径为空，示例配置仍保留。
- 优势与代价：避免搬运 Windows 环境和可重建缓存，保留当前开发成果及实验数据；服务器仍需按 Linux 环境另装依赖。服务器没有随此次传输获得 Git 历史、模型权重或已运行服务。
- 传输与验证：本地临时 gzip 包 96,821,040 字节（约 92.3 MiB）；通过 SCP 上传后先比对包 SHA-256，再解到同级临时目录，按清单逐文件核对大小和 SHA-256，最后替换仍为空的目标目录。首批校验在北京时间 17:12:49 完成，808 项全部通过；归档 SHA-256 为 `b6e4cbf6bb3ff7bf4c7f2a0e1e470fd7b49c89c6b1128df2797c92433fba8e20`。
- 失败与修正：Windows 虚拟环境没有 `tzdata`，首次打包脚本的 `ZoneInfo("Asia/Shanghai")` 初始化失败，未开始上传；改用明确的 UTC+08:00 时间偏移后完成打包，没有为此增加项目依赖。
- 证据位置：服务器 `/data/llma/my-agent/.transfer-manifest.json` 保存相对路径、文件大小、SHA-256 和快照版本；`.transfer-verification.json` 保存校验结果。临时上传包在验证后清理，不作为项目长期副本保留。
- Harness 来源：本批仅完成工作区传输，没有新增 Harness 机制或来源主张；代码参考仍见第 4 节。
- 交付边界：文件已传输并校验。服务器依赖安装、模型配置、应用启动和 Linux 上的行为测试尚未执行；这不改变 E09 列出的功能接线待办。

在服务器上复核已传文件：

```bash
cd /data/llma/my-agent
python3 - <<'PY'
from pathlib import Path
import hashlib, json
root = Path.cwd()
manifest = json.loads((root / ".transfer-manifest.json").read_text(encoding="utf-8"))
for entry in manifest["files"]:
    path = root / entry["path"]
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    assert path.stat().st_size == entry["size"], entry["path"]
    assert digest.hexdigest() == entry["sha256"], entry["path"]
print("ALL_FILES_VERIFIED", len(manifest["files"]))
PY
```

### E11：2026-11-11 / 建立 DSH-1 并实测持久会话缓存

- 目标与授权：用户授权设置一名 DSH 子代理并测试缓存。逻辑名称为 `DSH-1`；旧的 DS-1、DS-2 继续暂停，没有通过原 DS 子代理工具恢复它们。
- 修改范围：在本机根 `AGENTS.md` 记录当前助手状态、DSH 调用方式及限制；本文件追加本条；新增 [逐轮原始用量与计算结果](results/2026-11-11-dsh-cache-smoke.json)。没有修改项目业务代码或服务器依赖。
- 配置与实际调用：桥使用本机已有的 `headless` profile；已核对默认配置 `opencode-go` / `deepseek-v4.1-flash` / `reasoningEffort: max`，上下文配置为 1,000,000 token。配置值不等于已完成满上下文验证。DSH MCP 本次确实完成三次调用，状态返回 `bound=true`、`turns=3`、`ttl_seconds=3600`。
- 会话身份：工作目录 `C:\Users\MLL\Desktop\my-agent-harness`；绑定 `session-55444d0d-9c18-403b-96df-b34df532ef3e`。首轮 `reused_session=false`，后两轮均为 `true`；后两轮正确复述首轮标记，并分别回答 `17×24=408`、`17×25=425`。
- 实验方法：顺序调用三次 `mcp__dsh_bridge.dsh_delegate`。首轮注册 `DSH-1` 并记住 `HARNESS_DSH_CACHE_20261111_A`；后两轮显式传入同一 `session_id`，要求复述标记并计算。三轮均禁止调用工具、读写文件和访问网络，没有用大段填充文本人为增加公共前缀。
- 统计口径：本次 DSH 返回的 `usage.inputTokens` 是未命中输入；`usage.cacheReadTokens` 是缓存读输入，故命中率为 `cacheReadTokens / (inputTokens + cacheReadTokens)`。第二轮 `251 + 10240 + 96 = 10587`，第三轮 `269 + 10368 + 17 = 10654`，与其 `totalTokens` 一致。不能将这里的 `inputTokens` 直接当成旧 Codex 日志中的全部输入。

| 轮次 | 会话复用 | 未命中输入 token | 缓存读 token | 全部输入 token | 命中率 | 工具返回耗时 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 否 | 10,319 | 0 | 10,319 | 0.00% | 14.0 秒 |
| 2 | 是 | 251 | 10,240 | 10,491 | 97.61% | 10.5 秒 |
| 3 | 是 | 269 | 10,368 | 10,637 | 97.47% | 9.0 秒 |

- 加权结果：后两轮缓存读 20,608 token、全部输入 21,128 token，命中 **97.54%**；计入首轮后，三轮全部输入 31,447 token，合计命中 **65.53%**。首轮未返回 `cacheReadTokens` 字段，桥报告 0%，总 token 分解也与无缓存读一致。
- 前后变化与优势：此前当前工作区未绑定 DSH；本次建立一个可复用的会话，并验证后续调用可保留标记和读取缓存。桥直接返回逐轮 `usage`，主代理可及时核对缓存状态。
- 代价与证据边界：本次仅为三轮无工具短提示实验，两轮复用不足以证明长期稳定性；包含首次调用的总体值明显低于复用后值。不能把它与此前 DS-1 / DS-2 的源码阅读任务直接当作同工作负载对照，也不宣称已经达到说明文档中的约 99%。未验证实际源码修改、系统层只读隔离、写权限或满上下文运行。
- Harness 借鉴：本条采用 DeepSeek Harness 的持久会话与上下文延续能力，通过本机 `dsh-bridge` 调用。这里验证的是运行行为，没有对 DSH 上游源码做新的固定快照审计；项目调度内核的参考来源仍以第 4 节为准。
- 交付状态：DSH-1 已绑定并完成缓存测试；暂未派发后续开发任务。会话复用窗口配置为 3600 秒，过期 / 重置后的调用不能沿用本轮命中率结论。本批记录仅更新本地，L20 仍为 E10 快照。

## 6. 当前可复现的验证证据

以下保存已完成验证批次的实际结果，不能自动套用到之后尚未验证的改动。集合有重叠，**不能相加当作测试总量**，也不能替代最终完整 CI。后续新增测试后，同一命令的数量可以增加。

### 2026-11-11 本次复核

运行环境：项目 `.venv`；工作分支 `codex/scheduler-reliability`，HEAD 仍为 `a04e940`，验证对象包含当前未提交改动。

| 检查 | 实际结果 | 验证范围 |
| --- | --- | --- |
| 后端 15 个指定测试文件 | **172 passed in 8.01s**，退出码 0 | 调度、维护 fence、后台、队列、Agent 所有权、投递账本和预算基础；不代表未接线的产品功能已完成 |
| 补充会话 RPC / 会话树 RPC 两文件 | **21 passed in 4.91s**，退出码 0 | 对 DS-1 指出的会话维护边界补跑现有回归；不覆盖全部并发切换交错 |
| `my-tui` 下 `npm test` | **132 passed，2 skipped，0 failed**，退出码 0；TypeScript 构建通过 | 当前脚本已同时包含根测试目录与 bridge 子目录，无需把 bridge 再测结果相加 |

后端实际命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core/test_message_queue.py tests/core/test_agent_steering.py tests/core/test_agent_loop_pure.py tests/core/test_background.py tests/core/test_run_coordinator.py tests/core/test_run_coordinator_maintenance.py tests/core/test_agent_ownership.py tests/coding/test_rpc_scheduler.py tests/coding/test_rpc_scheduler_maintenance.py tests/coding/test_rpc_maintenance_fences.py tests/coding/test_rpc_server.py tests/coding/test_agent_steering.py tests/core/test_input_delivery.py tests/core/test_run_limits.py tests/core/test_execution_budget_guard.py -q -o addopts= --import-mode=importlib
```

补充会话回归实际命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/coding/test_session_rpc.py tests/coding/test_session_tree_rpc.py -q -o addopts= --import-mode=importlib
```

前端实际命令：

```powershell
Set-Location my-tui
npm test
```

`my-tui/package.json` 当前展开为 `npm run build && node --test test/*.test.js test/bridge/*.test.js`。此前遗漏 bridge 子目录的问题已在脚本范围上修正；以下旧批次保留当时的数量和独立执行方式。

### 此前验证批次（保留历史）

| 检查 | 实际结果 | 含义 |
| --- | --- | --- |
| 初始指定 Python 基线集合 | 通过 | 修改前选定用例可运行 |
| 此前后端组合集合 | **107 passed** | 队列、steering、纯 Loop、后台、coordinator、ownership、RPC 等相关回归 |
| 此前前端 `npm test` | **111 passed，2 skipped** | 包含队列 UI 的假内核测试；跳过项为既有条件跳过 |
| 单独 bridge 测试 | **21 passed** | 根目录 test glob 当时没有包含 bridge 子目录，故单独执行 |
| 独立投递账本 | **11 passed** | 状态、撤回、同文不同 ID、有界终态；尚非真实内核链路证明 |
| 预算 + deadline guard | **40 passed** | 共享计数、时间、取消原因与 guard；尚非实际 Agent 限制证明 |
| 本批已修改 Python 的 Ruff | 通过 | 静态检查，不等于行为已全部验收 |

此前后端组合命令（使用仓库 Python 3.13 虚拟环境）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core/test_message_queue.py tests/core/test_agent_steering.py tests/core/test_agent_loop_pure.py tests/core/test_background.py tests/core/test_run_coordinator.py tests/core/test_agent_ownership.py tests/coding/test_rpc_scheduler.py tests/coding/test_rpc_server.py tests/coding/test_agent_steering.py -q -o addopts= --import-mode=importlib
```

此前前端与桥接检查：

```text
cd my-tui
npm test
node --test test/bridge/kernel-bridge.test.js test/bridge/event-translator.test.js
```

## 7. 当前优势与不能夸大的边界

已验证的收益是**故障行为更明确、输入与执行的归属可解释、异步交错可以确定性回归**，不是已经测出了延迟或吞吐提升。

- 一个 owner 承担运行，停止有明确受理点和清理边界，便于排查“取消错人”和“旧清理吞新输入”。
- generation 表示意图，execution_id 表示一次实际执行，input_id 表示一次投递，三者职责不同。
- claimed 只表示被领取；appended 只表示已确认进入本地历史，不表示远端模型已收到、服从或完成工作。
- 相同文本可独立追踪与撤回；终态有界，活跃输入不会为满足 256 条上限而误淘汰。
- 队列更新独立于执行事实，保持既有事件种类的职责；前端不会用旧执行结束去清掉新执行状态。
- 预算接入完成后才可以声称持续执行受本地上限控制；当前只能确认预算基础测试通过。

本批不承诺端到端 exactly-once、进程重启恢复、跨进程租约、零延迟 steering、全部任务完成，或停止后回滚所有外部副作用。时长上限是协作式异步取消，不能抢占阻塞事件循环的任意同步代码。

### 收益对应的代价

| 选择 | 得到什么 | 付出的代价 / 使用限制 |
| --- | --- | --- |
| 单 owner 与异步清理交接 | 运行和取消对象唯一，新请求有明确启动顺序 | 同一 Agent 的新根请求需要等待旧清理；不能把并发提交数量当成模型并行度 |
| generation / execution / input 三种身份 | 可区分工作意图、实际执行和单次投递 | 跨核心、RPC、前端要维护一致字段，增加接线与回归测试成本 |
| 内存账本与 256 条最近终态 | 可按 ID 查询 / 撤回，终态记录有界 | 进程重启不恢复账本；较旧终态会淘汰，不能作为永久审计存储 |
| 同意图共享执行预算 | 自动续跑不能借重启 driver 重置额度 | 时间预算计入同意图等待；耗尽后需要用户明确发起新运行；产品接线仍待完成 |
| 固定快照借鉴机制 | 设计来源可核对，便于解释每项取舍 | 需要按上游具体版本核验；本地组合设计的正确性仍要靠本项目测试证明 |

## 8. 后续更新规则

后续每批代码、协议、配置默认值、测试结果或已知限制变化，都更新本文件：

1. 更新“当前进度”，明确是基础模块完成、真实链路完成，还是已提交 / 已通过 CI。
2. 追加日期、基线 / 提交、问题或假设、具体文件、修改前后行为、优势与代价。
3. 记录真实命令、结果、失败和修正；没有实测的数据不填数值。
4. 新增 / 调整 Harness 参考时，保留固定 commit 与机制映射，区分源码核验与设计启发。
5. 提交 / PR / CI 完成后补链接和 commit；后续修改继续追加，不用新结果覆盖旧失败历史。

每批追加使用以下格式；无实测结果时明确写“未运行”或“待验证”：

```markdown
### E编号：日期 / 本批实验名称

- 基线与版本：分支、HEAD、工作区或提交链接。
- 问题 / 假设：可观察的触发场景与验收条件。
- 本批修改：具体文件与责任范围。
- 前后对比：原行为 → 本批行为。
- 优势与代价：得到什么，增加什么成本或限制。
- Harness 借鉴：固定 commit、对应机制；自定设计单独注明。
- 验证：实际命令、退出码、结果、失败与修正。
- 交付状态：基础模块 / 真实链路 / 本地验证 / 提交 / PR / CI。
- 未完成项：下一步和仍缺的证据。
```

### 待补的交付证据

- 并发会话切换及失败交错的最终审计结果；compact / 单次 fence 的当前证据见 E09。
- 真实 Queue → Loop → Session → RPC → TUI 投递链路与撤回验证。
- 共享预算接入、产品默认值和 budget_exhausted 跨层元数据。
- 四个免费假模型演示及事件时序。
- 分阶段提交、PR、完整 CI 与最终可使用的简历表述。

### E12：2026-11-11 / 大工具输出外置的源码导学与回读边界核验

- 基线与版本：HEAD a04e940，工作分支 codex/scheduler-reliability；保留其他进行中的工作区改动。
- 问题 / 假设：工具长文本落盘后，是否自动形成模型可按需读取的完整链路。
- 本批修改：新增 docs/learning/03E-大工具输出落盘-预览与按需回读.md，更新学习索引；本批未修改运行代码、协议或权限。
- 前后对比：补充区分“完整文件已保存”“模型看到有界预览”“读取工具可访问该产物”；不再将 Full 路径提示直接等同于回读链路已接通。
- 源码事实：ContextManager 先按 80% 预算门控；budget_tool_results 默认对超过 20,000 字符的纯文本 tool 消息落盘，发送视图使用前 2,000 字符；原消息不直接修改，写盘 OSError 时保留原文。同名 tool_call_id / 下标可覆盖旧文件，不是不可变 artifact id。
- 回读边界：默认用户会话布局下，结果目录通常在用户数据目录的 sessions/tool-results；read 默认通过工作区路径校验。本批隔离验证确认工作区外路径被拒绝，尚不能宣称默认完整回读已接通。自定义路径或另行装配可改变结论，未检查运行中的具体用户配置。
- 优势与代价：外置视图减少反复内联的大文本，原文可留存；需额外磁盘、可靠归属与受限回读。首部预览可能遗漏末尾报错。
- Harness 借鉴：Pi a05e3a07763c38d23d50207742f7e073e35cd0f8 的 bash 尾部与完整文件；oh-my-pi f0200abf28e8d314125854600cf726dece495246 的 artifact 复用；OpenCode 57719d7fd400721783a059fee78c7d33a21cd9b6 的行/字节限制；Hermes 4124b34ff9ddaeebd3af831ff0c3d22a464cec2f 的头尾与 spill 处理；DeepSeek fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 仅核验输出契约，未声称默认外置策略。
- 验证：实际运行 python -m pytest tests/core/test_context.py -q -k 'budget_tool_results or prepare_gate'，退出码 0，5 项通过；隔离 TemporaryDirectory 验证全文保存、原消息不变、尾部错误未进入首部预览、外部路径默认拒绝，断言通过。
- 助手验证：通过 dsh_status 确认已有会话后，DSH-1 完成两次限定文件只读调查并返回最终证据；未调用暂停的旧 DS 助手，未重置会话。任务级只读不等于系统沙箱只读，不将本次短任务缓存数据外推成固定收益。
- 交付状态：本地源码和局部行为已核验，学习文档已写入；本批未提交、未创建 PR，未运行外部 Harness 端到端链路。
- 未完成项：完整产物的归属隔离、唯一引用、受限读取和清理策略仍需独立设计与真实链路验证；不全局放开工作区外读取来掩盖缺口。

### E13：2026-11-11 / 借鉴 DSH 的模型输入投影与前缀复用设计核验

- 基线与版本：HEAD a04e940；本次未修改运行代码，保留当前工作区其他实现。
- 问题 / 假设：DSH 的高缓存复用经验能否直接变成本地“高 KV 命中”的保证；本地已有前缀稳定机制还缺什么。
- 本批修改：新增 docs/handoffs/2026-11-11-context-snapshots-prefix-cache-codex.md，更新学习索引；提出请求投影诊断、分支绑定摘要快照、产物受限回读三项待实施交付。
- 前后对比：明确已有摘要前缀、边界 key 与 OpenAI 用量净额归一化不应重做；把“借 KV”改为“借鉴稳定、可追溯的请求与上下文投影”。
- 固定 DSH 源码：fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159，核验 agent-loop/runtime-context.ts、agent-loop/agent.ts、session/request-header.ts。动态上下文比较全文，不变不追加；变化追加快照。SystemPromptProjection 追加/替换受路由能力与系列状态约束。canonicalHeader 不排序 tools。
- 本地事实：摘要缓存选择遍历全树按深度；超预算后的历史工具预览替换可能改变已发送前缀；OpenAI 适配器已拆分净非缓存输入与缓存读取，原始 total_tokens 保持服务商口径不能误当需再减一次的错误。
- 优势与代价：明确每个快照和每次前缀变化的来源，利于恢复、诊断和对照实验；引入版本、归属、Provider 适配及测试成本，不能以缓存为由保留过期系统规则。
- 助手与会话证据：调用前 dsh_status 显示旧会话 idle 超过 3600 秒；实际委派返回新会话 reused_session=false，后续 dsh_status 确认新绑定，第二次本地调查 reused_session=true。未无故 reset=true，未调用暂停旧 DS。工具短任务缓存口径不外推为 my-agent 整体收益。
- 验证：实际运行 python -m pytest tests/core/test_context.py -q -k 'preserves_prefix_invariance or discrete_epoch_compaction_establishes_stable_prefix or repeated_prepare'，退出码 0，选中并通过 2 项；没有进行真实 API 缓存对照。
- 交付状态：源码核验与设计交接已写入，本批未实现新增功能、未提交、未建 PR；已有调度可靠性中间实现状态不因本记录而改变。
- 未完成项：分阶段实现、产物读取闭环、Provider 兼容测试、真实工作负载冷/热/压缩/回退/resume 对照，以及加权用量和任务总成本评估。

### E14：2026-11-11 / 压缩管线与结构化语义摘要源码导学

- 基线与版本：HEAD a04e940，codex/scheduler-reliability 工作区；保留其他进行中的源码和文档改动。
- 问题 / 假设：截图“三层压缩”是否对应当前三个独立算法；结构化摘要如何支持下一轮继续执行。
- 本批修改：新增 docs/learning/03F-压缩管线与结构化语义摘要-用任务交接理解.md，更新学习索引；未修改运行代码、协议或配置。
- 前后对比：澄清当前无独立 snip/micro 层，实际是预算门控、工具长文本外置、仍超阈值才摘要；摘要请求使用注入的 self.llm、tools=[]，执行视图保留原 system 与近期消息。失败或空摘要退回原消息，不保证解决窗口溢出。
- 优势与代价：六栏目交接更便于保存目标、限制、证据与下一步，可复用稳定摘要；会增加摘要调用、等待和信息损失风险，Markdown 提示不保证事实真实，也不自动更新 TaskStore。首次摘要可能接收原始长消息，不宣称只读取落盘预览。
- Harness 借鉴：Pi a05e3a07763c38d23d50207742f7e073e35cd0f8 六栏目；Hermes 4124b34ff9ddaeebd3af831ff0c3d22a464cec2f 未验证假设栏目；OpenCode 57719d7fd400721783a059fee78c7d33a21cd9b6 交付状态与相关文件规则；oh-my-pi f0200abf28e8d314125854600cf726dece495246 仅核验压缩方式设置与顺序；DSH fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 复用前次运行时上下文快照证据，不冒充其语义摘要实现。
- 验证：运行 .\.venv\Scripts\python.exe -m pytest tests/core/test_context.py -q -k 'prepare_below_threshold_no_summary or prepare_gate_above_threshold_triggers_compact_summary or prepare_trigger_summary_non_destructive or summary_call_shape or cache_reused_no_resummary or iterative_resummary or summary_failure_degrades or summary_prompt_includes_6_sections'，退出码 0，8 项通过。
- 验证限制：DSH-1 指出失败降级测试的假 LLM 参数名可能触发 TypeError，故通过仅证明异常回退，不声称预期网络异常被精确模拟；尚未修正测试。无真实 API 摘要质量、缓存命中与费用对照。
- 助手与状态：复用当前授权 DSH-1 会话完成限定 context.py 与摘要测试的调查；未调用暂停旧 DS、未 reset。只读是任务范围约束，非系统隔离证明。
- 交付状态：本地源码核验与课程文档，未提交、未创建 PR、未运行外部 Harness 链路；相关增强仍为提案。
- 未完成项：摘要质量与假设保真评估、失败测试精确化、分支绑定快照、受限原文回读及真实整任务成本测量。

### E15：2026-11-11 / 摘要查询通路与低成本上下文控制设计讨论

- 基线与版本：HEAD a04e940，当前工作区；本节不改变业务代码及已有实验基线。
- 问题 / 假设：X.parent=F 且 current=F 时，只沿父链无法发现摘要 X；独立成本裁剪层是否存在、如何设计。
- 本批修改：新增 docs/learning/03G-如何找到摘要与低成本上下文控制设计.md，更新学习索引。
- 已核验事实：Session.get_current_path_messages 读取当前对话父链；get_latest_compaction_cache 独立枚举 tree.entries.values 中的 CompactionEntry 并按路径深度选择；ContextSessionBridge.restore_cache 装回 ContextManager，运行中也直接复用 _summary。当前选择没有先严格按当前分支过滤，增强提案不当作已实现。
- 设计建议：新工具结果一次生成有界发送视图，规则只处理确定格式，完整文本保留并受限回读；已发视图在同一版本保持稳定，必要时明确切换版本，再按需语义摘要。不将工具外置与独立 snip/micro 层混称。
- 优势与代价：减少反复改写旧前缀、保留失败证据和来源；增加视图、产物权限、版本与质量评估成本，未测量费用或缓存收益。
- 验证：主代理核对 session.py:278/286/321、context.py:320/368/645 与 agent.py:269 的必要片段；新学习文件的围栏和本地链接检查通过。纯文档/设计变更未重复运行业务测试。
- 助手边界：原先按 cwd 复用的 DSH 返回 active write handle 占用，未重置或终止其进程；用户随后授权建立按 Codex 对话隔离的新会话，相关运行配置单独记录。
- 交付状态：教学文档与待实施建议；未提交、未建 PR、未实现新的上下文裁剪策略。

### E16：2026-11-11 / DSH 按 Codex 对话隔离与项目讲解交接

- 用户要求：项目讲解暂停并转交 DSH；本代理继续完成桥接。每个 Codex 对话各用独立 DSH，同对话复用，不借用其他对话会话。
- 修改范围：全局 C:/Users/MLL/.codex/mcp/dsh_bridge_server.py、dsh_conversation.py、test_dsh_bridge_conversations.py、README-dsh-bridge.md；全局及本项目 AGENTS.md 加入隔离规则；新增 docs/handoffs/2026-11-11-dsh-project-learning-prompt.md。不修改 my-agent 业务源码或改前基线。
- 前后行为：旧版仅以 cwd 查找绑定 → 新版按 cwd+conversation_id；显式 ID 优先，默认可取 CODEX_THREAD_ID。当前旧 MCP 未热加载新 schema，CLI 从当前 Shell 获取对话 ID，已立即生效，新 MCP 连接再使用新参数。
- 并发与持久化：scope 独占锁避免同助手并发写；不同 scope 可独立执行。状态短事务跨进程加锁、原子保存并重新加载合并；模型执行不占状态写锁。新版绑定用独立 .conversations 文件，旧进程仍写旧文件，不重置或终止其他会话。失败不覆盖既有成功绑定。
- 执行过程：独立 DSH 首轮在 240 秒限时内保存桥接核心，但未完成 CLI / 测试，进程树按限时退出；主代理接续集成。保留已有时间戳备份，不把未完成助手输出当最终交付。
- 首次测试失败及修正：跨进程锁在获锁前读写锁字节，触发 Windows PermissionError；改为直接锁定字节范围（允许超 EOF）。真实 CLI 首次验收被本地代码页破坏 JSON 中文输出；改为显式 UTF-8，并增加回归。
- 本地验证：运行 .\.venv\Scripts\python.exe C:/Users/MLL/.codex/mcp/test_dsh_bridge_conversations.py，退出码 0，12 项通过；覆盖不同对话隔离、同对话复用、旧状态不污染、显式 SID、reset 范围、环境 ID、失败不覆盖、线程忙碌、四进程合并、跨进程同 scope 忙碌和 CLI 编码/缺失 ID。
- 真实验证：CLI 同时启动两个 scope，分别得到不同 session_id；当前对话后续复用同 ID，准确返回上一轮标记。三个成功结果及复用状态保存到全局 mcp/dsh_conversation_validation.json。此次短提示不代表多步用量完整性、满上下文验证或固定缓存收益。
- 优势与代价：避免同项目跨对话争抢写会话；新增对话首次有冷启动成本，多个助手仍共享工作区，必须继续划分文件所有权。已加载旧 MCP 的新参数需要下一连接生效，CLI 可直接使用。
- 交付状态：全局桥接代码与规则已更新、本地单测和真实短链路已验证；项目学习交接 prompt 已保存，讲解暂停。未提交、未建 PR，未触碰其他正在运行的实验和服务。

### E-CACHE-01：2026-11-11 / 冻结上下文增强前基线并搭建三臂实验

- 用户选择：先建立对照实验并保存改前基线，再按上下文任务书分阶段改造、复测；新增 L20 约束为第三张卡 GPU 2，GPU 进程必须 `CUDA_VISIBLE_DEVICES=2`。
- 设计入口：[缓存对照执行方案](../handoffs/2026-11-11-context-cache-comparison-plan.md)。三臂为当前工作区冻结源码、增强后源码、实际 DSH headless；统一模型、任务和预算，披露原生工具与接口差异。
- 基线：HEAD `a04e94050855ac460ec400a911d5405186d2adba`，含原有未提交调度改动；`experiments/context_cache/baseline/source-before.tar.gz` 保存 95 个源码与依赖文件。SHA-256 `698687fcb8564b2dae6db3086d3182d5392e7fa76cde6404f38be438523c50dd`；归档及清单只读保留。
- 初步文件：DSH-1 在独占的 `experiments/context_cache/` 中实现数据获取、用量归一化、源码解包、my-agent / DSH 运行入口、原始记录与汇总；新增专用测试。主代理维护本记录、方案和根协作约定。业务源码增强尚未开始。
- 开源任务：已核验 HumanEval 为 MIT，下载原始数据 SHA-256 `b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef`，首批选择 0/1/2。HumanEval 上游已停止维护，本轮只作为无 Docker 的代码 smoke；SWE-bench Lite 与 Terminal-Bench 的正式任务/判分作为后续工程级入口。L20 当前未发现 Docker，不将缺环境的任务计作模型失败。
- 既有行为基线：运行 `.\.venv\Scripts\python.exe -m pytest tests/core/test_context.py tests/core/test_context_boundary.py tests/core/test_session.py tests/coding/test_read_tool.py tests/core/test_task_context_projection.py -q -o addopts= --import-mode=importlib`，退出码 0，**76 passed in 2.09s**。先前误写 `test_tools_read.py` 的命令未执行测试，已纠正为实际文件名，不作为产品基线失败。
- 初版验收失败：`runs/before-humaneval0` 虽完成真实 API 调用并通过一个代码测试，实际只运行 `LLM.chat`，未经过 Agent / ContextManager / Session，因此只能算接口连通记录，不能证明上下文改前行为。
- 初版统计更正：部分请求缺少缓存字段时，已知子集的 62.54% 不能充当整个任务比率；正常单次请求的输入也可能单调增长，不能由此猜测为累计计数。已交 DSH-1 改为明确口径、事件身份与完整性校验，保留旧失败文件。
- DSH 观察边界：桥只保留最后一条 `status.usage`。两次单步探针确认复用会话可工作，但未证明一个多工具任务内的用量覆盖完整；还需原生多步探针及实际请求观测，辅助模型调用缺失时必须标记 partial。
- 助手边界：旧 DS-1/DS-2 保持暂停。首次复用旧 DSH 会话返回 active write handle 占用错误，未终止其进程；为本轮新建独立 DSH-1 会话后继续，保留其他会话和既有改动。
- 当前状态：实验入口正在修正验收，尚无合格的三臂工程任务缓存差距结论；三个业务增强阶段尚未实施，不预填提升比例或完整任务费用。

### E-CACHE-02：2026-11-11 / 原生改前样本与统计验收更正

- 修改范围：实验目录新增 `worker_native.py`，经真实 `CodingAgent.run_stream`、Session 和 7 个编码工具执行；旧 LLM-only 驱动保留且标记不具备原生基线资格。增加 HumanEval MIT 许可证全文，原始数据和冻结源码归档 SHA-256 均保留。
- 用量更正：归一化器要求显式 `per_call` / `cumulative` / `unknown`，按事件身份去重；单调增长不推定累计。缺少完整字段时整体命中率为 null，已知配对子集独列 coverage，不充当整体。新增单次 10/20/30 总和 60、显式累计差分为 30 等反例。
- 验证：主代理实际运行 `.\.venv\Scripts\python.exe -m pytest tests/experiments/test_context_cache.py -q -o addopts= --import-mode=importlib`，退出码 0，**18 passed in 0.13s**。离线 smoke 与逐步探针文件保留，初版失败记录不删除。
- 合格改前 smoke：`experiments/context_cache/runs/before-native-humaneval0/`，原生 `HumanEval/0` 通过；6 次模型请求、2 个工具步骤、约 30.9 秒，模型为 `deepseek-v4.1-flash`、强制 max，无自动降级。原始 SDK 记录显示全部输入 16,197 token、缓存读 12,160 token，**本样本缓存读比例 75.08%**。
- 分类限制：该版驱动的 first/reuse 暂按用户 turn 分组，首 turn 内含两个模型请求；不能将 first 桶误称首次 API 请求或强制冷启动。后续观测器将按真实请求序号区分，已有原始逐调用记录可重算。
- DSH 原生多步探针：`runs/dsh-probe-steps-1/probe_steps.json`；同一次 headless 运行强制读取文件，观察 1 个工具调用、2 条带 usage 的 status。第一条 `10795+48=10843`；第二条 `175+10752+10=10937`，支持每条是单请求量。辅助 title / summary 用量仍未覆盖，明确 `sufficient_for_task_totals=false`，暂不输出完整三臂排名。
- E11 边界：其三轮均禁止工具，每轮单请求的短提示计算仍保留；新发现的是多工具任务不能只读取桥返回的最后一条用量，不把桥最后一步高命中当整任务成绩。
- 下一步：已派发真实业务增强第一阶段（动态状态快照与 Provider 请求诊断），尚未验收；后续需要临时 DSH 配置覆盖与请求用量观测，再做完整对照。
- 会话规则：用户追加按 `CODEX_THREAD_ID` 隔离 DSH；当前已启动阶段任务继续收尾，后续采用当前对话的新入口，不抢占其他对话写会话。L20 GPU 2 约束继续有效。

### E-CODE-SOL：2026-11-11 / 取消缓存对照，改用 Sol 收尾代码

- 用户决策：取消进一步缓存命中率对照与开源基准执行；弃用当前工作的 DS / DSH 执行助手，指定 **GPT-6.1 Sol / medium** 子代理完成上下文增强代码。
- 实际配置：通过原生子代理创建接口分别显式指定 `model=gpt-6.1-sol`、`reasoning_effort=medium`、`fork_turns=none`，启动 SOL-1、SOL-2。主代理继续负责设计、文件所有权、集成和最终验收。此设置针对开发助手，不修改产品默认模型或供应商。
- 分工：SOL-1 负责运行态/请求投影、分支摘要与 Core Context 集成；SOL-2 负责产物存储、回读工具、CodingAgent 装配和权限分类。旧 DS / DSH 不再派发任务，不删除其他对话的全局服务或历史会话。
- 范围变化：保留只读改前源码档案、历史测量与失败记录；实验 README 与对照方案增加停止标记。不再运行真实 API、HumanEval、SWE-bench 或 DSH 对照，未得到完整三臂结果，不宣称缓存/费用提升。只保留完成代码所需的定点离线回归与静态检查。
- 收尾重点：核对此前半成品的真实链路；修正摘要覆盖身份及恢复、运行中动态快照、请求诊断的配置变化与隐私，以及稳定产物引用和受限回读。
- 最终状态（北京时间 22:14 后验收）：本轮三项上下文增强已完成真实产品接线、独立代码复核和定点离线验收；未提交、推送或创建 PR，未同步本轮源码到 L20。原有调度预算 / 输入账本等其他待办不因本轮完成而变为已交付。

#### 实际代码变化与前后对比

| 范围 | 修改前 / 半成品风险 | 当前行为与优势 | 代价与边界 |
| --- | --- | --- | --- |
| `src/my_agent_core/runtime_context.py`、`agent.py`、`loop.py` | 动态事实缺少完整快照投影；初版仅在进入整次运行前注入，中途变化延迟生效 | 每次模型请求在安全边界交付；全文相同不重复，变化追加完整快照，清空显式；模型视图与 Session 保持一致，工具结果配对保留 | 状态变化仍产生新输入；不保证服务端命中率 |
| `src/my_agent_llm/request_projection.py`、`providers/openai.py`、`anthropic.py` | 缺少最终转换后的变化诊断；半成品诊断还保留原始消息，未覆盖部分请求配置 | 同步 / 异步 / 流式真实接缝诊断模型、system、tools、消息与 reasoning / tool_choice 等配置；只保留指纹和有界原因，记录流式降级后的实际参数 | 本地诊断不能还原服务端 token 前缀或 KV 状态 |
| `src/my_agent_core/context.py`、`session/session.py`、`agent.py` | 全树深度选择摘要可能错用其他分支；半成品 covered_count 与注入 system 存在偏移；相同尾部可能掩盖已改写的覆盖前缀 | canonical 覆盖前缀、Entry IDs 和内容指纹共同验证；拒绝无关摘要；切分支、resume 与不同 Session 重新对齐；经验证的空尾摘要可恢复；保留原始节点和合法 Floor | 来源无法证明时失效 / 重建；需要维护版本、身份和指纹 |
| `src/my_agent_core/artifacts.py`、`src/my_coding_agent/tools/artifact.py` | 原文保存后仅给工作区外 Full 路径；按 call ID 命名可能覆盖；回读还可能被通用序列化再次截断 | 工作区归属的稳定内容引用；相同 call ID 异内容不覆盖，重复投影稳定；`artifact_read` / `artifact_search` 受限回读；头尾预览保留结尾错误；拒绝跨 owner、穿越及外部链接 | 完整产物最多 32 MiB；更大或保存失败报告不可用，不返回假句柄 |
| `src/my_coding_agent/agent.py`、`permissions.py`、`tools/__init__.py`、`tools/bash.py` | 仅有存储 helper 不能保证产品可回读；只读权限与提前截断可能打断闭环 | 初始化时稳定注册 7 个基础工具及 2 个产物工具；默认 review 允许受限只读回读；启用产物库时保留 canonical 工具原文；可信有界回读不会递归外置 | 普通 ReadTool 的工作区限制保持；独立 bash 默认行为保持 |

产物默认值是本项目实现选择：预览触发阈值 6000 字符，头 3500 / 尾 1500；回读默认 48 KiB、硬上限 96 KiB；搜索最多 20 项、每项 200 字符、扫描最多 4 MiB。扫描 / 输出受限时返回相应状态，不宣称一次搜索覆盖全部产物。

与上下文增强前冻结源码对照，本批产品源码涉及 **10 个修改文件、4 个新增文件**；没有把已有单执行器、输入账本或预算基础重新计为本轮成果。

#### 独立复核发现与更正

- 摘要正例曾使用“Session 仅 q1、压缩输入却是另 20 条 x”的无关 fixture。现已修正有效正例，并保留无关输入必须拒绝的负例，避免测试掩盖来源错误。
- 独立审查复现了直接 `ContextManager` 的遗漏：摘要生成后没有立即建立覆盖身份，改写已覆盖前缀、保持尾部不变时仍复用旧摘要。现已在首次 / 迭代摘要时记录 canonical 覆盖指纹，前缀改写、插入或删除都会失效；测试直接断言新增内容可见。
- 合法尾部缩短、重复 prepare、system 变化、空尾恢复继续保留；100 KB 原文真正进入摘要的回归验证指纹来自 canonical 原文，而非产物预览。
- 144 KB / 8000 行真实 CodingAgent＋假 LLM 往返用例暴露了通用工具序列化提前截断。现仅在启用产物存储的正确链路保留原文，可信有界产物读取结果绕过二次裁剪；Session 原文、resume / 同工作区 fork 后旧引用和模型实际回读均已覆盖。
- 独立只读复核已确认原前缀误复用问题解决；审查范围内未发现其他可证明的重要缺陷。未运行模型 API 或缓存实验。

#### 最终离线验证

主代理实际运行以下集成命令，退出码 0：**279 passed，1 deselected in 11.28s**。此前 273 项批次保留为修复前的验证历史，不与本批相加。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/core/test_context.py tests/core/test_context_boundary.py tests/core/test_context_images.py tests/core/test_runtime_context.py tests/core/test_compaction_snapshot.py tests/core/test_compaction_orphan_tool_prevention.py tests/core/test_session.py tests/core/test_session_append.py tests/core/test_agent_steering.py tests/core/test_agent_loop_pure.py tests/core/test_agent_ownership.py tests/core/test_artifacts.py tests/llm/test_request_projection.py tests/llm/test_openai_provider.py tests/llm/test_anthropic_provider.py tests/coding/test_artifacts_integration.py tests/coding/test_tools_assembly.py tests/coding/test_permissions.py tests/coding/test_read_tool.py tests/coding/test_bash_tool.py -q -o addopts= --import-mode=importlib -k 'not test_bash_pipefail_propagates_error'
```

排除的 `test_bash_pipefail_propagates_error` 已用冻结档案解包源码在同一环境单独复核，并确认实际导入来自解包目录；基线同样因本机回退 cmd、无法识别 `cat`，返回 255 而非预期 42。它属于既有 Shell 环境问题，本轮未扩展修改 Shell 选择。

所有本批产品源码与相关新增 / 修改测试执行 Ruff，结果 **All checks passed**；`git diff --check` 通过。已增加 README 的新工具与行为说明。

参考机制仍来自既有核验：DSH 的完整动态上下文快照与请求投影、Pi / OpenCode 的摘要来源和恢复边界、Hermes / oh-my-pi 的头尾预览与原文引用。本轮实现为本项目 Python 代码，不依赖 DSH 运行时；开发执行助手已切换为 Sol。没有重新宣称上游具备本项目自定的权限、大小限制或默认值。


### E-GIT-CONTEXT：2026-11-12 / 分离上下文交付与未完成的调度工作

- 用户授权：评价长期记忆建议、自主选择实现并及时提交 GitHub。交付记忆改动前，先整理此前已验收但未提交的上下文依赖。
- 改前状态：本地 `codex/scheduler-reliability` 包含调度中间实现、上下文增强及用户文档移动 / 删除；整文件暂存 Agent 会混入未完成调度代码。
- 交付方法：保留本轮改造前源码副本；以 `a04e940` 为基底，只投影完整上下文 / 产物 / 请求诊断增量与相应测试。Agent 使用冻结版到增强版的差量，排除 `_prompt_active`、abort 清理等调度专用片段。其余调度、用户文档重排、取消的实验脚本 / 数据 / 源码归档未进入提交。
- 实际范围：24 个明确路径，含 14 个产品源码文件、8 个测试文件、README 产品说明和本实验记录。没有使用 `git add .`；临时 index 与独立交付目录不改变当前 index 或工作区。
- 验证：确认 Agent、ContextManager、CodingAgent 的实际导入路径来自独立交付树；上轮集成批次排除调度专属 `test_agent_ownership.py` 后 **269 passed, 1 deselected in 10.57s**。唯一 deselect 为已在冻结基线复现的本机 cmd / Git Bash pipefail 环境差异。主代理在该树执行 `python -m ruff check src tests` 通过；独立 index 的 `git diff --cached --check` 通过。
- GitHub：提交 [`7531dadd64aa3f4e5a88e0a382eaafc8b505e4f0`](https://github.com/294962silent/my-agent-harness/commit/7531dadd64aa3f4e5a88e0a382eaafc8b505e4f0)，推送到自己的 `origin/codex/context-memory-reliability`。未推 upstream、未合并、未覆盖当前脏工作区。功能分支 push 本身不触发当前 CI，尚不能宣称 CI 通过。
- 与前一版本的区别：E-CODE-SOL 的“未提交”是历史时点；本节把已完成源码单独交付，不把该提交当作调度账本、预算、跨会话后台服务或缓存性能实验的完成证据。


### E-MEMORY-01：2026-11-12 / 长期记忆脱敏、回读删除与空闲抽取

- 用户要求：评价学习文档 04A 第 11 节的六项建议，由主代理决定修改范围，完成后提交 GitHub；缓存性能实验继续取消。
- 取舍：六项采用小接口实现。优先修补存储与检索，再接可选抽取；不引入五后端、provider 插件、远程准入、lease/heartbeat 或持久跨会话 worker。设计见 [执行计划](../handoffs/2026-11-12-memory-reliability-plan.md)，使用方式见 [长期记忆说明](../memory.md)。
- 分工：Sol / medium 两名执行助手分别负责 MemoryStore 与 Agent/Context 生命周期，第三名助手独立复核；主代理保留设计、集成、交付树验证与提交责任。未恢复 DS/DSH。

#### 修改前后与涉及文件

1. **脱敏**：新增 `src/my_agent_core/memory_safety.py`，由 `memory.py`、`memory_index.py` 和抽取模块复用。原来写入、回执、检索与抽取请求无统一处理；现在落盘前、抽取外发前、读取和回执出口处理常见 provider token、Bearer、私钥块与命名凭据。结构化敏感 key 的短 value 也受保护，history 同样处理。收紧规则以保留 `max_tokens`、`token_budget`、`context_token_budget`、`secret_scanning_enabled` 等普通配置，避免仅凭子串误删语义。
2. **全文入口**：`memory.py` 增加 `read_full(key, subject)` 与独立只读 `memory_read` 工具，Agent 注册，CodingAgent 权限将其归为只读。原来只能看到预算内检索行；现在可按精确身份获得完整 JSON（含 value、有效时间、source 与 history）。超长首条有预览和截断提示，不再直接消失；需要修改前可先核对全文。参数不是任意文件路径。
3. **删除与写入可靠性**：扩展 `memory` 的 `delete` action，移除精确事实和替换历史，立即更新索引，持久删除标记阻止旧 JSONL 迁移复活，允许后续显式重新 remember。中文或规范化碰撞使用稳定 hash，兼容旧 untitled 文件。相同值 NOOP，不同值保留 supersede 历史；写盘失败上报并回滚内存，自由条目变更也刷新索引。冻结 system 快照不随后台写入改写。
4. **检索门控**：`agent.py` / `memory_distillation.py` 完整匹配 hi、hello、thanks、ok、继续、done??? 等琐碎输入；实际需求如“继续修改端口 8080”继续检索。多模态内容保留。
5. **空闲蒸馏**：新增 `memory_distillation.py`，替代 run 末尾同步模型调用；run 与 stream 的正常完成都只安排任务，不等待抽取才返回。`memory_autodistill=False` 默认保持；显式启用时 `memory_idle_seconds=30`、`memory_distill_timeout=15`，均为秒、Python 参数。任务有句柄、超时、异常回收；新输入 / abort / reset / close 使旧任务取消，落盘前核验 Session 对象、ID、路径、当前节点与 generation。`wait_memory_idle()` 允许宿主等待；Agent.aclose、CodingAgent.aclose / async with / close_mcp 及同步线程中止路径接通清理。
6. **压缩前接缝**：`context.py` 的自动 / 手动共同入口接受可选回调，传入 canonical 待覆盖原消息副本与覆盖身份。启用自动蒸馏时接入同一事实抽取器；失败或超时为 best-effort，允许继续摘要；CancelledError 传播。不会把请求投影 / 产物预览当原文。它不同于 Hermes 的可选 fail-closed 检查点，不保证压缩前必定保存所有事实。

抽取只使用带宿主来源标记的真实原始 user 文字（检索注入之前），要求每条候选有原文 evidence；不把 assistant 自述、工具结果、运行态、摘要或旧未标注历史自动提升为事实。只选择有持续价值的明确偏好 / 约束，使用有界首尾采样（最多 24 条、每条最多 1200 字符、总预算 12000 字符，条数较多时平分预算）；这不是全历史无损抽取。成功去重有界；失败、非法结果或非空但无有效 evidence 的结果不推进成功游标，合法空数组可记录本次扫描成功。

#### 验证与更正

- 先验证缺口：存储 6 项新增回归均失败；生命周期 / 压缩接口首轮 5 项因新参数 / 钩子缺失而失败；再实现对应行为。旧同步蒸馏断言改为“响应先返回，再显式 drain，严格检查事实与 evidence”，并在 finally 关闭任务。
- 开发中更正：脱敏最初会误伤普通 token 配置，已补负例并收紧；首次抽取对非空但无有效 evidence 的响应仍记成功，已修正并加 3 项正 / 负例。无效响应不会妨碍后续重试。
- 存储分批验收 64 项通过；独立复核 memory 相关 70 项通过。实现者 17 文件集成批次 265 项通过；最终独立交付树同批 **265 passed in 5.38s**。这些批次重叠，不累加为独立测试总数。
- 主代理最终验收目录为独立交付树，实际确认 Agent、MemoryDistiller、CodingAgent 导入来自该树。该树移除了调度专属 wrapper，因此把记忆 start stop / closed 检查移到基线 prompt_stream，保留所有正常完成、abort、reset、close 与 precompress 入口；未带入调度半成品。
- 主代理实际命令：`python -m pytest tests/core tests/coding tests/llm -q -o addopts= --import-mode=importlib -k 'not test_bash_pipefail_propagates_error'`，**1029 passed, 1 deselected in 52.18s**。排除项仍为 E-CODE-SOL 已在冻结基线复现的本机 cmd / Git Bash 差异，不是本轮新回归。
- `python -m ruff check src tests` 通过；`npm ci`、`npm run build` 通过；`npm test` 为 **96 passed、2 skipped、0 failed**。`uv build --wheel --out-dir dist` 成功，实际检查 wheel 的 175 项内容含 TUI 入口及新 memory_distillation / memory_safety 模块。
- 全程没有真实模型、KV cache、LongMemEval、SWE-bench 或 GPU 测试；本地实际 Agent/Context/MemoryStore 装配经假 LLM 验证，不等同生产模型质量和费用验证。未同步本批源码到 L20。

#### 参考、收益与限制

- oh-my-pi `fec500b7a891`：借鉴写入脱敏、空闲处理与全文回读；Hermes `5df966a97a2d`：借鉴琐碎输入闸门与 pre-compress 接缝。依据是用户提供的固定版本关键文件核验资料，保留“没有逐行全库通读”的边界；不从文件树筛选推断其他项目绝无相关能力。
- Mem0 仅借鉴论文层面的 DELETE 操作概念，未核验其源码，不宣称移植其实现。30 秒空闲 / 15 秒超时是本项目选择，不是照抄 oh-my-pi 的 12 小时 / 30 天 / 64 会话启动管线。
- 收益：错误事实可删除、长事实可核对、写盘失败可见；减少琐碎输入检索与响应尾部同步抽取等待；使压缩前保留事实有明确接缝。代价：显式启用抽取增加模型调用，压缩前仍可能增加等待；新增来源元数据、任务清理和兼容路径；正则存在误报 / 漏报，不给提升百分比。
- 历史原文件不批量重写；读出脱敏不代表原盘 / JSONL 备份已清洗或安全擦除。没有跨进程写锁或多实例自动同步；memory_dir 决定记忆作用域，不自动隔离项目。旧 legacy 迁移中途失败后可能部分可见的既有问题未在本批扩展修复。
- 进程退出可能丢弃尚未执行的 idle 任务；旧未标注 user 历史不会自动抽取；有界采样可能漏掉中段事实。best-effort 抽取失败仍可压缩，不宣称 exactly-once、持久恢复、全事实保真或强制抢占。
- 交付：记忆代码、对应回归、说明与本节构成后续独立提交，推送自己的 `origin/codex/context-memory-reliability`；此前上下文提交为 `7531dadd`。原本未完成调度与用户文档移动 / 删除保留在本地，不纳入此次提交。GitHub PR / CI 状态以实际创建和检查结果为准，不在提交前预填成功。


### E-GIT-MEMORY：2026-11-12 / 记忆提交与 GitHub PR

- 记忆代码已提交并推送：[`c7cd65e60974a305320ccae79007e417f69b70e6`](https://github.com/294962silent/my-agent-harness/commit/c7cd65e60974a305320ccae79007e417f69b70e6)，父提交为上下文增强 `7531dadd`。只更新交付分支 `codex/context-memory-reliability`，当前本地工作分支与未暂存内容保持原状。
- PR：[#1 完善长期记忆安全与生命周期，补齐上下文来源与产物回读](https://github.com/294962silent/my-agent-harness/pull/1)，目标为用户自己仓库的 main；未自动合并、未推 upstream。
- 创建过程：GitHub 连接器返回无创建 PR 权限，本机 gh 登录也未通过验证；使用已经能够推送该仓库的本机 Git 授权身份完成创建。没有打印或保存凭据，也没有更改全局认证配置。
- 创建后实际观察：CI [run 43135488211](https://github.com/294962silent/my-agent-harness/actions/runs/43135488211) 已启动；Ubuntu / Python 3.11、3.13 完成成功，Windows 两组仍在运行。此处为过程快照，最终结果继续追加。

- 最终复核：上述源码提交的 CI run `43135488211` 四个 job 均为 **completed / success**：Ubuntu-latest / Python 3.11、3.13，Windows-latest / Python 3.11、3.13。每组均完成依赖同步、Ruff、TUI 构建、完整 pytest、wheel 构建 / 入口检查及 TUI 测试。
- 本批最后的实验记录更新只补交付与 CI 证据，不改变已通过检查的源码。后续检查状态以 PR 实际结果为准。


### E-REDESIGN-CODE：2026-11-12 / 面向 Agent 实习的运行与观测重设计

- 用户目标：重新设计整个框架，面向 Agent / 大模型应用开发实习，持续整理逐模块消融、实际数值、技术取舍和开发问题；随后指定只使用两名执行助手。主代理负责设计、集成和记录，执行助手为 GPT-6.1 Sol / medium，当前无 DS / DSH。
- 工作区：从已交付的 `1f150e1` 创建 `.worktrees/interview-redesign` / `codex/interview-harness-redesign`，保留原 checkout 的调度中间实现和用户文档修改 / 删除。不是把原 checkout 的整个 Agent / RPC 文件复制过来。
- 架构：详见 `docs/architecture/HARNESS_VNEXT.md`。统一 Runtime、Model、Tools、Session、Context/Artifact、Memory、Permission、Observation/Evaluation 的职责；选择渐进重设计，保留已验证模块，新增运行归属与统一观测接缝。不因为有新架构就宣称所有模块重写。
- Runtime 改动：`agent.py/loop.py/events.py`、新 `run_lifecycle.py`；可选 RunLimits 默认无限制，同一 Agent 直接运行只有一个 owner，明确 run_id、signal、budget 和清理责任。轮次在主请求重试外计数，deadline 限制异步等待，abort / 外部取消 / budget_exhausted 有不同终态；关闭和异常后释放归属。
- 开发中回归：provider 已交付部分 tool_calls 但尚未 StreamDone 时，立即取消使 Session 中工具配对缺失。集中终止收尾保存已观察工具意图并补恰一条中断结果；新增 abort / deadline / consumer close 后 Session.load 回归。不保存纯 partial 文本，不通过放宽 deadline 隐藏问题。
- 观测改动：LLM门面五个入口及新 `observation.py/observation_export.py/langfuse_export.py`；不可变 start/end、root/parent/purpose、provider归一化usage快照，默认无正文 / 参数 / 异常消息。流在首次迭代开始，结束 / 错误 / 取消 / close 各一次终态，ContextVar不跨消费者yield泄漏。
- 导出：本地 recorder / JSONL 与有界 worker，丢弃、传输失败、sink清理失败和线程存活分开；观测异常不影响模型结果。独立复核发现 flush异常跳过own_client.shutdown，已用try/finally及失败回归修复。Langfuse适配对照官方Python SDK5.2.0 `00e5098bd6511a2f7bc7b6813ac125a3af559697` 的 start_observation/update/end，仅fake client验证，未宣称真实上传接通。
- 打包问题：独立Linux环境暴露PIL依赖未显式声明，开发环境的matplotlib间接依赖曾掩盖。新增 `Pillow>=10` 并同步uv.lock；61个既有包版本完全未变，锁中pillow仍12.3.0，只有direct依赖与少量revision/marker元数据变化。Linux另装Pillow13.0.0后实际import Agent/Session/CodingAgent成功，`uv lock --check`通过。
- 主代理验证：`python -m pytest tests -q -o addopts= --import-mode=importlib -k 'not test_bash_pipefail_propagates_error'`，**1105 passed、1 skipped、1 deselected in55.93s**；skip为主Windowsvenv未安装Harbor的可选测试，deselect仍是既有cmd/cat pipefail环境差异。执行助手独立分批core+llm665项、coding399项通过，重叠数量不累加。
- 静态与构建：`ruff check src tests experiments/harness_eval experiments/benchmark_live`通过；`npm test` **96 passed、2 skipped、0 failed**；TUI构建与wheel构建通过，实际检查wheel含RunLifecycle、观测模块、TUI入口及 `Requires-Dist: pillow>=10`。
- 代价与限制：协作取消不能强停同步Python/同步SDK，外部副作用不能回滚；exporter transport永久阻塞可能close超时；observer不是硬费用预算。真实API账本、官方任务预检与模型评分作为单独实验追加，不把软件回归数量当模型成功率。
- 原始基线：本轮改前干净core/llm **630 passed in21.01s**。本节完成本地实现与验证，GitHub提交 / CI 状态以后续实际记录为准，不预填。


### E-REDESIGN-EVAL：2026-11-12 / 八模块统一离线证据与面试材料

- 新增 `experiments/harness_eval/`：统一场景、原始记录 schema、唯一运行目录、manifest、从 raw 汇总与可读报告；记录 git SHA、dirty 状态、源码 hash（运行前后核对）、实际导入路径、seed、参数、环境、许可证和失败。
- 主代理实际运行：`PYTHONPATH=src;. python -m experiments.harness_eval --suite all --repeats 5 --output docs/experiments/results/redesign --seed 23`，保存 `docs/experiments/results/redesign/20261112T095857-2381042b458f/`。覆盖 **8 模块、12 独立场景、100 条记录、0 assertion failures**。manifest source_sha256 `19d94923b9843545da609de18225815c71e2490e1f3360db98b3fe359d8cdfd7`，运行前后相同，git基底1f150e1 + 当时未提交改造；不把它称作干净旧基线。
- 定量观测：独立工具并发峰值1→3且均完成3个工具；故意全并行对照的写读违例1→0；线性历史投影对照的跨分支泄漏1→0；2条标注记忆查询0/2→2/2、失效事实命中0；权限夹具未授权写副作用1→0；轮次预算把脚本模型调用5次截停至2次；辅助观测场景记录主调用与摘要2/2、隐私违例0。
- Context/Artifact：原文120019B，off视图120019B、on视图7041B，on额外产物写盘120361B；**两组 evidence_recoverable均1**。摘要来源contract中 unchanged复用1、覆盖前缀改变后旧摘要泄漏0。字节不等于token，磁盘与回读成本不能省略。
- 消融分类：真实开关/元数据配置称ablation，无生产开关而使用有限对照wrapper的Tools依赖与Session线性投影称comparison；流取消/关闭、single owner、摘要覆盖身份称contract。off如预期暴露缺陷也可满足其夹具断言，0 assertion failures不意味着off更安全。
- 局限：五次重复不是五个独立问题，p95/median只描述本机小样本；2条记忆查询是smoke，不是长期记忆质量评测。所有离线tokens/cache/quality为null，不给真实模型质量/吞吐/KV提升比例。默认不触碰凭据或真实模型。
- 文档：`docs/interview/` 为八模块分别整理问题、源码、方案取舍、原始指标、开发问题、优势代价、复现与追问；新增 DEVELOPMENT_PROBLEMS / CLAIMS 对齐真实证据。README改为项目定位与可核验入口，历史README旧性能/榜单数字保留Git固定链接，不继续作为新框架效果宣传。
- 用户新授权：真实实验使用 **DeepSeek V4.1 Flash总预算$30**，优先SWE/Terminal官方开源任务，替代旧3美元/自造任务提案；预算文件保留授权变化。未恢复DSH对照或L20 GPU实验。
- 官方预检：WSL Docker26.1.3、Compose2.27.1、Harbor0.25.0、SWE-bench5.0.2与Python3.12已准备。Windows本地代理无法直接用于WSL，网络转发helper启动被自动策略拒绝后未重试；改为标准下载官方crane v0.22.1并核SHA、拉取原官方镜像为tar再docker load，核image config。任务Git导出显式关闭autocrlf并核blob字节，没有修改评分脚本。
- 开发中问题：CLI exit0但trial有exception不能算通过；缺Compose、CRLF shebang、UV依赖下载失败和gold测试超时分别保留。SWE requests1963的失败未当作模型失败；官方入门sympy20590作为模型结果出现前的环境补充候选已freeze，并真实gold **completed1/resolved1/errors0，20.40s**。
- 后续真实模型配对：共用持久预算账本，oracle/gold通过且任务/镜像/报告hash匹配才能启动；隐藏测试/solution不进入prompt。真实结果另节追加，当前离线数字不能代替它。


### E-REDESIGN-LIVE：2026-11-12 / 官方 SWE 单例真实试验、预算与协议更正

- 用户授权是总预算30美元，实际模型使用已有OpenCode Go兼容端点的 `deepseek-v4.1-flash`，不是新接官方api.deepseek.com；未索要或打印密钥。Key只经host进程环境传入，未写CLI、容器环境、任务文件或Git记录。
- 预检：原官方SWE-bench5.0.2、固定taskrepo `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，sympy20590官方image config `fcfe2aec281d3a1af6a71bedef4637a446bcb076e0acdec6fec03b9b25a346f2`，任务base `cffd4e0f86fefd4802349a9f9b19ed70934ea354`；7文件逐字节Git blob核对，官方gold completed1/resolved1/errors0，20.40s。requests1963镜像错误和120秒gold超时均保留，未计作模型失败。
- 实际模型设计：**1个独立任务、2个model trial**，顺序on→off，seed23只控制活动顺序，不是provider采样seed；温度0.7、16请求/4096输出/600秒、共享30美元原子预留账本。两个run目录的r2是启动命名，不是第二个模型重复。r1因镜像HEAD不等任务base而被挡住，requests0，修复容器内先reset再核clean后才启动。
- 原始证据：`docs/experiments/results/live-swe-20261112/`含ledger、两个原始run.json、模型预测JSONL、官方aggregate / instance report、测试stdout、金标通过证据的hash及BSD-3-Clause许可；不含gold/test patch正文。`summary.json`从请求记录计算，manifest明确事后采集源码快照和协议版本。
- 官方非gold评分：on整体21.85s（测试4.23s），off整体21.49s（测试3.75s），各completed1/resolved1/errors0；各FAIL_TO_PASS1成功、PASS_TO_PASS21成功、0失败。补丁没有被人工读取修改来纠正答案，评分反馈没有交给另一solver。两份aggregate统计相同故SHA可相同，独立runid、日志目录和stdout分别保留。
- 真实运行终态：两组都是 `budget_exhausted` / 各16请求，补丁已产生并经官方判分；没有将截停美化成正常end_turn。32请求usage全部成功结算，cache_read字段各仅15/16覆盖，完整命中率保留null。
- 计量：on输入80706、输出4533 token；off输入110165、输出4902 token（输入由provider总量减输出计算）。账本按输入$0.30/M、输出$1.20/M并把cached输入也按全冷价计，on$0.029658、off$0.038939，合计 **$0.068597**。这是客户端保守计价，不是已核对的服务端账单或实际订阅余额。
- 关键更正：本题on没有生成artifact，外置未激活，所以这只是production Agent/Runtime/预算/官方评分的真实pipeline smoke，不能把80706 vs110165 token差异归因于外置，更不能宣称成功率提升。两组trajectory随机、cache状态未控制、小样本也不足以估计因果收益。
- 协议v1复核还发现仅注册shell但开启store，缺artifact_read/search。保留v1源快照及既有真实成绩并明确标v1/smoke；修协议v2后，两arm注册相同生产受限工具schema，off只关闭投影。SWE/Harbor的14KB输出真实handle→artifact_read中部/尾部marker回读、off全文保留和schema相等已经fake provider验证，原始4份contract存 `protocol-v2-offline/`，标零真实请求、provider_usage_synthetic；Linux独立17项通过。**v2没有重新调用真实模型，不能挪用v1成绩。**
- Terminal-Bench2.0固定repo `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`，先于模型结果选6个CPU候选；fix-git原oracle因UV0.9.5官方依赖下载HTTP2失败而reward0，保留原JSON/stdout，未给TB发模型请求。见 `benchmark-preflight/terminal-bench.json`，不替换评分标准或隐藏环境筛选。
- 最终本地代码验证（含v2和SWE入口）：主代理全tests **1112 passed、2 skipped、1 deselected in57.54s**；两个skip是Windows缺可选Harbor，deselect为既有cmd/cat pipefail差异。全源码 / 测试 / 新实验Ruff与diff check通过，TUI96pass/2skip、wheel新增模块/TUI/Pillow依赖检查通过。没有GPU、没有DSH、没有榜单上传。
- 收益与边界：获得可复现的真实任务链路和费用账本，发现并修复假环境测试遗漏的镜像初始化与回读接线问题；不宣称SWE/TB全量排名、Artifact因果提升、真实Langfuse上传或Mem0接通。源码重设计9509262已推送，后续实验 / 文档提交与PR状态以实际Git记录为准。

- 最终暂存检查补充：官方原始stdout包含终端回车和尾随空格，首次staged diff check因此失败；新增 `.gitattributes` 对实验结果目录禁用autocrlf文本转换，仅对两份SWE测试stdout和Terminal原始stdout关闭whitespace检查。保留原始字节，不删除报错行；重新核对index blob与本地证据及staged diff，结果另以实际命令记录。其他源码与文档检查保持开启。
- 暂存复核结果：对38份实验证据执行 `git show :<path>` 与本地原文件字节比较，全部一致；`git diff --cached --check`通过。JSON解析24份、16份入口文档相对链接检查无缺失；已用现有实际模型凭据进行精确匹配扫描，待提交文件中无匹配，不打印或保存凭据。两名助手额外复核104项源码hash、12项WSL原始证据与v1/v2边界，均一致。


### E-GIT-REDESIGN：2026-11-12 / 框架重设计与实验材料交付

- 已推送自己仓库的 `origin/codex/interview-harness-redesign`：运行 / 观测源码 [`9509262`](https://github.com/294962silent/my-agent-harness/commit/9509262)，实验框架 / 真实任务入口 / 原始证据 / 面试材料 [`e2181f6`](https://github.com/294962silent/my-agent-harness/commit/e2181f6)。原 checkout 的调度中间实现与用户文档修改保留，没有推 upstream。
- PR [#2 Unify Agent runtime, model observation and reproducible evaluation](https://github.com/294962silent/my-agent-harness/pull/2)，base 为尚未合并的 #1 分支 `codex/context-memory-reliability`，只展示本批增量。两份 PR 均未自动合并。
- GitHub 连接器创建 PR 返回403、无集成写权限；使用已能推送自己仓库的本机 Git 授权身份创建。凭据只在进程内存中使用，不输出、不落盘、不改全局认证。
- `e2181f6` 的 [CI run 43208908713](https://github.com/294962silent/my-agent-harness/actions/runs/43208908713) 过程快照：Ubuntu / Python3.11与3.13均 completed/success，完成依赖、Ruff、TUI构建、pytest、wheel与TUI测试；Windows3.11/3.13当时仍queued，不能预填四组通过。后续文档补交会产生新head，最终状态以[PR checks](https://github.com/294962silent/my-agent-harness/pull/2/checks)为准。
- 收尾清理：确认没有运行中的Docker CLI或容器后，只删除本轮三份已经导入的官方镜像tar下载副本（逐个核绝对父目录与文件大小），共2261497856字节；Docker镜像缓存和原始实验材料保留。未同步L20，未运行GPU，未追加模型请求。
- 本批已完成架构设计、关键接缝实现、八模块离线证据和SWE单例真实链路验证。尚未完成v2真实模型因果实验、Terminal-Bench模型评测、真实Langfuse上传、Mem0或Dream-RSI集成；这些不构成本次已交付效果。


### E-SWE-V2-CODE：2026-11-12 / 扩展官方任务入口与小样本执行协议

- 用户在核对第三方集成边界后，明确要求继续扩展SWE入口、完成v2真实小样本；沿用已授权30美元总预算和两名Sol/medium执行助手。设计与执行清单见 `docs/handoffs/2026-11-12-swe-v2-pilot-plan.md`，不是恢复DSH或完整300题实验。
- 改前为 `db77a71`：SWE solver只接受写死的两题，没有通用配对调度；预算只有总额政策。现在新增 `swe_catalog.py` 的冻结catalog/TaskSpec，`swe_agent.py`贯穿安全metadata/base/image/config/source/hash绑定；CLI须成对提供catalog与批准的SHA，旧两题接口保留。可信宿主审批hash只检测变更，不伪称数字签名或恶意宿主防护。
- 新增 `swe_campaign.py`：plan/run/score/summarize，固定输入与源码hash、seed23平衡先后、逐trial唯一输出与started/run_id、跨进程排他、完成项hash校验后跳过、未完成项不自动重试。独立官方评分不携带模型key；blocked、执行尝试、真实model attempts、scored与resolved分开，unknown usage停止后续消费。
- `budget.py`新增可选绝对累计ceiling，在原FileLock预留锁内检查min(原30美元政策,本批ceiling)，不改变旧账本政策和provider归一化计量。历史消费0.068597美元，本批新增客户端准入3美元，上限字段为3.068597；供应商实际账单仍需另核。代码默认保持旧行为。
- 交叉复核发现取消官方评分未回收进程的问题，已修同组terminate/kill/wait并限定本instance/run_id清理评分容器；新增取消回归。另补同campaign多进程排他和ledger正常增长不误判输入漂移，0request预代理失败单列，避免冒充模型任务失败。
- 验证：SOL-1先取得catalog缺模块的22项红回归，再完成第三题数据驱动/gold绑定/hash篡改拒绝；SOL-2补ceiling和campaign缺口回归。作者Linux窄回归A43项、B19项通过（有重叠不累加）；另一名助手分别按db77a71定点diff复核，无剩余阻断。主代理明确 `PYTHONPATH=src;.` 后实际运行 `python -m pytest tests/experiments -q -o addopts= --import-mode=importlib`，**64 passed、2 skipped in10.31s**（可选Harbor未安装）；Ruff与diff check通过。本批生产core/llm/TUI未改，不重报旧构建为新验证。
- 数据准备：官方taskrepo固定 `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，六题选择在模型结果前保存 `results/swe-v2-pilot-20261112/SELECTION.md`。通过GitHub连接器取52文件并两侧逐字节核Gitblob/SHA256；Windows原官方crane拉原镜像再Linux加载核config，不重建镜像或改测试。Go工具不读取Windows系统代理导致初次registry超时，显式在子进程内采用系统代理后恢复，未打印代理或凭据。
- gold预检：20590复用之前原成功报告并重绑安全metadata；11400与pytest5103/5221/5227本轮原评分器通过，11870固定300秒窗口超时（318.39秒含清理、completed0/resolved0/error1），保留环境blocked，不替换、不收费。最终选定6、eligible5，计划最多10个真实model trial。此时尚未发本批模型请求，实际结果另节追加。
- 价值与代价：解耦任务批准与solver、使消费与重入有证据且可阻断，避免环境问题混入模型成绩；增加catalog准备、冻结、状态和原始数据维护。评分仍依赖原官方Docker环境，小样本/两个项目/已知回归题不具有全量代表性；不声称Mem0、Dream-RSI或真实Langfuse已接入，也不预填Artifact或缓存收益。
- 代码已提交并推送 `3e2d345`。正式模型前追加 `run --max-new-trials N`，仅限制本次新增eligible执行；完成/blocked不计数、不改冻结顺序或预算，便于先验首配对后resume。两个新增回归先红后绿，主代理实际campaign定点 **12 passed in6.03s**，另一助手只读复核无阻断。正式SDK的免费模型目录读取成功，确认请求模型仍可用；Config实际温度默认0.7已核对。普通urllib默认UA读取目录曾被403/1010拒绝，正式SDK正常，不把这种网络目录失败算成模型trial或模型费用。


### E-SWE-V2-RUN：2026-11-12 / 固定六题真实小样本执行与更正

- 模型生成使用已提交源码 `765216830b1972ba3b3c0d20171315343c63d288`；该提交GitHub CI run `43218293170` 四组Ubuntu/Windows × Python3.11/3.13均completed/success。运行前核真实core/llm导入来自目标worktree/src，温度默认0.7；原API profile通过正式SDK模型目录验证，密钥只经host环境转发。
- 首个真实请求前冻结campaign `9684ed3b72784f52b939ac77d5ab46bf`，plan SHA256 `b7a3ddfbd871743026fa76601805f24a138b1beac93a2e48882d5fcb22c1398a`，catalog SHA256 `fe6f15e9887c5e07243c54e91b6f5e93cff56f8c3841d387cb312c2db43f126c`。184项输入/源码指纹经独立复核一致；选定6、eligible5、blocked1，eligible首发臂按seed23平衡为3 on/2 off，blocked单独分配不计模型先后平衡。
- 先用max-new-trials=2完成pytest5103首配对：两组各16请求，终态budget_exhausted，所有请求usage已结算。保守账本on$0.050700、off$0.070654，新增合计$0.121354；on实际生成1个artifact，read/search响应0，off无artifact。两组模型patch长度均0，不能当作交付修改。
- 首次自动summary将两份官方empty_patch报告误标为environment_or_execution_error，因此主代理先停后续收费排查。直接核原aggregate：两组empty_patch_instances=1、completed_instances=0、resolved_instances=0、error_instances=0、infra_failure_instances=0。这是预算内没有修改的已知未解决结果，测试没有执行；不是评分环境故障。没有重跑模型或评分、改补丁、改题、放宽预算。
- 更正后按原冻结顺序恢复其余8个eligible trial；冻结源码在整批模型生成结束前不动，避免运行中引入新策略或输入漂移。原错误分类/state/report保留，公共收集层追加带来源的派生分类；源码统计修复与回归在本批生成结束后单独记录。整个任务的最终费用、成绩和回读以本节后续原始证据为准，不从首对差值宣称因果收益。
- 最终真实结果：5个eligible任务/10个trial全部生成与独立原官方评分完成；160请求全结算、0在途/unknown，均budget_exhausted。on解决2/5、off1/5；总3resolved、7empty_patch，无模型评分error/infra/ambiguous。真正执行测试3次、已知结果10次、完整known pair5、双臂都执行测试的pair1。11870仍是gold超时blocked，不进模型分母，没有追加题目或重试。
- 费用与用量：本批全冷价微美元逐请求向上取整合计$0.503462，on$0.252843、off$0.250619，开启总费用没有下降；历史$0.068597另计，全局$0.572059。on完整输入583228/输出64864，off输入617804/输出54369，总token1320265；归一化prompt字段140552不等全部输入。cache_read仅158/160覆盖、cache_write0/160，完整总量与命中率保持null，不用缺失补零制造排名。
- 激活情况：5个on trial累计11产物，off0；read/search响应均0。真实投影链路已激活，但没有真实模型回读使用证据；不将离线往返或2/5对1/5的小样本差异宣传为稳定质量/费用/KV收益。7次空补丁是后续预算/交付策略要研究的问题，原因尚未通过新实验归因。
- 归档：`results/swe-v2-pilot-20261112/raw/`为81个manifest列入文件+manifest自身，原manifest SHA `7321985b32f571baa37d96361b45ab51c1a91ea5343b1dd381b6e6219f69e282`。收集前184项输入及92项原结果hash全部匹配，复制后81项再核；保留原state/report与原错误category，empty更正单独派生，不复制gold/testpatch/hints/session或artifact正文。六份固定base许可证、14份原gold/预检证据另存同目录，准备阶段源码快照不冒充765生成版本。
- 清理：所有模型/评分进程已结束，实际Docker ps无运行容器；逐个核已导入镜像下载副本的绝对父目录和字节数后删除五份本批tar，共5205992960字节，Docker原镜像缓存与原始记录保留。无L20/GPU/DSH，未继续使用预算扩题。


### E-SWE-ANALYSIS：2026-11-12 / 空补丁分类修复与可移植离线重算

- 整批模型/评分及原始归档完成后，新增 `swe_results.py` 单一 `parse_official_outcome`；_score与summary共用。对原始report bytes核hash后重派生，旧state只deepcopy审计、不写回；新增known_official_outcomes/test_executed_trials/complete_known_pairs，旧complete_pairs保留双臂实际执行测试的语义。
- 官方SWE-bench5.0.2语义核验：total_instances可以是整个dataset规模，submitted需匹配单题；completed与error可能重叠，infra/ambiguous是附加标签。parser按真实语义校验ID/计数/集合，不用模拟schema误拒合法官方结果。empty明确False/False，completed unresolved为False/True，错误/矛盾/缺失保留unknown，错题不能冒充本题结果。
- 新增纯stdlib `archive_outcomes`/CLI：只用归档相对路径、先验证manifest路径边界和hash，再从原报告重算；无需原宿主绝对路径/模型/Docker或生成源码仍与当前一致。输出分析parser版本 `swebench-5.0.2-outcome-v1`及其SHA，生成版本仍765；run/score的source drift保护没有放宽。当前81文件独立复算得到相同3resolved/7empty/5known pairs，archive_modified=false。
- 数据分析脚本 `analysis/recompute_metrics.py`从plan.run_ids划分本批/历史，只解析plan/ledger，逐字段覆盖完整才给total。主代理重新执行后与 `analysis/metrics.json` 逐对象一致；同大小文件篡改、路径逃逸拒绝，换工作目录复算相同。此脚本与许可证/预检文件不改raw manifest。
- 验证：原empty误分类回归先失败，再实现；作者Windows/Linux同版窄26项通过，另一助手三文件只读复核无阻断。主代理最终 `pytest tests/experiments -q -o addopts= --import-mode=importlib` 为 **80 passed、2 skipped、5 subtests passed in11.57s**（skip为缺可选Harbor），Ruff通过；CLI和精确费用重算已实际执行。批次数量不累加为独立测试总数。
- 文档：更新README、面试CLAIMS/上下文/成本模块及开发问题P13–P17；完整报告见 `results/swe-v2-pilot-20261112/REPORT.md`。本批借用官方SWE任务/镜像/评分协议，调度、归档与分类为本项目实现，不将其他Harness分数混作对照；Mem0、Dream-RSI和真实Langfuse服务仍未接入。后续Git提交与CI状态以实际记录为准。


### E-SWE-CI：2026-11-12 / 跨平台deadline测试稳定性修复

- `3e2d345`交付catalog/campaign，`7652168`交付分段执行并作为本次实际生成版本；统计修复、121文件证据与面试材料已提交推送 `2857b9c`。PR #2标题/说明已按最终范围更新，未合并。
- `2857b9c`的CI [run43227265253](https://github.com/294962silent/my-agent-harness/actions/runs/43227265253)暴露Ubuntu/Python3.11的 `test_deadline_interrupts_each_awaited_runtime_stage[prepare]`：总0.08秒deadline在前置阶段已过，started=False。先读取原失败日志定位，未只通过重跑掩盖失败。
- 仅改 `tests/core/test_run_lifecycle.py`：120ms前置hook确定性复现旧断言；注入已有RunBudget的虚拟clock及模块局部wait代理，在选定await开始后触发原0.08deadline，保持真实取消/gather/finally、预算终态与owner释放断言。独立复核后补5秒外层测试watchdog+finally a.aclose，不改变产品预算或全局asyncio。
- 主代理实际命令 `pytest tests/core/test_run_lifecycle.py -q -o addopts= --import-mode=importlib`，**15 passed in1.08s**；作者Linux/Python3.12同版15 passed in1.43s，Ruff/diffcheck与另一助手只读review通过。本地不宣称运行过3.11；后续新head矩阵以实际CI记录为准。
- 这一修复只改测试和记录，生成源码765、已归档81文件、原评分、消费$0.503462均保持。没有新模型请求、评分重跑或上游推送。


### E-SWE-DELIVERY：2026-11-12 / v2小样本最终交付

- 运行入口与分段执行已推送 `3e2d345` / `7652168`，统计修复和121文件证据提交 `2857b9c`，CI测试稳定性修复提交 `47a89aa8538b2d5d061555e0a5c4048bc0b67ce7`，均在自己的 `origin/codex/interview-harness-redesign`。原checkout修改保留，无upstream推送或自动合并。
- PR [#2 Unify Agent runtime, observation and reproducible SWE campaigns](https://github.com/294962silent/my-agent-harness/pull/2) 已更新标题、最终实现、真实结果与限制；仍以#1的 `codex/context-memory-reliability` 为base。实际模型生成版本为765，后续统计和测试修复均未重新调用模型。
- 测试修复后的[CI run43228948231](https://github.com/294962silent/my-agent-harness/actions/runs/43228948231)四组 **completed/success**：Ubuntu/Python3.11、3.13，Windows/Python3.11、3.13。此前失败平台已实际重验成功，不将本地3.12/3.13替代3.11证据。后续本节仅文档补交，最新head状态以[PR checks](https://github.com/294962silent/my-agent-harness/pull/2/checks)为准。
- 提交前复核：121个明确路径通过实际现有API凭据精确匹配扫描（无匹配）、86个JSON解析、本批新增本地链接检查无缺失；两条交付前已存在的历史档案链接保留。108份本批证据的index blob与原文件逐字节相同，staged diff check通过；没有 `git add .`、金标patch/认证缓存/大镜像入库。
- 已完成用户授权的通用SWE入口与v2小样本。最终selected6/eligible5、10模型trial、160真实请求；on2/5/off1/5、7empty；本批保守账本$0.503462，累计$0.572059。未运行完整300题，未恢复DSH、未接Mem0/Dream-RSI或Langfuse远端，未依据五题与缺失cache字段宣称稳定提升。
- 最终head复核补充：文档提交f0d12c1的CI run43229586859在Ubuntu3.11暴露同文件另一项30ms模型启动假设（llm.closed=False，模型可以在前置deadline前根本未启动）。此前47的四组通过是历史事实，不能替代这个新head的检查。因此没有直接重跑CI取绿，而是审计整份15项测试并统一阶段同步fixture。
- 最后一批仅测试修订：model_wait(.03)、四stage(.08)、消费者已收到MessageUpdate的部分工具意图deadline共用deadline_at_stage，原预算值不增；120ms受控延迟分别复现旧失败。仅early-deadline保留真实30ms且允许任何前置位置耗尽；0.5s close和5s收集均是阶段同步后的watchdog。生产代码、归档、模型/评分/费用不改。
- 这批主代理Windows定点15 passed in1.21s，作者Linux同版15 passed in1.59s，Ruff/diffcheck与另一助手整份复核无阻断；新提交矩阵仍逐项跟踪。此后不再为写入CI终态而重复文档提交触发新矩阵，最终检查证据更新在PR正文及checks，最终答复只在新head实际四组通过后报告成功。


### E-BUDGET-READBACK-DIAG：2026-11-12 / 空补丁与未回读的只读调查

- 用户继续授权查明两项现象，并提供旧f0d12c1 CI失败邮件。两名Sol/medium继续执行，主代理负责设计/验收；子代理现场核PR2当前d437650/run43231106780四组success，旧30ms启动假设已修，不因历史邮件回退、重跑或更改通知设置。
- 空补丁调查：10份原Session结构与原官方空/非空结果绑定。7个empty最支持未进入源码编辑，未见编辑失败/回滚/提取丢失正证据；唯一非零shell是读取/grep诊断错误。3个resolved首次可识别写入在10/13/14次模型调用附近。没有逐步git差异快照，不能绝对排除隐式修改；“均budget_exhausted”只是共同终止条件，不等于预算不足的因果证明。
- 回读调查：相关四源码hash与765一致；11个artifact与canonical完整工具结果相同，生产只读回读全部匹配，重建预览隐藏中段合计32281字符并含完整handle/正确read指令，工具参数吻合。5个on中86个结构化调用均为shell，未见格式错误或可记录的回读意图。但Session不是原始Provider请求抓包，重建不能冒充实际传输；原SWE任务也没有“必须读隐藏中段”的标注，0回读本身不是已证实的接线缺陷。
- 设计取舍：当前system要求实际编辑，但无16请求预算提示。首实验仅加初始静态cap信息，不增步数/其它策略，避免尚无安全追加接线时动态改写前缀。每shell后新增不入模型的只读worktree计数，改进“没有编辑”与“提取遗漏”的证据边界。隔离回读探针另测必要隐藏中段，不强制所有真实任务回读、不混作SWE成功率。
- 方案与固定面板见 `docs/handoffs/2026-11-12-budget-visibility-readback-plan.md` 和 `results/swe-budget-visibility-20261112/SELECTION.md`。沿用原30美元账本与累计$0.572059，本批额外准入至多$2：probe阶段至多$0.25，随后SWE阶段至多$1.75。此记录时仅离线调查/方案，尚无新增模型请求或新效果结论。

### E-BUDGET-READBACK-CODE：2026-11-12 / 静态预算可见性与受控回读探针

- 改前 `d437650` 已修复截图所示旧 f0d12c1 的测试计时假设；主代理现场再核 run43231106780 四组 completed/success。旧失败通知仍是真实历史，不更改邮件通知或通过重跑掩盖失败。
- `swe_agent.py` 新增默认关闭的 static guidance，初始 system 只告知真实 API 上限（含辅助），同 run 不改前缀。`budget.py` 新增本地 request_ceiling 与只读 effective_request_limit，预留和提示共用；旧账本的16请求/$30根政策保持。`swe_campaign.py` / `swe_results.py` 明确区分实验条件、artifact_projection、budget_guidance；预算实验两臂投影均开启，官方预测/评分标签仍跟随条件，旧计划默认解释为投影实验。
- 每 shell 响应后新增宿主只读差异计数 sidecar，包含失败 shell；git --no-optional-locks 不写 index、不向模型反馈。观察或写盘失败隔离为 unavailable，不能掩盖原工具结果。指标是“响应后可见工作树差异”，新文件不自动称为源码编辑。
- 新 `artifact_probe.py` 通过生产 Agent / Context / Artifact factory 验证隐藏中段回读，真实 Provider 入口只记元数据（SDK 前、非 wire）。四 trial、每 trial 四请求，且在原 FileLock 内执行 min(root16,local4)；提前泄漏在 reserve 前拒绝，用量未知停全批。合成输入 SHA、成功条件、费用见独立 probe README，未硬编码模型回读决策。
- 作者 Windows/Linux 定点各17项、双方独立互审无阻断；主代理实际 `PYTHONPATH=src;.`，`python -m pytest tests/experiments -q -o addopts= --import-mode=importlib`：**114 passed、2 skipped、5 subtests passed in14.13s**。`python -m ruff check experiments/benchmark_live tests/experiments` 与 `git diff --check` 通过。CLI 实际动作为 plan/run/score/summarize（一次误用 create 的本地帮助命令被 argparse 拒绝，未发模型）。
- 运维脚本离线准备初次按 list 读取旧任务文件，实际是 tasks 对象，已按原 schema 修正；首次冻结的 fixture 未重生、未覆盖。此时账本仍192请求/全部已结算/$0.572059；fixture SHA `e0babe0f79fa847f79eea3f42a6d0d71bdb00d75cfe6db4ac9e24fc2795c223d`，三题输入 SHA `e7cf61df7dc418648f677526e0e12bfaa19dc6bb630d21a37799ed106957c89f`。先提交冻结源码并验 CI，再发新模型请求。
- 优势是把预算可见性与产物投影解耦，并补充真实回读/工作树证据；代价是额外观察开销和诊断脚手架。借鉴的是前批 DSH 的稳定前缀与 oh-my-pi/Hermes 的受限回读思路，本轮不调用 DSH、不将外部 Harness 成绩当基线，也未新增 Mem0/Dream-RSI/Langfuse 远端集成。尚无新模型效果结论。

### E-READBACK-PROBE：2026-11-12 / 真实回读与最终交付分开计数

- 源码提交 `c69ff37ed148f53d526b458b9b36233860b85f26` 在模型前通过[CI run43241787757](https://github.com/294962silent/my-agent-harness/actions/runs/43241787757)四组矩阵。Windows主机运维脚本最初在WSL调用git rev-parse，Windows worktree的gitdir `C:/...` 被Linux按相对路径解释而失败；当时尚未启动收费。改为Windows侧核完整HEAD/源码无脏改，Linux侧保留实际导入路径和冻结hash校验，不改共享.git指针或已提交源码。
- 实验输入保持首次冻结的两份日志；probe plan SHA `b0f50a8e40bfd33c65edda3800e3020ce0b2879e6a7528e763bf21aeec03f366`。顺序fixture-a on/off、fixture-b off/on，四trial共12请求全部结算，新增$0.009106，累计204请求/$0.581165。没有未知用量、提前目标泄漏或schema漂移，临时Session/产物正文已由所有者清理。
- on两次均观察到隐藏预览与handle、实际read/search、后续Provider输入目标可见；能力链路2/2。但第4次响应仍为工具调用，随后4请求上限耗尽，最终答案正确0/2。off两次均2请求正常end_turn并答对，2/2。读到和完成分开统计，不把回读能力冒充端到端质量，不提高上限补成功。
- SOL-1独立核四份audit/result及Agent.run终态：budget_exhausted映射为预算终态文本，probe按最终准确值判定；当前元数据不含响应正文/推理/finish reason，不能解释模型为何继续读。方法入口审计仍为pre-SDK而非wire。结果详见独立probe README，后续只归档元数据。
- 下一阶段维持三道固定诊断题、每题两条件的原方案：两臂artifact_projection均on，仅静态预算提示不同。新plan SHA `89377797e869d1ef662a7fd298c5ca91e8acd60bc726e9eaac611ebf1d28f1f5`，本阶段新增额度1.75，绝对ceiling $2.331165（前累计0.581165+1.75，未另加2）。三题按既有结果选定、不代表未见随机样本；此时先执行5227首配对，未评分，不预填成功率。

### E-BUDGET-VISIBILITY-RUN：2026-11-12 / 三题对照与用户调整优先级

- 实际生成及评分均已结束，仍为冻结源码c69ff37。六trial各16请求且usage全部结算；官方5227双臂resolved，11400/5103双臂empty_patch，合计on1/3、off1/3、4empty、2次实际测试、unknown/环境blocked均0。没有通过换题、追加请求或放宽评分消除失败。首次响应后可见差异为5227 off14/on11次请求，另外四条无响应后差异；100/100观察available，不能把该指标等同首次源码编辑。
- 费用：SWE off48请求/$0.148623，on48请求/$0.158567；合计96请求/$0.307190。probe另12请求/$0.009106，本阶段共108请求/$0.316296；加此前192请求/$0.572059，累计300请求/$0.888355。300是请求数、不是300道SWE；客户端保守冷价不等于供应商账单。cache_read仅92/96覆盖、cache_write0/96覆盖，完整缓存总量null，不给完整任务KV排名。
- 本次条件是budget_visibility，两臂projection均on。off产生8份/75999B，on15份/140052B；六份canonical Session均可解析，read/search均0。Session工具响应不是wire证据，原SWE没有必须回读隐藏中段的标注；独立probe的真实回读能力和最终0/2交付另记，不混入SWE成功率。
- 归档60个manifest列入文件+manifest，共61；manifest SHA `18888467ad123eb0b70f850e40ab1d2c93fa974f1fcefd17a02e02a2ba4883dd`。采集前核148项输入、100项原结果；108个runtime py同时核原SHA和c69 Git规范化blob，关闭此前仅比较commit常量的来源缺口。只复制安全metadata、官方结果、无正文侧车/探针审计；旧raw不改。所有JSON/JSONL、hash/大小校验通过，双方互审无阻断。
- 主代理实际执行新raw的`swe_results --archive --manifest-sha256 …`，得到6known/2resolved/4empty/3pairs、archive_modified=false；复用前批通用stdlib账本脚本独立重算96请求/$0.307190并保存analysis两JSON，均先核全部60文件。详见 `results/swe-budget-visibility-20261112/REPORT.md`，默认static仍关闭，未提供质量提升证据。
- 用户随后要求先适配Mem0、Langfuse、Dream-RSI，SWE/benchmark以后再测。本批运行在该指令前已全部结束；后续不新增benchmark/收费试验，保留以上记录。新适配将作为下一批独立设计、实现和提交，不把适配完成冒充真实远端接通或质量消融。

### E-THREE-INTEGRATIONS：2026-11-12 → 2026-11-13 / 三项可选适配

- 用户调整优先级后，仅复用两名GPT-6.1 Sol/medium实现，主代理负责接口取舍、集成和验收。旧结果收尾e81d424已推送；本批未新增模型/embedding/云端上传或SWE/效果benchmark，原账本保持300请求/$0.888355。方案见 `docs/handoffs/2026-11-12-three-integrations-plan.md`。
- Mem0固定OSS mem0ai2.2.1/git f1791daa86d469249c9b002f62644d58a1eb96a0，Apache-2.0。新增memory_semantic.py/memory_mem0.py与MemoryStore.aretrieve；core Agent实例注入，CodingAgent/Settings/RPC装配。改前仅本地词法；现在可选语义候选，但本地active/tombstone/scope/revision复验后才提供正文，infer=False避免二次抽取，错误回退词法。未测真实语义质量。
- Mem0成本与边界：每adapter单daemon worker，超时永久熔断避免叠加，不能杀死SDK同步请求/内部重试；owned资源逐一关闭，caller-owned保留。真实SDK回归发现Memory.close只关SQLite、Qdrant锁仍占用，补关自有vector/HTTP客户端并验证同路径重建；后续review补工作失败时finally清理、关闭/完成共锁以及工作失败但实际已关闭时不永久误报False。direct Agent.aclose也用finally保护ownedindex清理。
- Langfuse固定5.2.0/已核SDK源码00e5098bd6511a2f7bc7b6813ac125a3af559697，发行元数据MIT。新增LLM订阅句柄与observation_runtime.py，保留base observer，五入口每次开始快照并独立隔离异常。全局配置创建有界导出器，缺凭据清晰报错；close只detach/关闭自身，保留dropped/errors/alive。usage按完整total-completion映射input，prompt/cache仅数字metadata，缺失不补零；避免将仅未缓存prompt当全部输入和缓存双计。
- 运行期接线：Settings新增memory_index/observation；两块沿用既有特权字段规则，仅全局可设，项目不能修改外发地址/凭据环境引用。显式Python注入仍允许，memory_dir=False优先全局开关。RPC new/switch/fork/clone改内部async，先结束旧Agent在途和owned资源；未关完不假装替换成功。构造失败、取消、core关闭异常均尝试清理并保留原错误，外部LLM/observer不关闭。
- Dream身份由作者zhengkid/Dream-RSI README到Google正式代码仓库闭环；固定dream-rsi0.1.0/git8ec35bd2538220517ce1bfca4282db21b8c518db，Apache-2.0。新增dream_rsi_replay.py/_dream_rsi_worker.py与my-agent-dream入口，真实调用TraceEnv.replay+官方baseline，验证v1评分树/固定来源/预算，输出新目录与hash manifest。OptimalPolicy模板实际为空，因此未使用；删除无效beta调节项。Windows Job Object/Unix进程组限时清完整子树，不继承API凭据且禁网络。未启动online exploration/自动RSI或声称学得更优策略。
- 依赖/交付：pyproject三个optional extras+固定Git引用，uv.lock冻结88包；默认安装不启用服务、不导入SDK/建线程。主代理实际`uv sync --frozen --all-extras --python 3.13`，元数据核Mem0/Dream commit与版本；新增CI Ubuntu3.13 all-extras SDK契约lane和wheel模块/入口核验。用户说明与三份JSON示例在docs/integrations，面试材料同步，示例树明确合成契约数据。
- 验证：作者关键回归与对称只读review已完成，最后配置权限4参数例先红后绿，Settings13项通过；计数不与前146/42累加。主代理最终命令在worktree、PYTHONPATH=src;.、MEM0_TELEMETRY=false、PI_BASH_PATH=D:/soft/Git/bin/bash.exe，`uv run --frozen --all-extras python -m pytest -q -o addopts= --import-mode=importlib --tb=short` 为 **1254 passed、2 skipped、5 subtests passed in64.49s**。skip为缺可选Harbor，不是三个SDK缺失；Ruff/lockcheck/diffcheck与CLI --help通过。
- 真实SDK层级：tests/integrations三个用例经生产适配器；Mem0为本地Qdrant/SQLite+确定性假embedding、拒绝LLM与网络；Langfuse为真实SDK内存SpanExporter+生产sink/flush，非云端收录；Dream为CLI/worker→真实SDK的合成评分树，非效果benchmark。主代理wheel build成功，6项必需模块/TUI入口、Dream console及optional metadata均核验。环境无pyright，未声称类型检查通过。
- 本机初次全套失败为直接调用新venv Python时，工具子进程仍从PATH选全局Conda造成stdlib错配；改uv run后剩余原cmd无cat/pipefail差异，指定已存在Git Bash后用例通过、整套无排除通过。未改无关Bash产品实现或靠跳过用例取绿。最终source提交/最新head CI以本次实际PR/checks为准，此日志不预填远端成功。
- 优势是外部能力具有真实可用接缝、配置/所有权明确、结果可解释；代价是可选SDK/向量缓存、额外embedding/导出成本和回放输入准备。借鉴Mem0的索引API、Langfuse官方SDK/OTel、Dream官方trace/policy格式，不照搬外部论文/榜单数字；新增质量/消融指标依用户要求留待后续独立计划。

### E-THREE-CI：2026-11-13 / Linux退出确认与后台E2E时序

- 适配ec0fcdc240a1cb2cfe9760f9bdb113db95ef181f已推送，PR2范围/说明已更新。实际[run43257245212](https://github.com/294962silent/my-agent-harness/actions/runs/43257245212)的Windows3.11/3.13均success，Linux3.11/3.13失败；不以本地1254或Windows绿灯代替Linux验收，也不简单重跑取绿。
- Dream normal-exit后代/proc状态仍R：两Linux均失败。SOL-2原样复现并读取pending signal，证实SIGKILL已排队但约1.25ms后才退出，原代码只wait根。仅dream_rsi_replay.py补Unix整组有界退出确认（非Z/X成员），无法确认5秒报错，不引入全局subreaper/新依赖，不用sleep改测试窗口。测试覆盖受控存活序列、永不确认失败、comm/Z/R解析与真实清理；作者Linux3.12和Windows3.13各20项通过，SOL-1只读复核无阻断。
- 另一失败仅Linux3.11：[background E2E job](https://github.com/294962silent/my-agent-harness/actions/runs/43257245212/job/131318683497)首答Completed而非Waiting。SOL-1用真实job完成事件门控第2次假模型响应，稳定复现原断言；后台完成即add_followup，loop阶段末继续同run是合法语义，没有产品逻辑缺陷证据。仅改test_coding_agent_tasks_e2e.py，用release文件控制真实子进程、await真实job、第二轮全调用范围查通知/状态，finally回收；移除小sleep阶段假定。Linux3.12和Windows3.13各1项通过，SOL-2只读复核无阻断。
- 主代理修订后实际 `uv run --frozen --all-extras python -m pytest tests/coding/test_coding_agent_tasks_e2e.py tests/coding/test_dream_rsi_replay.py tests/integrations -q -o addopts= --import-mode=importlib --tb=short`：**24 passed in6.30s**，含三个真实SDK离线契约；Ruff与diffcheck通过。该批与旧1254/作者20有重叠，不累加。新的完整矩阵仍须由最新head实际验证，最终终态写PR/checks，不为记录绿灯再触发docs-only矩阵。
- 这批没有模型、embedding、云上传、SWE或效果benchmark；账本仍300请求/$0.888355。WindowsJob路径、三项适配范围及默认关闭不变，旧CI失败和全部原始实验保留。

### E-LOCAL-CPU-OPT：2026-11-13 / 本地中文记忆与受控参数自优化

- 用户解释需求后已明确选“本地CPU中文embedding”“受控策略参数优化”“暂无真实评分树，先闭环和功能验证”。项目定位收敛为面向长程任务的Coding Agent Harness，副线长期记忆/可观测执行/受控策略自优化；不是宣称任意代码进化、权重训练或持续提升。计划见 `docs/handoffs/2026-11-13-local-memory-controlled-optimization.md`。
- Mem0 SDK原已安装，optional仅是分发依赖和运行时开关。新增mem0-local extra，fastembed0.9.1/ORT1.31.1，uv.lock解析100包。固定BGE-small-zh-v1.5，中文512维/512-token、MIT，官方Qdrant模型repo revision46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59；7文件95332206B，ONNX94781076B/SHA1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38，另外6个独立Gitblob校验。缓存manifest SHA03283f4998f67e65623a88a408716bd8e8f22af9753c4c3132c28e3e59a6ab42。
- 下载实际问题：官方域直接TLS EOF/超时；显式采用WinINet匿名代理后成功，未降TLS/读HF认证或用镜像。prepare已有缓存也重验全部文件，不覆盖坏缓存；运行期仅本地路径。模型留用户.cache，不进项目Git。
- 新memory_local.py固定SDK局部构造真实Memory，因原Mem0 fastembed provider忽略CPU/cache实参；显式CPU/cuda=False/threads2/NeverLLM，不构造OpenAI或读取Harness认证。每adapter独占SQLite/Qdrant缓存，权重只读共享；ownedclose释放模型强引用及自身目录。冷构造同步加载，不在reconcile/search等待超时内；不宣称强杀SDK线程或RSS收益。互审补空userinfo代理拒绝、部分初始化逐项清理且保留原异常。
- 主代理实际运行本地/配置定点39 passed in2.42s，包括真实CPU模型生产链路：禁词法fallback并拒绝socket仍取得事实，实际ORT provider仅CPU、512维；另一例真实Mem0配假Dense验证装配/双实例独占/清理。代码功能证据不等于召回消融。作者Linux3.12无Mem0/FastEmbed/Langfuse/Dream环境另测本地模块与Settings，37 passed in2.19s、无skip，证明分发默认导入兼容。
- Dream新增contract/optimizer/registry，参数只允许max_width/max_refine，不执行生成Python；固定网格候选在结果前冻结，只train选一个，holdout验baseline/该候选/冻结incumbent。父进程从冻结原始score、action/cost重算，不信summary布尔；source/currenttask/语义树防split泄漏。指标为归一化回放best均值，非论文reward或真实任务成功率；SDK reset best实际为0，没有把baseline分数伪造为初始已达。
- 自动晋升只有observed声明、完整验证且严格胜baseline及active；锁内CAS核target/version/完整token，nonce防ABA、同参不重复，原子指针最后提交，previous/rollback可核。replay实际消费合法active参数，损坏回退baseline，优化内部use_active=False冻结基线。总墙钟300s+每worker剩余时间、候选/树/步数上限；失败不丢样本或改预算。源声明不是密码学评分认证，仍不能伪造Session/SWE单链成评分树。
- 无真实数据：所有observed晋升用例仅临时fixture/注入评估器；真实SDK用synthetic验证参数改变真实决策、搜索/active消费/损坏回退，不宣传提升。主代理又用文档synthetic manifest f2242a11ab5d17cfc6dfb7daff56ae9765ecb4bea51c0cc6141e93946dc1b74d经真实CLI完成dry-run，status=completed/promoted=false；没有写生产active。示例在docs/integrations/dream-demo。
- 本机已merge-only启用：全局C:/Users/MLL/.my-pi-agent/settings.json，原文件在同目录.integration-backups留备份，不进Git、不输出内容；其他所有字段逐项相等。memory_index使用绝对mem0-local.zh-cpu.json、timeout10/close1；本地profile无key。dream_optimization enabled=true、dataset_manifest=null，实际status为needs_dataset/no_active_policy；正确optimize --output NEW仅返回needs_dataset，未创建目录。已有进程需重启读配置，不强行终止其他会话。
- 最终本地主代理命令以私有MY_AGENT_HOME测试目录隔离实际用户配置，显式模型缓存env、MEM0_TELEMETRY=false、PI_BASH_PATH与src路径，`uv run --frozen --all-extras python -m pytest -q -o addopts= --import-mode=importlib --tb=short`：**1302 passed、2 skipped、5 subtests passed in77.78s**。两个skip为Harbor，真实CPU模型例已执行；Ruff/lockcheck/diffcheck通过。作者Dream Windows40/Linux38有重叠不累加，双方最终只读互审无阻断。
- 本批不需要额外模型提案：参数网格搜索完全CPU/已记录结果，未调用付费模型、embedding API、Langfuse云上传、SWE或效果benchmark。旧账本和两份原始归档保持。优势是本机记忆可用且费用/外发边界清楚、受控优化有真实active/验证/回滚；代价是约95MB模型缓存、本地CPU/冷启动、额外候选回放与真实数据准备。借鉴Mem0 API、FastEmbed官方模型控制、Dream评分树/固定策略类，未冒称其完整自动代码RSI已启用。

- 交付前增验：本批wheel构建成功（188项），memory_local、Dream contract/optimizer/registry/worker等8个必需模块及TUI入口、Dream console和mem0-local/fastembed元数据均核验；再次status仍needs_dataset/no_active_policy。Ruff/lockcheck通过，按仓库行尾规则diffcheck通过；最终提交及CI状态以PR/checks为准，不把旧head绿灯外推为新head。

### E-LONG-PILOT-PLAN：2026-11-13 / 恢复小样本长程与记忆实验

- 用户在基准选型建议后明确要求设计并开始；先前暂停不再阻止本批已授权实验。新计划见docs/handoffs/2026-11-13-long-horizon-pilot.md，两名Sol/medium分别拥有benchmark_live与longmemeval，主代理负责协议/成本/材料。
- 固定TB2.1八题、SWE-EVO两题、LongMemEval cleaned按六类型各5题；代码只切artifact_projection，记忆只切none/production lexical/Mem0，不做全框架一次开关或Dream在线效果宣称。数据/官方Git blob与LFS均核，源SHA和限制见计划。
- 环境核验更正：L20可连但无Docker，历史官方任务实际运行于本机WSL Ubuntu-20.04/Docker26.1.3；本批优先WSL CPU，所有子进程CUDA_VISIBLE_DEVICES=2，无GPU训练或推理。原数据、原checkout和旧结果保留。
- 原账本实际300请求/888355微美元，unknown held0，根上限$30不重置。记忆阶段绝对cap$3.888355，代码阶段绝对cap$15.888355，本批最多新增$15；保守客户端价格不是供应商核账。run级冻结64请求/任务原时限(上限1800秒)，旧16/600默认不改。
- 原样TB首轮八oracle均registry连接超时，属于environment-blocked，无模型新请求。旧fix-git verifier uv下载失败也保留。定位到WSL旧代理端口失效，正用仅WSL客户端可访问的临时转发和官方crane匿名拉镜像，再由Docker导入；不改共享daemon或官方task/test/solution。crane0.22.1官方release digest已核；第一镜像已导入，完整oracle尚未通过，不能写成模型成绩。
- 新开发问题P30–P35同步写入问题账本和九段深挖文档，明确当前技术验证阶段、取舍、失败路径、责任边界和待补证据。规划指标、任务成功率、召回率与本批费用全部待实际产物计算。

### E-LONG-PILOT-IMPLEMENT：2026-11-13 / 预算、驱动与付款前验证

- 新long_horizon.py提供freeze/run/summary，源/输入/oracle/ledger/模型/任务时限绑定，run需显式execute，首轮默认最多2trial；journal与ledger trial_started防跨目录重复执行。只对完整且schema一致的配对计算差值；缺缓存用量保留None。Harbor原task不改，环境代理冻结为HTTPS-only/HTTP direct，避免代理使Debian apt返回502。
- 新harness_ablation/corpus/retrieval/paid提供30题冻结、同语料生产三臂、独立Linux seccomp worker、回答与judge固定不同1请求/180s run；输入标签隔离、响应逐字重建、无效结果保留、付款前hash核验、BudgetStop封阶段不重试。上述是固定历史索引实验，不测自动事实抽取效果。
- 实际依赖安装曾多次失败：uv内部Git仍命中失效URL级代理，普通HTTP_PROXY/进程Git配置不足；用仅本任务PATH内git wrapper显式-c覆盖后成功，未修改共享Git配置。按uv.lock导出安装Linux mem0-local，真实CPU+seccomp单chunk经生产链路成功，close_complete=True，0API。
- 公共镜像通过官方crane0.22.1匿名下载后dockerload；8个镜像均可inspect，customheap大镜像load客户端超时后daemon仍完成导入，未仅凭CLI超时就断言导入失败。未改共享Docker daemon；保留最初registry失败、后续uv依赖失败和HTTP代理502的各次oracle原始证据。HTTPS-only第四次第一题cancel-async-tasks官方oracle实际reward1/noexception，其他仍进行中，不提前写为8题全过。
- 交叉review抓到P36：Memory计划漏真实付费/guard源码，TB汇总在schema不同时仍算delta。双方修复并互相复核，付款前篡改反例不写ledger。主代理最终Windows命令沿用隔离MY_AGENT_HOME、真实模型缓存及Git Bash，全套 **1334 passed、4 skipped、5 subtests passed in94.93s**；Ruff/diffcheck通过。WSL记忆12项及实际Harbor定点另有重叠，不累加。
- 本批此刻模型新调用仍0，旧300条/$0.888355不变；下一步冻结源码/选题、先首个检索和付费对照再扩展。所有收益和完整费用以实际原始记录为准。面试P30–P36已附深入取舍/反例/证据与个人边界。

### E-LONG-MEMORY-PROTOCOL-V2：2026-11-13 / 真实数据探针纠偏

- dfcdc5b代码与设计a55aa3a已推origin，新head四平台CI已实际completed/success。记忆plan冻结30题，首题921chunks经真实CPU/生产MemoryStore/Mem0及seccomp完成；此时无新模型请求。
- 主代理读取真实context发现官方SID的answer_前缀暴露证据身份。即使未传has_answer也可能泄漏；该单题探针排除正式成绩，旧memory-plan/retrieval不覆盖。SOL2把所有SID等价映射为sid_<24hex>，原映射与gold仅host references，协议升fixed-history-index-ablation-v2-anonymous-sessions。正式实验另开memory-plan-v2/retrieval-v2，完整历史/日期/30ID选择规则不变。
- 新行为回归经真实Fact磁盘/索引/reader验证原SID标签与gold canary未入公共材料，Recall仍可算；source/旧plan拒绝和append稳定性同测。已用retrieval/已判答案hash变化会在付款前拒绝；WSL15tests通过。没有把排除探针的Recall1或冷构建耗时当正式召回/性能收益。
- 临时urllib models GET曾被403/1010拒绝；改用项目真实SDK（既有项目UA与稳定session header，未伪造官方客户端身份）实际GET成功、flash可用。此为零generation连通检查，不是Agent基线或付费任务完成。
- TB HTTPS-only runtime下已有7/8官方oracle reward1；git-multibranch仍uv release TLS失败/uvx缺失，保留blocked并继续基础设施诊断，不换题。SWE-EVO固定fork73文件/471797B已逐Gitblob验证，真实评分入口导入与help通过，镜像准备中，尚无EVO成绩。

### E-LONG-VERIFIER-CONTRACT：2026-11-13 / 官方结果与归因资格

- 真实oracle证明缺uvx仍可能reward0/noexception，另有namespace名称和fake短名不一致。SOL1最小修long_horizon，保留official_reward/category，不改generic解析；缺预期ctrf+明确安装器失败/uvx缺失+无测试执行才suspected，证据有path/bytes/hash且有界读取。正常代码/collection失败继续scored0。suspected或schema不一致不能计算科学配对delta。
- 名称按冻结oracle身份仅接受短名或terminal-bench/<短名>，拒绝其他namespace；started恢复收割真实result不重跑。最终WSL21项、Ruff通过，无模型请求。SWE-EVO两官方镜像也已导入、config ID保存，固定forkgold/empty CPU预检开始准备，仍未宣称EVO通过。

### E-LONG-SMOKE-AND-LOCAL-RUN：2026-11-13 / 正式首题与本机批次

- 用户明确暂不迁移，继续本机；全部执行器、Docker与CPU索引留WSL，生成/judge仍调用既有远端DeepSeek接口。不在L20跑任务。
- v2匿名SID正式首题06db6396完成3回答+3judge，none0/1、lexical1/1、mem01/1；两检索证据会话Recall/MRR均1。独立源码/输入清单核对和原文人工辅助审验支持答案5，模型输入仅匿名SID（重建SHA证据，不是抓包）。n=1不能做质量收益结论。
- 六请求全ok，prompt3062/completion470，客户端按全冷参考价逐请求结算1485微美元；原账本现306请求/889840微美元，unknown0，cache未提供保留unavailable。原始六条metadata/usage和首题指标存docs/experiments/results/long-horizon-20261113/smoke-v2，不含密钥、prompt、gold或完整历史。
- 实际Mem0建库等待1800秒/close60秒与产品工作等待10秒不同；首题约100秒包含921块首次embedding/索引，不称query-only延迟。原始历史分块检索不测自动抽取/巩固/脱敏写入。详见首题报告和P41。
- 30题Memory批量resume已启动，由专属进程顺序retrieve→answer→judge→summary，已完成首题不会重付；TB另进程等待Memory成功且无unknown才freeze实际计划、先跑2trial，再仅在完整/同schema/无疑似verifier故障的配对后续跑余下。两个进程只读旧123源补充清单，原账本$30、本批最多新增$15保持；没有自动恢复已失败API。
- TB原8题最新7oracle通过，git-multibranch追加同配置网络复查仍0且依赖问题，保留所有轮次不替题。SWE-EVO固定fork四项gold/empty已真实评分：Dask gold F2P44/44、P2P2861/2861，empty0/44、2861/2861；Conan gold72/72、640/649，empty0/72、630/649，因此仅Dask可进入生成，未把gold失败归因模型。
- Dask原镜像Git含4569个不属于base祖先的可达提交和303refs，存在基线之外信息通道。新接缝同tree单root清理三次探针通过，原objects/reflog不可读；丢失合法历史能力的代价记录在P39。EVO交叉review另发现foreign report路径/额外sitecustomize可执行清单缺口，修复复核中，尚未EVO付费。
- 已推16bdae0的CI run43408977540实际completed/success；新增EVO文件未据此宣称CI已覆盖。首题报告、后续完整结果、官方预检与不同协议探针保持独立，不覆盖历史。

### E-LONG-MEMORY-COMPLETE：2026-11-13 / 30题真实三臂结果

- v2固定30题已完成90回答+90judge，180请求全部ok、unknown/pending0，客户端49191微美元（$0.049191）；全账本480请求/$0.937546，仍受原$30约束，未重置起点。原单题6请求包含在180中，不重复相加。
- none4/30、mem015/30、lexical22/29有效且另1不可用。lexical的75f70248判分在1024输出上限以length结束、正文空，invalid_judge_format；不补零、不从推理猜分、不加请求重跑。三臂共同29题：lexical22、mem015、none4；mem0-lexical为-7/29。4弃答三臂全对，召回分母26。
- 检索90槽位valid且全部close完成，词法session Recall@8/MRR为0.86538/0.80449，mem0为0.73077/0.61255。总耗时均值4.32/99.13秒含首次建库，不当warm查询或线上SLA；中文BGE面对原英文数据，固定历史索引不等于自动抽取管线。本次结果不支持语义优于词法，不改默认值、换题或覆盖负结果。
- 独立stdlib审计核完全部manifest、输入重建SHA、检索/答案关联、exact判分及ledger一对一，require-complete/summary对照通过。公开metadata在results/long-horizon-20261113/memory-v2（6份机器证据，含manifest），无prompt/gold/历史。使用原summary bytes，并可从90行独立重算统计。
- usage合计prompt95175/completion17067/total112498；有一条另列cache256，全输入按total-completion为95431。按每请求全冷参考价向上取整重算49191微美元，与ledger一致；cache_read覆盖仅1/180、write0/180，完整KV命中率不可用，不排名。供应商账单未核。
- 面试材料新增P42判分可用性与P43负结果定位，均按九段证据深入，保留个人责任和适用边界。[完整报告](results/long-horizon-20261113/memory-v2/REPORT.md)。

### E-LONG-EVO-REPAIR：2026-11-13 / 官方评分闭包与可复现接缝

- 新增evo_agent.py、evo_cache_adapter.py及test_evo_agent.py，不改Memory/TB/预算/src/uv.lock既有冻结文件。EVO支持freeze/solve/probe/score、官方双gate、64请求/1800秒、源/模型/预算绑定和独立fresh评分；仅Dask符合前gate，Conan仍blocked。生成清理原Git历史，同tree固定单root，不声称完全消除泄漏。
- 修复前只glob唯一report、只核已知73hash，会接受foreign报告或额外sitecustomize。修复后绑定固定run/model/iid报告路径、外层iid及父路径无symlink；校额外可执行inventory，并从原bytes建立clean snapshot，不复制旧pyc。-I -B隔离启动，解释器/site-packages仍信任，不是任意代码沙箱。
- adapter公开为可配置根路径，原60672af1…文件/前gate不改；新dda7435e…的cached_pull和ALLOWED经独立AST核相同。公开73文件manifest与原8abe773c…hash一致，docs/experiments/SWE_EVO_ADAPTER.md给下载/核验/CLI和信任边界，不再依赖私有硬编码路径。
- 作者WSL19 passed；主代理Windows实际13 passed/6 skipped（WSL评分及Unix symlink边界），Ruff/diffcheck通过；独立复核原反例11项通过，不把重叠计数相加。真实clean75file逐hash、0pyc、-I-B --help通过；新head CI尚待本批提交后核验。
- 为避免源码稳定前自动freeze，主代理核原Terminal等待器waiting_for_memory、无child/无已开始trial后只停止该等待器，保留停前快照和stopped_before_freeze原因；Memory未中断。待源码稳定另开Terminal调度阶段，不重跑已消费任务。
- 真实clean-Dask-empty零LLM契约首次在官方评分前因私有脚本漏mkdir(output_final)失败；产品score已有mkdir。仅修私有脚本，保留首失败目录，新v2目录重试；不影响账本或原gold/empty。最终评分结果另行追加，不预填成功。

验收追加：主代理实际执行新v2 clean-Dask-empty已通过：官方CLI退出0、F2P0/44、P2P2861/2861、resolved=false，原report SHA仍053b84ab…，与旧empty一致；75份快照逐hash一致、pyc0、无cleanup_error。0模型请求、没有伪造generation run.json。安全证据存[evo-contract](results/long-horizon-20261113/evo-contract/REPORT.md)，它是评分装配契约，不是Agent解题成功。

本批集成回归：主代理Windows在冻结依赖下实际执行 `uv run --frozen --all-extras python -m pytest tests/experiments -q -o addopts= --import-mode=importlib --tb=short`，**171 passed、10 skipped、5 subtests passed in32.47s**；包含上面13项EVO，不重复累加。skip含6个明确WSL/Unix边界和既有可选环境条件；不是当前整库总测。独立EVO源码审查与真实clean评分覆盖关键Linux路径，最新远端四平台CI待推送后核实。

跨平台追加：提交前LF→CRLF提示表明新adapter和源码manifest的固定字节pin可能在Windows fresh checkout失效。仅.gitattributes追加最小LF规则，保留原stdout与旧证据规则；执行助手用真实`git -c core.autocrlf=true checkout-index --prefix=<新的私有目录>/ -- <27目标文件>`核验27文件hash全同、CRLF0，adapter dda743…与manifest8abe773…不变。主代理随后核34个暂存文件、21份JSON、三份产物manifest及暂存bytes与原证据相同，实际API/Git凭据精确匹配0。未将缓存、模型、auth或本机配置提交。

### E-LONG-CODE-START：2026-11-13 / 源码CI与本机正式首配对

- 实现/档案已推自己的origin，源码head455915743ee687006698eaeefe3a98d43b55609e；PR2正文按最终实现与真实Memory负结果重写，base仍codex/context-memory-reliability，未合并。该head的[run43422249290](https://github.com/294962silent/my-agent-harness/actions/runs/43422249290)已completed/success，四组Ubuntu/Windows × Py3.11/3.13实际全部成功，Ubuntu3.13 SDK专项实际运行成功；主代理核run/head与四个jobs，不用旧绿灯外推。
- Terminal新调度器terminal-queued-v2已在本机启动，实际计划8cb76c1479c97638eed024e627e13397a1fe63078b4209105886ac4442e16216，首cancel-async-tasks off/on。七题通过oracle可候选配对、一题双槽blocked；首个完整/同schema/无suspected配对后续跑其余槽位。此刻尚无完整结果，不预填质量/费用收益。
- [启动快照](results/long-horizon-20261113/terminal-start/REPORT.md)保留源码、任务、约束和预算，不公开运行时凭据或代理。后续SWE-EVO仅Dask可运行，须前阶段完成、账本无unknown及源码CI已核；Conan不因失败换题，继续blocked。所有执行留本机，L20未运行任务。

### E-LONG-CODE-STOP：2026-11-13 / 实际故障触发停止，费用不补零

- 首off正常end_turn，raw reward0但uv0.9.5下载TLS35→env/uvx缺失，无ctrf、未执行测试，suspected=true。on在原900秒上限取消、Harbor AgentTimeoutError；验证器执行且raw reward1，但分类结果execution_or_verifier_error/official_reward=null。两者原分数、执行异常与配对资格分开，科学有效pair0，delta=null，不能宣称投影0→1提升。
- 两trial共53请求，52已结算、1cancelled缺完整usage。已知$0.281062，完整任务费用unavailable；45131微美元预留保留。全账本533请求，已知$1.218608+未知预留$0.045131=客户端已知加预留$1.263739，不是完整实际费用或供应商账单。原$30不重置，unknown未补0；队列核到unknown后stop，剩余14槽未启动、EVO未发模型。
- 定点调查未见代理漏配：实际off job/Harbor/trial与成功oracle同HTTPS-only策略，env.exec确实传递。独立fresh同镜像运输探针只丢弃下载体、不test.sh/评分，CONNECT GitHub→302→CONNECT release-assets→200/exit0，NO_PROXY未绕过；当前可通不能追溯原TLS原因。首私有probe解析器None stderr失败另存，不把探针当前成功改写原trial。
- [首配对停止报告](results/long-horizon-20261113/terminal-first-pair/REPORT.md)公开安全原reward/分类/证据hash、53请求与未知预留、运输元数据；原result/ctrf/stdout/Session和ledger留本机。终态不是跑满八题，不把运行失败当完整benchmark成绩。Memory180请求/30题仍是独立已完成实验。
- 付费进程均结束；主代理确认本批队列/Harbor PID已不存在、精确任务及探针容器剩余0，核cmdline/父子身份后关闭自有临时代理41392/18360及17913监听，用户原7897服务仍在。没有迁移L20、改共享Docker或重启用户代理。worktree和失败档案保留。

### E-LONG-ERROR-AND-RSI-AUDIT：2026-11-13 / 解释负结果，纠正融合口径

- 用户追问Mem0为何落后、RSI是否融合、如何证明长程能力。两位既有Sol/medium分别只读核评分分解与RSI调用链，0新增API、未改原数据或未知账本；主代理独立从90行重算15组计数通过，诊断存[memory-error-analysis](results/long-horizon-20261113/memory-error-analysis/REPORT.md)。
- 词法对/Mem0错8题、反向1题；前8中3题标注会话覆盖不全、5题会话全命中仍错。抽查其中2题，词法送入具体答案chunk而Mem0没有。故session Recall=1不等于答案证据已送入，不能直接归因reader。真实BGE tokenizer重建27517条索引文本最长463tokens、超过512为0，本批没有512截断证据；语言/阈值/无rerank贡献仍待单因素实验。
- 实际Mem0 infer=False、top8、threshold0.1、rerank=False；本轮是原始历史索引，不是整套记忆抽取/巩固质量。未换模型或重新付费选答案，原负结果保留。
- RSI源码确认optimizer默认评估器replay_trace，worker为TraceEnv.replay→ParallelRefineMethod.solve，active只被replay消费。真实Agent未自动产出对应独立评分树，也未用active控制在线动作。已完成的是离线受控参数研究通道，线上主链尚缺接线；纠正“适配完成”可能造成的完整融合误解。默认registry只读no_active_policy，未核任意自定义registry，不推广配置结论。
- 更新integrations说明与CLAIMS，分别说明已实现机制、已完成Memory实验、无效Terminal对照、缺失在线RSI和真实长程收益。新报告给出相同预算/工具/模型、仓库隔离与隐藏最终测试不反馈选分支的建议；属于待讨论方案，不是已实现或已跑的新实验。执行源码未修改，不因文档更新重跑模型。

### E-LAYERED-COMPACTION：2026-11-13 / 先低损投影，再有界摘要

- 用户采纳真实RSI闭环优先/三臂等预算，并给Claude Code五层压缩截图要求改造。复用两名Sol/medium，SOL-2拥有Context管线，SOL-1接真实RSI并独占Agent/配置工厂；主代理负责集成。起点0e9ef15，初始工作树干净；Git改前归档SHA256 439fbae0dca996e43a3ca1fe12e2cf59af445f6c5bf4078364c8be6e37a7f586，旧context-cache基线不覆盖。实施约束见handoffs/2026-11-13-online-rsi-layered-compaction.md。
- 新context_compaction.py的CompactionPolicy支持baseline兼容路径与layered显式启用。canonical/分支摘要身份先验→既有大结果外置→旧完整成功tool组micro成可回读handle→显式完成前缀fold→预算内结构化摘要。当前user turn、首条user契约与host标记context_protected原文保留；未知/错误/未闭合/近期tool组不误删。原canonical不改、cut使用原坐标，不能适用的前缀或无法容纳保护文本时拒绝请求。
- 涉及context.py、新context_compaction.py、core Agent三个policy透传点、test_context_layered.py及docs/context-compaction.md。动态micro不作为缓存身份；fold复用Session已有覆盖指纹和持久化桥，不新增任意中段schema。保留输出/安全/摘要reserve和purpose=summary，复用原注入LLM，不新建裸client或隐式重试。
- 官方[context editing文档](https://platform.claude.com/docs/en/build-with-claude/context-editing)说明清理工具结果会使对应缓存前缀失效；截图cache_edits不作为DeepSeek/OpenAI能力依据。本版不发送provider cache edits、不启用模型任意删句、不宣称完整复刻五层或KV收益。结构化摘要是有损的，回读与保护文本不能证明所有语义无损。
- 交叉review复现P2：新摘要不能fit时曾清除内存中仍合法的旧摘要。auto/manual两个反例先红后绿，改为只对当前canonical先验有效的旧cache保存事务快照，失败恢复并pending=None继续拒绝；失配分支绝不恢复，磁盘Session未被错误覆盖。SOL-1修后只读复核通过。
- 作者Windows组合101项通过；修前WSL新用例10项通过不外推为修后结果（批次有重叠）。主代理修后实际Context/Session/Artifacts组合 **94 passed in1.55s**，Ruff通过。使用fake摘要provider与真实磁盘Session/ArtifactStore，不计作模型任务成绩，最终跨平台状态另核CI。
- 独立synthetic机制对照见[compaction-contract](results/context-rsi-20261113/compaction-contract/REPORT.md)：同29984B canonical、5000输入预算、512输出reserve；baseline fake摘要1次后4108B，layered fake摘要0次后12044B，6份真实产物nonce回读成功，两臂canonical不变/重复稳定。baseline最终更小，layered只证明先micro避免一次合成摘要；不编造成质量、速度、总token或缓存提升。显式前缀fold的静态host摘要与LLM摘要分列，Session保存/恢复已验。
- 本批付费API仍0新增，旧unknown/预留保持。RSI和本地Qwen GPU适配仍在独立文件实施，不能据本次Context验收提前宣称它们已完成；后续分批追加真实接线/本地模型验证结果。

交付前补充：主代理在新的私有输出目录实际运行`python -m experiments.context_rsi.compaction_ablation --output <新目录>`，所得metrics与公开指标JSON完全一致；11个本批暂存文件实际API/Git凭据精确匹配0。只暂存本批Context、三点透传、独立机制证据与计划/日志，不纳入仍在实现的RSI/GPU文件。

### E-RSI-EXECUTION-LOOP：2026-11-13 / 补齐真实分支与冻结策略消费

- 之前active只供离线replay，Agent不会自行产对应评分树。本轮新增rsi_runtime.py/rsi_branch.py，CodingAgent.run显式接portfolio，现有core loop/文件工具执行独立分支、host可见命令评分，合法完成前缀导出train/holdout trace。FrozenPolicy可在下一episode读registry active或复制已验参数，真实控制width/refine；无active失败、运行中不热换。
- ParentBudget/SharedLLM共享请求、token预留、deadline与评估池，包含摘要/辅助异步调用及后置final；未知usage保留reservation并停止。total-completion兼容另列cache。无通用裸client透传、同步provider入口明确unsupported。children首版禁MCP/skills/plugins/memory/background/retry，不复制Agent loop、不从生产导入experiments预算。
- visible用于探索，独立final仅选定产物后在副本执行，反馈不进选择或训练；TaskSplits校任务隔离，synthetic永不生产晋升。选定tree/patch只供显式review/apply。目录隔离非OS沙箱、apply非原子、不消除所有TOCTOU；同步snapshot/copy/export可阻塞IO，非所有操作硬墙钟抢占。RSI首版串行、单次runtime，TUI流式run_stream明确unsupported。
- 交叉review修复无尾换行patch、真实Windows junction越界和cleanup失败仍导trace三类问题；Git应用五组LF/CRLF/无换行逐bytes正例、junction拒绝与普通copy、取消传播均有回归。作者最终44项，早先190项属修前历史；主代理最终70项组合通过，包含44 RSI及26 memory，不重复累计。
- 实际CLI使用真实CodingAgent/工具/本地测试与固定Dream SDK optimize dry-run；A/B/random各4有效尝试、12脚本请求/6评估，C为2尝试、6请求/4评估。ceilings全部12请求/300000tokens/90秒/6评估，SDK21次离线replay选择width1/refine1。四臂独立final通过、源answer仍0；quality=unmeasured、final_success=null、未promote。C少调用为合成夹具行为，不写成50%真实节费。A→B同时切layered与artifact，是工程增强组合。
- 主代理在全新目录独立复跑，源码hash/参数/计数/上限与公开证据一致，见[rsi-contract](results/context-rsi-20261113/rsi-contract/REPORT.md)。保留作者与复跑raw hash，不覆盖旧档案。真实模型/未见长程收益尚未测；下一批四臂[实验协议](RSI_CLOSED_LOOP_EVALUATION.md)已设计但真实任务manifest未冻，不自动恢复付费benchmark。

### E-QWEN-GPU：2026-11-13 / 本机独立embedding与SDK边界

- 本机RTX5060 Laptop 8151MiB/cuda:0/sm120，独立Torch2.9.1+cu128与Transformers4.57.6；默认应用venv不装Torch。官方Qwen3-Embedding-0.6B revision97b0c614…10文件1,207,470,234B逐官方Git blob/LFS校验。大权重httpx成功、小文件TLS EOF后改官方native curl，TLS不降级、不用镜像/认证token；下载失败不伪装成功。GPU只embedding，不能生成摘要。
- 新memory_qwen.py/_memory_qwen_worker.py及mem0工厂分派：独立-I -X utf8 -u stdio worker，fp16 SDPA/last-token/L2归一化1024维；batch1..4/max_length64..1024，超长显式报错。local_files_only/trust_remote_code=False/token=False，剥离凭据代理、拒PythonInternet socket，非OS网络沙箱。1024维新Qdrant目录，不复用BGE512旧库。
- 真实路径暴露Mem0会懒下载BM25/spaCy，改Qwen-owned dense-only建库及隔离SDK函数globals禁NLP；固定版本/commit/10方法LF规范hash，未知源码失败。不是SDK全局patch，也不是完整原版混合语义。**旧结果更正**：当前BGE只读装配BM25 slot=true、原NLP函数，但历史30题实际是否触发未记录；不能无条件称旧臂纯稠密，未来模型比较须控制此变量，原负结果与raw档案不改。
- 修复Windows GBK中文stdio、worker不读导致write/flush无限阻塞、venv launcher与真实worker PID不同。统一发送/接收deadline、有界1MiB帧、熔断终止owned树并确认线程/native handle；清理窗口独立，不宣称瞬时OS退出。SOL-1对send_deadline独立1项复核通过。
- 主代理最终GPU实测禁词法回退、host/worker拒Internet socket：两事实中英文均返回语义候选，1024维norm约1；冷构造16.45s、文档batch2约1.30s、两查询0.467/0.053s，短文本峰值allocator1,207,842,816B。这是功能smoke，不是质量/吞吐/SLA benchmark。close_complete与worker退出核验；owned终止returncode1如实保留，桌面显存2437→2435MiB不当作零显存承诺。
- 全局settings私有备份后merge-only启用layered（output reserve16384）、GPU profile与memory等待60s；CPU profile/模型保留，其余字段与Dream入口原值不变。实际Settings→RPC→CodingAgent→Context/GPU factory构造后关闭验证0provider调用。新配置需本分支worktree，旧checkout切回需恢复CPU配置；本机配置/模型/向量缓存不进Git。
- 最终主代理Windows冻结依赖整库：`uv run --frozen --all-extras python -m pytest -q -o addopts= --import-mode=importlib --tb=short`，**1424 passed、11 skipped、5 subtests passed in107.00s**；Ruff src/tests/experiments/context_rsi、uv lock --check、wheel新模块/TUI与源码bytes比对通过。新增CI专项含RSI闭环与GPU CPU契约，不冒充CI有真实GPU。当前最终head远端CI待提交后核验。
- [GPU安全证据](results/context-rsi-20261113/qwen-gpu/REPORT.md)与[面试P45–P50](../interview/11-RSI闭环与本地推理工程深挖.md)已同步。付费API本轮0新增，不改旧账本533请求/已知$1.218608/未知预留$0.045131；所有进程留本机，不迁移L20。真实RSI收益与Qwen召回效果保持未测，Langfuse云上传状态不因本轮变更。

交付字节核验追加：Git暂存暴露5个源码文件CRLF与提交LF指纹差异，仅做换行规范化；历史source hash不覆盖，另存before/after/CRLF行数映射与最终checkout-validation。最小.gitattributes规则保护11份RSI/Qwen源码，实际autocrlf=true导出11文件逐bytes相同。规范化后专项44 passed in17.98s，新目录CLI四臂计数/上限一致、0provider、未晋升；重新构建wheel按最终源码bytes核验。没有以格式变化改写旧实验结果。
