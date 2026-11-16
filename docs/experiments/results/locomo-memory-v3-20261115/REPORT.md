# LoCoMo four-arm memory study v3

执行日期为 2026-11-15（Asia/Shanghai），对应 UTC 2026-11-14。本批在独立
`locomo-multisession-four-arm-v3` 协议中完成 30 道原题、92 个唯一 session、四个固定检索臂的
真实回答与盲判。v3 初跑保留在 8,192-token 抽取上限处；恢复批次只提高抽取输出上限和请求
窗口，没有改题、数据、四臂定义、top-8、6,000 字符上下文或 judge prompt。

## 面试摘要

这次实验回答的是：在相同原题、相同历史、相同答案模型和相同 judge 下，原生 Mem0 向量检索、
抽取后的 facts 检索，与各自的词法对照相比，是否提高正确率、跨 session 召回和 MRR。

总体正确数（固定 30 题分母；`length` judge 按冻结规则计错）为：

| arm | correct / denominator | accuracy | valid judges | valid-only correct rate | session recall@8 | session MRR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` | **16 / 30** | **53.33%** | 30 / 30 | 53.33% | 0.8299 | 0.7837 |
| `raw_mem0` | **17 / 30** | **56.67%** | 28 / 30 | 60.71% | 0.8125 | 0.7722 |
| `facts_lexical` | **12 / 30** | **40.00%** | 25 / 30 | 48.00% | 0.8913 | 0.7678 |
| `facts_mem0` | **15 / 30** | **50.00%** | 25 / 30 | 60.00% | **0.9140** | **0.8689** |

最重要的观察是：Mem0 在这批数据上没有形成稳定的整体优势。`raw_mem0` 只比
`raw_lexical` 多 1 题，`facts_mem0` 比 `facts_lexical` 多 3 题，但 12 个 judge 达到 512-token
上限；因此不能把四臂排序解释为纯检索质量差异。`facts_mem0` 的 session-level recall/MRR
最高，却在 10 道 multi-hop 题中只答对 1 题，说明“找到来源 session”与“抽取出的 fact 足够回答”
不能混为一谈。

## 冻结设计

| 项目 | 冻结值 |
| --- | --- |
| 题目 | dev 6、test 24；原始题文与 evidence binding 不变 |
| 唯一 sessions | 92；实际按 6,000 字符文档边界产生 94 个 extract documents |
| 四臂 | `raw_lexical`、`raw_mem0`、`facts_lexical`、`facts_mem0` |
| 检索 | top-8、BM25 或本地 Qwen3-Embedding 0.6B、6,000 字符上下文 |
| 回答模型 | DeepSeek V4.1 Flash，temperature 0；请求自动重试 0 |
| judge | 固定非官方 DeepSeek prompt；只接受单个 JSON boolean；异常/长度保留并计错 |
| 抽取上限 | 32,768 completion tokens；answer 1,024；judge 512 |
| 请求窗口 | 模型调用 600 秒；只读 Langfuse 回读总窗口 180 秒 |
| 冻结预算 | 384 requests、5,000,000 accounting units、每 split 8 小时 |
| 用户总授权 | 20 亿 token；本实验实际累计约 146 万 tokens |

恢复批次 plan SHA256 为
`716d25917f028793ed4a4f1263dc5aa03287fa7b0ceb6973eb975ad8a1c6bc1b`，plan manifest
SHA256 为 `1d55c514bf8dd5ea74c1d9e2b183a8085edcf470a0dc509fb5fa45e2c50519ab`。
开源数据 SHA256 为 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，
gold SHA256 为 `3c91283cc2fda4bb21ce5c363712f50c6363d9f2be0fffee0734c629308e0abf`，
Qwen3-Embedding revision 为 `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`。

## dev 与 test

| split | arm | correct / expected | accuracy | valid judges |
| --- | --- | ---: | ---: | ---: |
| dev | `raw_lexical` | 4 / 6 | 66.67% | 6 / 6 |
| dev | `raw_mem0` | 5 / 6 | 83.33% | 6 / 6 |
| dev | `facts_lexical` | 1 / 6 | 16.67% | 5 / 6 |
| dev | `facts_mem0` | 2 / 6 | 33.33% | 4 / 6 |
| test | `raw_lexical` | 12 / 24 | 50.00% | 24 / 24 |
| test | `raw_mem0` | 12 / 24 | 50.00% | 22 / 24 |
| test | `facts_lexical` | 11 / 24 | 45.83% | 20 / 24 |
| test | `facts_mem0` | 13 / 24 | 54.17% | 21 / 24 |

dev 的 6 题只用于桥接和协议检查，不据 `raw_mem0` 的 5/6 再做选题、替换或重跑；test 仍按
冻结的 24 题全部执行。test 中 `facts_mem0` 为 13/24，是四臂最高；`facts_lexical` 为 11/24。

## 类别与跨 session

| subgroup | `raw_lexical` | `raw_mem0` | `facts_lexical` | `facts_mem0` |
| --- | ---: | ---: | ---: | ---: |
| multi-hop (n=10) | 2 | 3 | 0 | 1 |
| temporal (n=10) | 6 | 7 | 7 | 7 |
| single-hop (n=10) | 8 | 7 | 5 | 7 |
| single-session (n=20) | 14 | 14 | 12 | 14 |
| cross-session (n=10) | 2 | 3 | 0 | 1 |
| 1 evidence session (n=20) | 14 | 14 | 12 | 14 |
| 2 evidence sessions (n=6) | 2 | 2 | 0 | 1 |
| 3+ evidence sessions (n=4) | 0 | 1 | 0 | 0 |

四个臂在 single-hop 与 temporal 上差距较小；主要分化出现在 multi-hop/跨 session。
`facts_mem0` 的 session-level 排名最高，但它在 multi-hop 只有 1/10，提示后续应把“来源召回”
和“事实充分性”拆成两个指标，而不是只优化向量相似度。

## Paired comparisons

下表把 `length` judge 当作冻结评分规则下的错误；`paired delta` 是左臂减右臂的净正确数。

| comparison | left only | right only | both correct | both incorrect | paired delta | McNemar exact p |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` vs `raw_mem0` | 2 | 3 | 14 | 11 | -1 | 1.000 |
| `facts_lexical` vs `facts_mem0` | 1 | 4 | 11 | 14 | -3 | 0.375 |
| `raw_lexical` vs `facts_lexical` | 5 | 1 | 11 | 13 | +4 | 0.219 |
| `raw_mem0` vs `facts_mem0` | 4 | 2 | 13 | 11 | +2 | 0.688 |
| `raw_lexical` vs `facts_mem0` | 3 | 2 | 13 | 12 | +1 | 1.000 |
| `raw_mem0` vs `facts_lexical` | 7 | 2 | 10 | 11 | +5 | 0.180 |

