# LoCoMo judge-length scoring sensitivity v1

执行日期为 2026-11-15（Asia/Shanghai），状态为 `complete`。本实验只补评原 LoCoMo v3 中 120 个 judge 的 12 个 `finish_reason=length` 案例；原答案、gold、answer/judge prompt 和模型均不改变，原 v3 报告不改写。本文件是独立补充报告，不修改共享 `HARNESS_EXPERIMENT_LOG.md`。

## 冻结输入与选择

- 原 plan SHA256：`716d25917f028793ed4a4f1263dc5aa03287fa7b0ceb6973eb975ad8a1c6bc1b`。
- 原 safe rows SHA256：`a5e1fa465b0ef928750a3203821be53c879cf3e1caea1f9724e8236e80a0629e`。
- 原 gold SHA256：`3c91283cc2fda4bb21ce5c363712f50c6363d9f2be0fffee0734c629308e0abf`。
- judge prompt SHA256：`8d861de234d83a87ee7b16e7a26646563035758483d1c9fa368f70c1dc71ea31`。
- 模型固定为 `deepseek-v4.1-flash`，provider `openai`，temperature 0，自动重试 0；只把 judge output cap 从 512 提升到 4,096。
- 选取规则是全量且仅限原 `judge_status=length` 的 12 行，没有按结果筛选：`facts_lexical` 5、`facts_mem0` 5、`raw_mem0` 2、`raw_lexical` 0。
- 本次 scoring-only plan SHA256 为 `cb7b9dede7837b0cdc95a79bdbfb308e8c67345e5347db5235947522ff0da41d`。旧 v3 plan 的 `multisession_runner_v3.py` 实现 hash 与当前 HEAD 不同；数据、gold、原 plan、120 个 answer SHA、prompt 和模型绑定均重新核验通过，差异仅记录在 provenance 中。

## 主结果与敏感性结果

主结果保留原规则：12 个 length judge 仍按错误计。敏感性结果对全部 12 个案例各补评一次；仍截断或解析失败也按错误计。

| arm | 分母 | 主结果正确 | 主准确率 | 主有效 judge | 补评后正确 | 补评准确率 | 补评后有效 judge | session recall@8 | session MRR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` | 30 | 16 | 53.33% | 30/30 | 16 | 53.33% | 30/30 | 0.8299 | 0.7837 |
| `raw_mem0` | 30 | 17 | 56.67% | 28/30 | **18** | **60.00%** | 30/30 | 0.8125 | 0.7722 |
| `facts_lexical` | 30 | 12 | 40.00% | 25/30 | **13** | **43.33%** | 30/30 | 0.8913 | 0.7678 |
| `facts_mem0` | 30 | 15 | 50.00% | 25/30 | **17** | **56.67%** | 30/30 | 0.9140 | 0.8689 |

12/12 补评请求均返回 `stop` 和严格 JSON boolean；0 个仍为 length，0 个 malformed。补评共新增 4 个正确结果：`raw_mem0` +1、`facts_lexical` +1、`facts_mem0` +2。retrieval 未重跑，因此 recall 和 MRR 与主结果相同。

新增正确的 4 个案例是：`q_0a9fa04c5e0b0b58c9e4d64e/raw_mem0`、`q_13a726f575b9747a8fe48c27/facts_mem0`、`q_97f4c58f30635ecc76e45d3b/facts_lexical`、`q_97f4c58f30635ecc76e45d3b/facts_mem0`。其余 8 个均为有效且判错；逐案例内容哈希、finish reason、boolean 和 usage 见 [cases.jsonl](cases.jsonl)。

## 配对比较

主结果：

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| `raw_lexical` vs `raw_mem0` | 2 | 3 | 5 | 1.000000 |
| `facts_lexical` vs `facts_mem0` | 1 | 4 | 5 | 0.375000 |
| `raw_lexical` vs `facts_lexical` | 5 | 1 | 6 | 0.218750 |
| `raw_mem0` vs `facts_mem0` | 4 | 2 | 6 | 0.687500 |
| `raw_lexical` vs `facts_mem0` | 3 | 2 | 5 | 1.000000 |
| `raw_mem0` vs `facts_lexical` | 7 | 2 | 9 | 0.179688 |

全部 12 个案例补评后：

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| `raw_lexical` vs `raw_mem0` | 2 | 4 | 6 | 0.687500 |
| `facts_lexical` vs `facts_mem0` | 0 | 4 | 4 | 0.125000 |
| `raw_lexical` vs `facts_lexical` | 5 | 2 | 7 | 0.453125 |
| `raw_mem0` vs `facts_mem0` | 4 | 3 | 7 | 1.000000 |
| `raw_lexical` vs `facts_mem0` | 2 | 3 | 5 | 1.000000 |
| `raw_mem0` vs `facts_lexical` | 8 | 3 | 11 | 0.226563 |

补评改变了部分正确数，但没有把任何配对比较推过 p<0.05。主报告中的“未观察到稳定的 Mem0/fact 优势”结论保持不变；敏感性结果也不支持更强主张。

## 请求、trace 与验证

- 模型请求：12；prompt 1,902；completion 9,264；provider total 11,166 tokens。
- 12 次请求跨度为 152.260 秒；账本记录的请求延迟合计 136.795 秒。
- 前两次请求通过 required Langfuse 的 180 秒有界 metadata/usage 回读：2 个 distinct traces，每个恰 1 条 observation，额外模型请求 0；等待 1、2 秒，回读耗时 9.881 秒。
- 12/12 ledger 行状态为 `ok`，unknown/inflight 为 0。owner close 的 dropped、export errors、close errors 均为 0，runtime 已退出。
- Langfuse ledger SHA256：`83c2e0998e2abdf986379554f6fe282a89839e97f499165202a436650d939759`。
- 离线验证：新增测试、既有 `test_memory_validation.py` 和 `test_langfuse_sdk.py` 合计 30 passed；Ruff 通过；导出器重新核验旧 plan/data/gold/answer SHA、12 个 ledger 行和安全文件。

## 边界

这是同一 30 题 pilot 上的 judge-cap 敏感性分析，不是新的确认集，也不是 judge 无偏性证明。只改变输出上限，保持 prompt、gold 和答案不变，避免把 512-token reasoning 截断误当成事实错误，同时保留主结果的预注册计分方式。
