# AIDE 固定源码审查

对象：`WecoAI/aideml@ad73d227af481bea3de06f884f41b2993dd6453a`，只读 README、agent、journal；未安装、运行或调用模型。仓库公开、未归档；README 标注 MIT，许可正文未核。

## 已核

循环是选择节点→生成完整 Python→执行回调→反馈模型分析→追加 Journal。先生成指定数量的根草稿，再按概率调试有限深度的失败叶节点，否则贪心改进最佳有效节点；并非每步生成五份草稿。改进提示要求一次原子修改。生成解析最多尝试三次，后端重试另计，调用预算须覆盖全部阶段。[1][2]

奖励是可比较的连续验证指标，但数值和方向由反馈模型读取代码及终端输出后返回；异常、缺指标或模型判错会标记失败。holdout、交叉验证、输出预测主要由提示要求，三文件未见宿主重算、密封测试集或强制划分，反复选择同一验证集仍可能过拟合。[3]

Journal 保存代码、父子关系、执行结果、分析和指标，统一方向排名；摘要只含成功节点。可序列化对象、可传入旧 Journal，以及 README 的日志产物，均不足以证明断点恢复、崩溃安全或请求去重。[4]

README 记录房价五步真实 API 测试契约，要求至少两个有效方案及一次改进尝试，不保证提升；未复跑或核查 CI。[5]

## 推断：适配价值与边界

适合借鉴为小型表格 ML 实验闭环。CPU／8GB 上，远程代码与反馈模型、简单 sklearn 模型、串行及样本／线程／时限约束可能可行；这不是实测，也不能推广至本地大模型或深度训练。[6]

最小接缝：宿主固定数据版本、划分和种子，从预测重算指标，选优后才用最终测试集；Mem0 存含失败的已验证经验及数据／代码哈希；Langfuse 观察节点、调用用量及产物；Dream-RSI 只在统一预算内选择草稿／调试／改进／停止，与贪心基线公平比较。三者均不承担可靠恢复。

## 未核与不宜照搬

完整依赖、解释器隔离、持久化恢复、资源峰值、许可证正文未核。已见 Python≥3.10、SDK 版本要求及 humanize／dataclasses-json 使用。不要照搬包均已安装的提示、模型自报分数、验证集反复选优或论文效果数字；复用代码前核正文并保留许可声明。[7]

## 固定版本证据

1. [搜索策略与执行循环：agent.py L161–192](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L161-L192)；[执行及追加 L375–393](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L375-L393)。
2. [生成重试 L253–268](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L253-L268)；[原子改进 L306–338](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L306-L338)；[后端重试文档 README L179–189](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L179-L189)。
3. [验证要求 agent.py L218–239](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L218-L239)；[反馈评分 L395–440](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L395-L440)。
4. [节点字段 journal.py L24–81](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/journal.py#L24-L81)；[方向、排名和摘要 L138–239](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/journal.py#L138-L239)；[日志产物 README L86–89](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L86-L89)。
5. [真实测试的文档契约 README L260–296](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L260-L296)。
6. [本地模型配置 README L224–239](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L224-L239)；[环境包提示 agent.py L194–215](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L194-L215)。
7. [Python／MIT 标识 README L9–11](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L9-L11)；[SDK 要求 L179–184](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/README.md#L179-L184)；[源码依赖 agent.py L1–14](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/agent.py#L1-L14)；[journal.py L1–22](https://github.com/WecoAI/aideml/blob/ad73d227af481bea3de06f884f41b2993dd6453a/aide/journal.py#L1-L22)。
