# v2 终止态：仅覆盖率与已知成本

本目录是运行停止后的只读快照，保留全部 **53 个预注册槽：5 个 collection、48 个 test**。没有重跑、恢复或重新选择策略；37 个尚未启动的 test 槽仍在分母中。对应数据为 `analysis.json`，校验文件为 `analysis.sha256`。

test 的 48 个槽中，11 个已有宿主终态：10 个 completed、1 个 failed；另有 37 个 planned。各臂均计划 12 槽：A 已完成 2；B 已完成 3；C 已完成 2、失败 1；random 已完成 3。合法 final 结果共 10 个；未执行 final 的失败槽和未启动槽保持 unavailable，没有补成质量分数 0。数据未齐，配对统计、排名及增益结论均不计算。

**更正停止消息的误导表述：** `new unknown usage stops campaign` 是通用停止文字，不能据此断定用量 unknown。实际 v2 journal 的 **298 个请求全部 settled，pending=0、unknown=0、held tokens=0**。失败槽为 `test-HumanEval-6-s23-C`，已审查为宿主 `PermissionError`；其失败状态和 10 请求、22,670 tokens 成本保留，final 保持未测。

本批可核对的 provider 用量分别为：collection 5 题 **84 请求、237,239 tokens**；test 已启动部分 **214 请求、632,966 tokens**；v1 预算校准 **4 请求、8,496 tokens**；独立连通检查 **1 请求、55 tokens**。合计已观察到 **303 请求、878,756 tokens**，其中 input 829,021、output 49,735。v2 本身为 298 请求、870,205 tokens。尚未运行槽的未来成本未知，`full_total` 保持 null；货币、价格与金额保持 null。SDK 恢复是离线已评分树回放，不计作 LLM 请求。

C 的 SDK 原始选择为 baseline、parameters=null；在线显式 fallback 为 `{max_width: 2, max_refine: 1}`，与 B 同参数。两种身份及哈希分列，学习贡献为 **not_identifiable**，不能称作 RSI 提升或“0% 学习提升”。验证覆盖仍为 1/2，完整 validation gain=null，生产发布被拒绝。

分析器核验 JSON 重复键、journal 哈希链、53 槽身份、预注册时序、策略与源码指纹、终态→final→result 封印、provider 用量与逐槽账本。input 以合法 `total_tokens-completion_tokens` 为准；仅缺 total 且四个用量组件显式齐全时才求和。cache 缺字段不补零，完整缓存命中率仅在字段覆盖完整时可得。工具定义 hash 不一致或不可得时禁止配对。Context activity 是快照所见 views/groups，不是操作次数。

报告只含哈希、计数、类型、状态及数值用量；不含原始 prompt、response、candidate 代码、任务答案或官方 final 测试内容。分析过程不读取这些源材料，不构造模型客户端，不写 journal。

边界：这是本地受信宿主证据的描述性审计，不是独立账单、远端执行真实性证明或 OS 沙箱证明；哈希也不能抵抗宿主同时重写全部材料。6 题各 2 次调度重复不是 12 个独立题，不做 p 值、显著性或长期工程效果泛化。continuation 尚未在本报告中执行；若另行授权，必须保留本次失败与原始成本并披露协议修订。

## 训练树为何选中 SDK baseline

仅核 train 数值：HumanEval/7、11、27 的起始 stub 分数均为 0；各树四个记录节点 `b0_a0、b1_a0、b0_a1、b1_a1` 均为 1，共 12/12。**SDK baseline 是完整树参考策略，不是这个 0 分 stub。** 五种策略×三棵树×预算 1/2/4 的 45 个 train 回放叶 best 全为 1，五者 train score 均为 1，因此固定参考优先的 tie rule 选中 baseline。不能解释为候选更贵：c00 实耗 action 为 1/1/1，baseline 为 1/2/4，c01–03 为 1/2/2，但目标未奖励 action 节省。当前可见树第一 probe 即饱和，分数目标没有区分度；不证明真实模型没有提升空间。action 也不等于真实 API/token 成本。精确数值和来源哈希见[独立诊断 JSON](../train-tree-diagnostic-20261114.json)。本次无 API、无 SDK 重放、未分析题面/代码/gold/final，未修改已选策略或利用 test 重调参数。
