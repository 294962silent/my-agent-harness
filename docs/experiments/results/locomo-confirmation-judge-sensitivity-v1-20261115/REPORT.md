# LoCoMo raw-confirmation judge sensitivity v1

执行日期为 2026-11-15（Asia/Shanghai），状态为 `complete`。本实验只补评 LoCoMo raw-retrieval confirmation v1 的三个 `judge_status=length` 案例；答案、gold、question、answer/judge prompt、模型和 temperature 均保持原值，仅把 judge output cap 从 1,024 提高到 4,096。主结果不被替换，本报告单独记录敏感性结果。

## 冻结输入与选择

- 原 confirmation plan SHA256：`6d4a41d19308526ae91dfa6bbc42cc3a972ec7084ce6b2def6b6753d970f4cbe`。
- 原 safe rows 为 24 题 × 3 arms，共 72 行；选择规则是全量且仅有 `judge_status=length` 的三行，没有按正确性筛选。
- 三个案例为 `q_3a58e1cb8843d924c1e366af/raw_lexical`、`q_237878f81d3e18d629972459/raw_hybrid_rrf`、`q_4776cc50be81a1c897c3091b/raw_hybrid_rrf`，均属 multi-hop。
- 模型固定为 `deepseek-v4.1-flash`，provider `openai`，temperature 0，自动重试 0；judge cap 4,096，单次请求 180 秒上限。
- scoring-only plan 在付费调用前冻结；预算为 6 requests、100,000 accounting units、1 小时。

## 主结果与敏感性结果

主结果继续把三个 length judge 计为错误。敏感性结果对每个案例补评一次；仍截断或解析失败也按错误计。

| arm | 分母 | 主结果正确 | 主准确率 | 主有效 judge | 补评后正确 | 补评准确率 | 补评后有效 judge | session recall@8 | session MRR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` | 24 | 10 | 41.67% | 23/24 | 10 | 41.67% | 24/24 | 0.8264 | 0.8194 |
| `raw_semantic_qwen` | 24 | **12** | **50.00%** | 24/24 | **12** | **50.00%** | 24/24 | 0.8542 | 0.7431 |
| `raw_hybrid_rrf` | 24 | 10 | 41.67% | 22/24 | **12** | **50.00%** | 24/24 | 0.8819 | 0.7951 |

三个补评请求均返回 `stop` 和严格 JSON boolean；0 个仍为 length，0 个 malformed。两个 `raw_hybrid_rrf` 案例改判正确，`raw_lexical` 案例补评后仍判错；`raw_semantic_qwen` 没有 length judge，因此不变。

## 配对比较

主结果：

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| lexical vs semantic | 0 | 2 | 2 | 0.500000 |
| lexical vs hybrid | 1 | 1 | 2 | 1.000000 |
| semantic vs hybrid | 2 | 0 | 2 | 0.500000 |

补评后：

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| lexical vs semantic | 0 | 2 | 2 | 0.500000 |
| lexical vs hybrid | 0 | 2 | 2 | 0.500000 |
| semantic vs hybrid | 1 | 1 | 2 | 1.000000 |

补评把 hybrid 从 10/24 提高到 12/24，与 semantic 相同；所有配对比较仍未达到 p<0.05。原报告“本样本不能支持稳定优势”的结论不变。

## 请求、trace 与验证

- 模型请求：3；prompt 584；completion 1,683；provider total 2,267 tokens。
- 3 次请求的账本延迟合计 28.785 秒。首次与第二次请求均通过 required Langfuse 的 180 秒有界 metadata/usage 回读：2 个 distinct traces，每个恰 1 条 observation，额外模型请求 0。
- 3/3 ledger 行状态为 `ok`，unknown/inflight 为 0；owner close 的 dropped、export errors、close errors 均为 0。
- 只改变 judge output cap；retrieval、answer 和主结果文件未重跑或改写。

## 边界

这是对三个已知截断 judge 的一次 scoring-only 敏感性补评，不是新的确认集，也不是 judge 无偏性证明。补评改变了 hybrid 的点估计，仍没有足够样本支持任一 arm 的稳定优势。
