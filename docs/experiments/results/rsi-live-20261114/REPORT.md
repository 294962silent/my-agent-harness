# 真实RSI短代码校准：48槽终态与效果边界

2026-11-14，本机执行，模型`deepseek-v4.1-flash`，实际OpenCode Go兼容端点。**48个测试槽全部到终态：47个正常交付且独立final通过，1个权限失败、final未执行。** 新续跑37槽全部完成，原失败未重跑。真实分支→评分树→实际SDK搜索→冻结策略消费已串通；本轮没有形成新的学习干预，不能证明RSI收益或长程能力。

## 固定任务与条件

- 官方HumanEval commit `b9453e8f1865df0a0057a1383767caa400acfbcf`，data SHA256 `b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef`，MIT许可同目录保存。准备器v3的11题empty/gold门控与隔离边界见[开始快照](start-v2.json)。
- train：7/11/27；val：32/45；test：1/6/9/20/33/40。test为6题×2个调度seed×4臂，12槽/臂；调度seed23/47不是API确定性seed，独立问题只有6个。
- A：基础上下文+固定2/1；B：layered/产物增强+同固定2/1；C：增强+冻结研究条件；random：增强+seeded随机探索。C实际为预先登记的reference fallback，与B同参数。
- 统一attempt12轮，episode父上限64请求、1M reservation tokens、1800秒、6评估；输出4096、HTTP180秒、零重试。32k客户端上下文预算，采样temperature0.7；服务商权重revision不可得。
- 公开例用于分支选择，独立final只在选定副本上执行、反馈不进入选择/训练。host checker与candidate worker分进程，typed JSON传结果，目录隔离仍不是OS安全沙箱。

## 原分母下的描述性结果

以下是实际计数，不做排名、显著性检验或学习效果估计。每臂均包含全部12个预注册槽。

- **A**：正常交付12；final通过12、失败0、未执行0；221请求，输入655713、输出45459，合计701172tokens。
- **B**：正常交付12；final通过12、失败0、未执行0；231请求，输入630594、输出39643，合计670237tokens。
- **C（固定reference）**：正常交付11、权限失败1；final通过11、失败0、未执行1；214请求，输入587388、输出40602，合计627990tokens。包含失败槽的10请求22670tokens，未删除失败成本。
- **random**：正常交付12；final通过12、失败0、未执行0；219请求，输入590162、输出39672，合计629834tokens。

这些是多次尝试、独立可见评分后选定产物的结果，不是HumanEval pass@1或官方榜单成绩。C的少一次交付不能解释成RSI质量下降，其较少token也不能解释成学习节费。

## 为什么没有RSI学习结论

5个collection共84请求237239tokens，完整交付4/5。val32达到12轮上限，虽然可见分数1，但没有合法trace、selected或final；完整验证覆盖不足。原导出失败和全5题分母保留，首test前登记available-tree探索性恢复，只用全部3个train选唯一候选，partial holdout不参与排序或重选，未生产晋升。

3棵train树各4节点，12个可见分数全1；5策略×3树×预算1/2/4的45个train回放叶best全1。当前质量目标没有奖励少用action，按fixed-reference优先平局规则选baseline。SDK baseline不是起始stub的0分；tree action也不是API请求。见[训练诊断](../../../../experiments/context_rsi/reports/train-tree-diagnostic-20261114.json)。

raw SDK baseline/parameters=null/hash `f46ca7adbe15280bc7ec375255bee72fd0d5f55cb237e672ce05b8918d6aecc6`；effective在线reference2/1/hash `33030146a9b9f6f49b71087c77834e0f0542611ebf881ceb2d4c5370e996adb8`。两种身份分开，publication为`validation_incomplete_rejected_for_publication`，promoted=false，learning_contribution=`not_identifiable`。这不是“0%学习提升”。

## 执行完整，比较证据仍有缺口

原`test-HumanEval-6-s23-C`在第三attempt的新请求前发生PermissionError，报告只存类型，没有路径/WinError。前两个attempt正常end_turn、10请求全部结算；失败final保持null。调查未定位根因，离线复制可行，不能声称已修复ACL。

