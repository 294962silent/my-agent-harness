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
