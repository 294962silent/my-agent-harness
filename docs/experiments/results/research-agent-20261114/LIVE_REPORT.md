# Wine Quality red：真实代码实验流程验收

2026-11-14。**真实 v2 完成两轮、新进程恢复及一次独立 final；两个 checkpoint 均保留原均值 baseline，没有算法质量改进。** 本报告仅验收 red 的工程流程，未完成初始三数据集计划或正式效果消融。

机器摘要见 [live-summary.json](live-summary.json)。SOL-2 独立读取本机私有 `live-v1/red`、`live-v2/red` 的 journal、round result、calls、checkpoint、预测与经验文件，校验每条 journal 的 sequence/previous/manifest/hash 链，逐请求比较调用 trace 与预算记录的 token 数，复算产物 hash；不重跑 API、embedding 或 final。私有根为主仓库 `.git/codex-delivery/research-runtime-20261114/`；公开交付仅聚合与 hash，不包含标签、候选/session 或完整响应。

## 运行与统计口径

- **v1 原失败**：1 次真实请求、1252 reported tokens、1 次 dev baseline 评分、0 committed rounds；MemoryError，未运行 final。该次调用已结算，不能删除或记为零调用。失败具体行无法从原始证据恢复；轻负载离线重放通过不能证明根因已修复。
- **v2**：24 次真实请求、106826 reported tokens，8 次 dev 评分加 1 次独立 final；两轮均完整提交。round 0 结束累计 12 请求/48915 tokens/4 评分；新进程恢复后 round 1 memory_finished 仍保存相同记录及 root_id，第二轮后累计 24/106826/8。final 只增加第 9 次评分，不增加模型请求。
- **全部 red 运算成本口径**：v1+v2 共 **25 请求、108078 tokens、10 次评分（9 dev+1 final）**。USD 账单没有证据，保持 unavailable；不能把 reported tokens 换成已确认费用。Mem0 的本地 CPU embedding 单独记数，不计作模型 API。

v2 是首次失败后的运维修订，v2 首请求前登记在 [LIVE_SMOKE_PLAN.md](LIVE_SMOKE_PLAN.md)：串行执行，另建 campaign，从原 train ceiling 扣掉 v1 花费。v2 限额为 31 请求/198748 tokens/19 评分/878.78 活跃秒；暂停时间不计活跃预算。两 manifest 的 11 个研究源码 pins 一致，当前工作区研究源码 hash 也匹配；其他离线恢复器 CI 修复不属于这些 pins。原失败保留并阻断，没有重置 v1 冒充连续成功。

## 评分、有效性与 checkpoint

两轮 dev baseline RMSE 均为 **0.8467093297076379**，utility 均为 **0.5415037352729004**。每轮 3 次 attempt 全部执行完毕，但其中 2 次以 budget_exhausted 结束、不可作为有效候选；仅 b0_a1 以 end_turn 结束并有效，分数与 baseline 相同。两轮状态均为 completed_with_invalid_attempts，invalid_attempts=2、valid_attempts=1；因包含无效节点，两轮 optimizer trace 均为 null。

同分按规则保留 baseline，两个 checkpoint 的代码 SHA256 均为 `55e413e3d1ffd256ad16ee8e4183edb19da23deafa027e84cd1eaa80cecf6a0a`，与原均值脚本一致。checkpoint 代码与对应 result hash 已独立核对。这里的“两轮完成”是持久流程完成，不等于模型给出两个改进方案。

独立 final 的唯一 final_finished 事件为 valid=true，**RMSE=0.7554124334834056**，utility=0.569666695373474；final 预测 SHA256 为 `b04d7fbe2d513f40af54ee2f1170cf751da1fa6979ff8af1002680ba306b3e38`，已对实际文件校验。最终代码仍是 baseline；dev/final 属于不同样本，二者数值差异不能写成提升。

## Mem0、观测与 final 边界

v2 首轮为空库：embedding_calls=1、valid_semantic_candidates=0、lexical_fallback=true，不称成功语义召回。第二轮真实本地 CPU Mem0（bge-small-zh-v1.5 固定 revision、CPUExecutionProvider）记录 embedding_calls=3、有效候选=1、fallback=false、semantic_status=verified_candidates、closed=true。此为当前单条已提交经验的功能执行证据，没有召回质量或端到端收益对照。sdk_status 是关闭前快照，最终清理以外层 closed=true 为准。

local calls 对 v1 为 1 start/1 end、v2 为 24 start/24 end，call ID 一一覆盖，均 status=ok；每个 end 的 tokens 与 journal settled 记录逐个相同。v2 两次 observation_closed 均 local_closed=true、dropped=0、export_errors=0；两次 provider_clients_closed=true。cloud_upload 始终 unverified、cloud_closed=null，因此没有 Langfuse 云上传结论。final 是纯 evaluator 路径，不产生模型调用。

experience 恰有两条事实，其 key 均由各自 committed dev payload 重建并匹配；未含 final RMSE。final 方法没有记忆/模型/控制器反馈路径，final_finished 后 journal 无新事件，未开始下一阶段。结合源码与落盘证据，当前流程未把 final 用于经验或后续选参；未伪造不存在的运行前后经验快照。

## 收敛与未完成项

用户要求一天内形成可写简历的工程交付，因此只完成 red。Airfoil 策略 holdout、white 冻结策略测试、fixed/random/RSI 同预算正式三组未启动。缺少合法且完整的策略 train+holdout 数据；red 两轮也没有完整 optimizer trace。CLI optimize 返回 blocked、promoted=false，原因是需要显式独立 train/holdout task dataset；该命令证据是 [operator-evidence.json](operator-evidence.json) 中工具结果转录，**不是 journal 封印的优化实验**，没有 optimizer_started/finished 事件。

可以写“真实流程完成两轮、恢复与独立 final，可信评分和预算证据可复查”；不能写 RSI 学到新策略、Mem0 提升质量、模型超越 baseline、科学对照完成、官方长程 benchmark 成绩或全部候选成功。个人职责仍须本人确认，项目事实不自动等于独立手写。
