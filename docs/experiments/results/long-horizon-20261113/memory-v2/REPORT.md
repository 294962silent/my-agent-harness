# LongMemEval 固定历史索引三臂实验

日期：2026-11-13。30题、三臂各一次回答和一次判分全部执行完毕，180次请求均已结算。**当前配置的 Mem0 没有优于词法检索。** 这是英文历史与中文 BGE 模型的小样本派生评估，不是 Mem0 的普遍能力排名，也不是完整长程 Agent 成绩。

## 条件与来源

全部执行在本机 WSL Ubuntu-20.04，embedding 使用 CPU；回答和判分调用既有 DeepSeek V4.1 Flash 兼容 API。未迁移 L20。生成源码为 `16bdae0e4ec7fb8511ea8bd86d52e26d84d04c18`，计划 SHA256 `6d946de38daf968f4402fd4578e84d5833531bfdf815b5397581e2721be4129f`。123份实际源码/uv.lock补充证明见[预检清单](../preflight/execution-source.json)。

数据使用 `xiaowu0162/longmemeval-cleaned` 原英文 S 历史，LFS SHA256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。六类型各5题、seed23，30个ID在付费前冻结；它是分层平衡样本，不能直接与完整官方数据集或榜单比较。

none 是不提供历史的基线；lexical 使用生产 MemoryStore 词法检索；mem0 使用生产 Mem0Index、本地 CPU BGE-small-zh-v1.5。两检索臂索引同一份完整历史分块、top8、最多6000字符。没有 query expansion、LLM 抽取、自动巩固或语义故障后的词法替代。SID同规则匿名，gold、证据标签和原SID映射仅host评分端可见。旧的0API标签泄漏探针被排除。

回答与判分均为一次调用、不重试；judge复用官方prompt，但模型替换为DeepSeek，因此标为非官方judge。观察点在SDK前输入哈希，不声称抓取到wire请求或验证服务端权重。

## 回答质量与覆盖

- none：4/30正确，13.33%；判分覆盖30/30。
- lexical：22/29正确，75.86%；判分覆盖29/30，另1条unavailable，pending=0。
- mem0：15/30正确，50.00%；判分覆盖30/30。

三臂共同有效的29题中，lexical为22/29，mem0为15/29，none为4/29；mem0相对lexical的正确率差为 -7/29，即 **-24.14个百分点**。这是本批已观察差值，不是总体因果增益或显著性证明。各题仅一次回答和一次judge，未测不同随机种子的稳定性。

4道无证据弃答题三臂均答对；none的4个正确全部来自这部分。其余有证据题分别为none 0/26、lexical 18/25（另1不可用）、mem0 11/26。偏好类尤其薄弱：lexical 0/4有效（另1不可用）、mem0 0/5；不能用整体平均遮盖该问题。

唯一不可用判分是 `75f70248 / lexical`：回答已完成，judge返回空正文、finish_reason=length、输出上限1024，记录invalid_judge_format。该次API用量正常结算，但没有合法yes/no，所以不计对错、不按错误补零、不从推理文本猜分，也没有追加请求。若只做缺失结果敏感性边界，词法组全30题正确率为22/30至23/30，即73.33%–76.67%；正式值仍是22/29并同时报覆盖。

## 检索与资源代价

检索90槽位全部valid、资源close_complete，语义错误没有悄悄变成词法成功。4道弃答题不进入证据召回分母，剩余26题：lexical平均session Recall@8=0.86538、MRR=0.80449；mem0为0.73077、0.61255。指标按实际发送上下文中的去重session计算，不是向量库返回候选但未发送的命中。

平均总检索阶段耗时为lexical 4.32秒、mem0 99.13秒，**均包含各题首次建库，不能叫纯查询延迟或计算在线加速倍数**。离线index等待1800秒、close60秒；本机产品默认工作等待10秒。本批没有测warm查询、在线冷启动SLA、峰值内存或吞吐。

中文BGE处理英文历史、chunk粒度、向量排序和reader综合能力都可能影响结果；本批无法将落后唯一归因于某一项。下一轮若比较多语模型或混合召回，应另冻模型/协议和预算，不覆盖本次结果，也不据此自动调整产品默认值。

## 用量、审计与复算

本批180次调用：客户端保守全冷参考记账49191微美元，即 **$0.049191**；全项目由300请求/$0.888355变为480请求/$0.937546，未知/在途用量均0。上限仍是原$30，不重置历史；费用不是供应商账单。

供应商usage原字段合计prompt_tokens=95175、completion_tokens=17067、total_tokens=112498。有1条请求将256个cached输入另列，故全输入按total-completion为95431，不能简单把95175当全部输入。独立按每请求向上取整的 `$0.30/M × 全输入 + $1.20/M × 输出` 重算，得到相同49191微美元。cache_read仅1/180请求有有效值，cache_write为0/180，**不报告完整任务缓存命中率**。

- [summary.json](summary.json)：生产汇总原bytes，明确各臂available/unavailable。
- [rows.json](rows.json)：90条安全逐题记录、检索/输入/回答/判分关联哈希与分段时间，不含历史和答案正文。
- [metrics.json](metrics.json)：分类型、检索、完整三臂配对及预算汇总。
- [requests.json](requests.json)：180条用量和成本，未包含凭据或prompt。
- [audit.json](audit.json)：独立stdlib核验输入重建、判分格式、结果/账本唯一对应与汇总的结果。
- [manifest.json](manifest.json)：绑定以上5份机器可读文件，不含本说明。

可以从rows.json按arm重算 `correct=true数 / correct非null数`，从三臂correct均非null的题目交集重算配对差。判分前输入哈希和完整历史的核验依赖本机私有原始产物；公开元数据可复算统计，不能独立重判未公开的答案。数据源、源码和采样ID已记录，可另行复现实验，但真实模型再调用不保证逐字一致。
