# LoCoMo four-arm memory study v2

执行日期为 2026-11-15（Asia/Shanghai），对应 UTC 2026-11-14。本批创建了新的
`locomo-multisession-four-arm-v2` 协议，没有修改或重放 v1 的失败请求。v2 将共享
事实抽取的输出上限从 2,048 提高到 8,192；题目、原始数据、四臂、top-8、6,000 字符
上下文和 judge 规则均保持不变。

## 冻结范围

| 项目 | 冻结值 |
| --- | --- |
| 题目 | dev 6、test 24 |
| sessions | 92 |
| 指令四臂 | `raw_lexical`、`raw_mem0`（raw + Qwen semantic）、`facts_lexical`、`facts_mem0`（native fact + Qwen semantic） |
| 模型 | DeepSeek V4.1 Flash，retry 0；Qwen3-Embedding 0.6B revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| 时间模型 | 最长 session 1,710 tokens；embedding 上限 4,096；WSL RTX5060 GPU 0，无凭据 seccomp worker |
| 计划调用 | 92 extraction + 120 answer + 120 judge = 332 |
| 门禁 | 384 requests、5,000,000 accounting units、每 split 8 小时 |
| 输出上限 | extraction 8,192；answer 1,024；judge 512 |

冻结 plan SHA256 为
`b0e9edc48fffb75b0ca69c64bcf36813eed86a8a5e3ad9ab287e2119d3a7368e`，plan
manifest SHA256 为
`d80b9cd89841bd3bde6af85118f48a16c278a9b01728005b904f2f0f1883b3b0`。数据 SHA256
为 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，gold SHA256
仍为 `3c91283cc2fda4bb21ce5c363712f50c6363d9f2be0fffee0734c629308e0abf`。

## 真实执行

dev 进程完成两份独立 Mem0/Qwen 冷索引后，派出两次共享事实抽取：

| 调用 | finish | facts | prompt | completion | cache read | provider total | 请求耗时 (s) | 保守成本 (USD) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `stop` | 17 | 9,303 | 2,178 | 0 | 11,481 | 28.633 | 0.005405 |
| 2 | `stop` | 10 | 1,586 | 5,259 | 7,680 | 14,525 | 60.302 | 0.009091 |

两笔都通过 8,192 上限约束并在上限前正常结束，说明 v1 的 2,048 截断问题在本批
前两次调用中消失。第二笔 `total_tokens` 含 7,680 cache-read tokens，因此不能把
`prompt + completion` 与 provider total 混用。两笔合计 prompt 10,889、completion
7,437、cache read 7,680、provider total 26,006，保守账本成本 0.014496 USD；供应商
实际账单仍为 unavailable。

从 dev conversation 开始到停止的 wall time 为 336.830356 秒，其中两笔真实请求的
ledger 时间跨度为 89.814669 秒；差值主要是两份 CUDA embedding 索引冷启动。

## 停止原因

协议要求前两笔正常抽取分别做一次 Langfuse 云端 metadata/usage 回读，且任一回读
不匹配立即停止。两笔模型结果均为正常 `stop` 并已结算，但云端回读门禁未形成验证收据；
进程记录 `LLMError`，自动重试为 false。ledger 随即封锁 campaign，没有第三笔抽取、
answer、judge 或 test 调用。

本批没有在失败后再次查询云端，也没有放松为轮询或多重回读。因此只能确认“单次回读
门禁没有匹配并按协议停止”，不能从当前公开证据区分远端暂未可见、字段不匹配或其它
单次查询失败原因，也不能宣称两笔 trace 已通过云端验收。

## 四臂结果

| arm | 已实现分母 | 正确数 | 计划分母 | paired difference | session recall@8 | session MRR |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| `raw_lexical` | 0 | unavailable | 30 | unavailable | unavailable | unavailable |
| `raw_mem0` | 0 | unavailable | 30 | unavailable | unavailable | unavailable |
| `facts_lexical` | 0 | unavailable | 30 | unavailable | unavailable | unavailable |
| `facts_mem0` | 0 | unavailable | 30 | unavailable | unavailable | unavailable |

题目没有进入 answer/judge，因此逐题结果为 `not_dispatched`，不是错误、0 分或拒答。
[question-outcomes.jsonl](question-outcomes.jsonl) 保留 30 题 × 4 臂 = 120 个未派发
记录；[rows.jsonl](rows.jsonl) 是空文件，因为有效 answer rows 为 0。

计划分层为三类各 10 题：multi-hop、temporal、single-hop。跨 session 分组为
single-session 20 题、cross-session 10 题；按唯一 evidence session 数，计划分布为
1/2/3/4/5/7 个 session 分别 20/6/1/1/1/1 题。由于没有 answer/judge，这些分层的
正确率、session recall@8 与 MRR 都保持 unavailable。

## v1 保留

v1 已完成的 1 请求 / prompt 9,303 / completion 2,048 / total 11,351 / 保守
0.005249 USD 失败及其 Langfuse 回读收据均未改写或重放。相关原文件 SHA256 记录在
[v1-preservation.json](v1-preservation.json)。旧 LongMemEval 结果也保持不变：
none 4/12、lexical 11/12、Mem0 9/12。

## 验证与边界

冻结前定向回归为 51 passed，Ruff 全部通过。冻结后只运行冻结源并核验 plan/data/gold/
model 哈希。测试覆盖 8,192 安全上限、两笔首抽每 trace 一次回读、缺少云端结果即停止、
v2 预算与协议冻结，以及原有四臂/ledger/fail-closed 契约。

本批不是四臂效果比较，也不能回答 Mem0 或 fact extraction 是否优于词法。没有有效
题目结果、配对差、类别准确率、跨 session 准确率或 session recall/MRR；没有 provider
实际账单。公开结果不含题目正文、history、gold、模型正文、缓存或凭据。