这些配对差来自 30 题 pilot，p 值均未达到常规 0.05 门槛，也没有多重比较校正。它们能说明哪些
case 值得追查，不能支持“某个生产记忆系统整体更优”的结论。

## 检索、延迟和 token

| arm | mean retrieval s | mean answer s | mean judge s | answer total tokens | judge total tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` | 0.0204 | 5.0273 | 4.5238 | 55,218 | 10,690 |
| `raw_mem0` | 0.1304 | 5.3314 | 4.8261 | 54,837 | 11,225 |
| `facts_lexical` | 0.0366 | 4.2623 | 5.0865 | 20,186 | 12,045 |
| `facts_mem0` | 0.0573 | 4.3400 | 4.9225 | 21,966 | 11,874 |

检索时间只覆盖已建立索引后的查询；每个 conversation 的冷启动和事实索引时间见源 safe files。
四个臂的回答与 judge prompt 不同，因此上表是系统端到端成本，不是单次向量查询 benchmark。

| stage | requests | prompt | completion | cache read | provider total | summed latency s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| extraction | 94 | 210,593 | 183,697 | 727,808 | **1,122,098** | 2,114.084 |
| answer | 120 | 133,753 | 18,198 | 256 | **152,207** | 513.496 |
| judge | 120 | 26,559 | 19,275 | 0 | **45,834** | 526.631 |
| v3 total | **334** | **370,905** | **221,170** | **728,064** | **1,320,139** | **3,154.210** |

provider total 不能由 prompt + completion 代替；这里包含 cache-read tokens。v3 的保守账本成本为
`$0.595242`。连同保留的 v1、v2、v3 初跑，累计账本为 345 requests、
prompt 412,183、completion 256,086、cache read 791,680、provider total 1,459,949，
保守累计成本 `$0.668615`。供应商实际账单仍为 unavailable。

`conv-47` 从冷索引开始到 dev 完成的 wall time 为 1,153.818 秒；test 从 `conv-41` 开始到
最后一笔判分为 2,983.688 秒；整批从首个 conversation 开始到收尾异常为 4,150.528 秒。
冻结 ledger 的首末模型请求跨度为 3,910.827 秒。

## 观测、失败和真实性边界

- v3 初跑前两笔抽取均正常 `stop`，但第 8 笔抽取在 8,192 completion tokens 处 `length`，正文为空；
  该 8 请求 / 102,453 tokens 批次原样保留，未重放。
- recovery 首两笔抽取通过 180 秒有界指数轮询：第 1 个 trace 查询 1 次，第 2 个查询 4 次，
  等待 1/2/4 秒，13.4118 秒后两者各只有一个匹配 observation；额外模型请求为 0。
- 120 个 answer 全部为 `valid`。judge 为 108 `valid`、12 `length`、0 `invalid`、0 unavailable；
  `length` 按冻结规则计错，不自动重试或补判。
- 334 个 v3 请求全部 `ok`，unknown/inflight 均为 0，len(LLM) client 重试为 0。
- 冻结 plan 按 92 个唯一 session 登记 332 calls，但现有 `session_documents` 将 2 个超长
  session（`conv-41/session_13`、`conv-42/session_27`）各拆成 2 个符合 6,000 字符上限的
  文档；因此真实抽取为 94 次，v3 实际模型调用为 334 次。题、数据、四臂、检索和评分没有
  改变；以 ledger 为真实用量来源。
- test 的 253 次调用及两个 conversation 均已终态完成后，进程在写第二个
  `owner-close.json` 时因路径与 dev 重名触发 `FileExistsError`。该异常发生在全部模型调用和
  `report` 数据落盘之后；`STOPPED.json` 原样保留，离线 finalization 明确记录
  `stopped_after_all_terminal_calls`，停止后新增模型请求为 0。

## 保留的历史结果

LongMemEval 旧结果不变：none `4/12`、lexical `11/12`、Mem0 `9/12`。LoCoMo v1 保留为
1 request / 11,351 tokens / 2,048-token length stop；v2 保留为 2 requests / 26,006 tokens /
单次云端回读未及时命中；v3 初跑保留为 8 requests / 102,453 tokens / 8,192-token length stop。
恢复批次的 `v3r1.` 命名空间与三份旧 ledger 均无 call identity 重放。

后期只读复核已能同时精确匹配 v2 的两个旧 trace；这解释了当时是远端可见性时序问题，但不会
回写 v2 的 STOP、恢复其答案或把旧 trace 混入 v3。证据见
[v2-posthoc-readback.json](v2-posthoc-readback.json)。

## 复现和验证

冻结前相关测试为 **66 passed**，Ruff 通过。覆盖有界轮询成功、180 秒上限超时、重复/歧义匹配、
未知 usage 立即停止、旧 v2 final receipt 隔离、累计 lineage 去重、32,768 预算预留、四臂/冻结/
失败关闭和原有 v1/v2 契约。冻结后同一 source/data/gold hash 通过 `verify`，真实 run 使用
`retry=0`；最终 report 从 120 条 safe rows 生成，导出器再次核验 334 个 v3 请求全为 `ok`、
3 个 conversation 全为 `completed`、首两 trace 回读为 `verified`。

机器可读结果见 [summary.json](summary.json)、[rows.jsonl](rows.jsonl)、
[split-summary.json](split-summary.json)、[usage.json](usage.json)、
[provenance.json](provenance.json) 和 [finalization.json](finalization.json)。
公开结果不含题目正文、对话 history、gold、模型正文、私有 facts、索引或凭据。

## 解释与下一步

第一版最稳妥的结论是：在这组 30 题、单次采样中，词法与 Mem0 的总体差异很小，且 judge 的
512-token 上限损害了四臂比较的解释力。Mem0 的 session-level 排序在 facts 组合上更高，但答案
正确数没有同步提高，尤其 multi-hop 表现较弱。

下一批最有信息量的实验应只改变一个变量：把 judge 输出上限提高到足以稳定返回 JSON，并预先
验证所有 judge 都为 `valid`；同时保留同一批题和同一抽取快照做配对复核。对抽取应把
“unique session 数”和“实际 document 抽取数”分开登记，按 94 个文档冻结 334 次真实调用，而不是
继续沿用 92/332 的旧规划口径。

可面试看板位于
[mem0-rsi-effects.canvas.tsx](C:/Users/MLL/.cursor/projects/c-Users-MLL-Desktop-my-agent-harness/canvases/mem0-rsi-effects.canvas.tsx)，
沿用原 Canvas 路径，展示 LongMemEval 旧结果、v1/v2/v3 初跑失败和 v3 recovery 的真实成绩。
Canvas 使用本地内联数据，不发起网络请求。