原停止文字把所有blocked统称unknown，实际当时298请求全部settled。核journal后已更正；这批最终也没有unknown。另登记一次[精确运维例外](../../../rsi-live-continuation.md)，封印原634行prefix、唯一failed及原37未启动槽，只续这些槽，旧失败不改状态、不重跑或补final。原summary依旧blocked，独立operational gate最终剩余0；两者不是同一状态。

第三attempt没有发送模型请求，工具schema指纹为null。严格同schema门禁在6-seed23组触发`schema_mismatch`，这里是**证据缺失，不是观察到工具定义改变**；其余组未触发该门禁。主分析器以显式`--allow-incomplete`输出完整coverage与原始数值，comparisons不生成排名。`test.complete=true`与`comparisons.status=schema_mismatch`分别报告执行覆盖和比较资格，不篡改null以求通过。

## 全部准备成本与缓存字段

- test48：885请求，输入2463857、输出165376，合计2629233tokens。
- collection5：84请求，237239tokens；v1四轮先导：4请求8496tokens；独立连通：1请求55tokens。
- **本批总量：974请求，输入2695287、输出179736，合计2875023tokens。** 全部settled、0pending/unknown/held；实际SDK为48个离线回放评估叶、合计82次tree action，均不算API调用。
- v2自身为969请求2866472tokens。旧533请求账本及已知$1.218608/未知预留$0.045131保持原样；本批币价未核，currency/cost=null，不计算完整美元费用。20亿token额度为用户声明，未核供应商余额。

test的cache_read字段覆盖为A220/221、B230/231、C213/214、random219/219。已知read sum分别586624、555904、517888、521984；前三臂完整命中率保持null。random字段完整，API报告的read/input为0.8844757880；不与缺失字段的其他臂排名，不证明缓存提升。整个v2覆盖964/969、已知read2378240；不把缺字段补0，未做DSH对照。

## 验证、复核与下一步

- 主代理真实执行最终analyzer与continuation披露CLI均exit0：48/48 test终态、37/37续槽终态，原failed保留；所有53槽均有sealed终态。
- 独立[运行审计](runtime-audit-final.json)核26项：原prefix/failed bytes、严格一次续槽、用量、117份原执行源码、新wrapper、任务/策略封印及所有result/final链均通过；相关owned进程匹配0，cleanup_errors全空。
- 分组与实际触发指标见[独立指标](metrics-detail-final.json)；summary purpose请求、artifact_read调用和snapshot-derived micro/fold视图分别计量，缺失观察保持unavailable。
- v2自身969次provider请求purpose均为agent，summary请求0；artifact_read/search recorded starts均0，成功回读次数不可由该元数据判定。phase-fold snapshot-derived views为B24/C22/random24，micro snapshot groups为B0/C2/random1，A均0；记录到投影视图不等于测得操作次数或语义无损。
- 新controller/原runner/analyzer本机组合91 passed；独立披露工具10 passed，批次不混成模型成功率。源码及辅助工具head6488442四组CI和SDK专项已核成功，最终文档head检查另见PR。
- 下一批先在独立train先导确认评分区分度，预注册质量与效率目标、补齐完整holdout，并分别记录configured和实际发送的schema。当前test不用于重选策略或修改目标；长程收益仍需适配容器任务后的独立评测。

本批沿用固定Dream SDK的受控参数路径和已有分层上下文/Session实现，新增真实执行审计及精确续跑协议；没有恢复DS/DSH。实际问题、取舍与面试追问见[P52–P58](../../../interview/12-真实RSI实验的数据与评分可信度.md)。

原始prompt、候选、官方final与本机凭据不入Git；本地原档保留。安全[完整53槽分析](../../../../experiments/context_rsi/reports/live-v2-analysis-final-20261114-r1/analysis.json)和[最终续跑披露](../../../../experiments/context_rsi/reports/live-v2-continuation-final-20261114-r1/continuation.json)附各自SHA256，支持复核公开聚合；完整原始执行需固定数据和对应环境。
