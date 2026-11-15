# LoCoMo semantic context-diversity v1

执行日期为 2026-11-15（Asia/Shanghai），状态为 `complete`。本实验在未用于本轮研究的新 conversation 上比较三个固定 dense-retrieval arm；conversation 与问题选择、Qwen 索引和三臂候选在付费 answer/judge 前冻结。本文件是独立实验记录，不改写共享 `HARNESS_EXPERIMENT_LOG.md`。

## 预注册选择

- 数据固定为 `snap-research/locomo` revision `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`，source SHA256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，CC BY-NC 4.0。
- seed 固定为 `20261116`，排除本轮已使用的 `conv-30`、`conv-41`、`conv-42`、`conv-43`、`conv-47`。选择器只读取 `sample_id`、`qa.category`、`qa.evidence`；在冻结前没有读取任何模型答案或分数。
- 选中 `conv-49`、`conv-44`；每个 conversation 从 category 1/2/4 各取 4 个 evidence-labeled 官方题，共 24 题，类别为 multi-hop 8、temporal 8、single-hop 8。
- 两次 conversation 共 53 个唯一 session、296 个 raw chunk；每题三个 arm 都有 8 个候选，context 上限 6,000 字符。
- plan SHA256 为 `184637586d25dfc6143ca6af8028e6bc406ee29b2a8bb4fe7c90cc91bc34450b`，preflight SHA256 为 `5f5e41ed9fbca80d6f504a569160948d33b4628ca919a6e92795b5de6a896651`。

## 固定检索 arm

三臂共用同一 raw chunk 语料和本地 Qwen3-Embedding 0.6B revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`。语义 relevance 为归一化向量余弦相似度，统一保留 12 位小数；完全相同时按 chunk key 字典序决胜。

| arm | 固定定义 |
| --- | --- |
| `semantic_top8` | 对全部 raw chunks 做 dense semantic ranking，取前 8 |
| `semantic_mmr_top8` | 首个 chunk 取 semantic rank 1；之后令 `relevance=cos(q,d)`、`diversity=1-max_selected_similarity`，最大化 `0.7*relevance + 0.3*diversity`，即 `0.7*relevance - 0.3*max_selected_similarity`；候选池为同一全部 raw chunks；并列依次按更高 relevance、更小 chunk key |
| `semantic_session_cap2` | 按 semantic ranking 扫描，每 session 最多取 2 个 chunk，直到选满 8；只有全部语料无法满足时才按 semantic ranking 补位 |

候选由本地 WSL credential-free seccomp GPU worker 计算；Qwen revision 未改变。当前宿主仅暴露一张 `NVIDIA GeForce RTX 5060 Laptop GPU`，实际映射为 `CUDA_VISIBLE_DEVICES=0`。三臂共享原 answer/judge prompt、`deepseek-v4.1-flash`、temperature 0、answer cap 1,024 与 judge cap 4,096；自动重试 0。

## Retrieval-only preflight 与成本

- preflight 完成 24 题 × 3 arms 的完整候选、MMR trace、session-cap 边界与 6,000 字符渲染检查；付费模型请求为 0。
- 本地 embedding invocation 共 320 次：296 个 raw chunk + 24 个问题；最大实际 context 为 5,902 字符。
- session-cap 没有发生不足补位；每题每臂生成 8 个候选，最终 context 包含 7 或 8 个 chunk。
- 按 `ceil(serialized prompt chars/4)+output cap` 估算 144 次请求约 557,386 emitted tokens，低于 2,000,000 accounting budget；请求上限为 180。

## 主结果

answer length 与 judge failure 均按错误计，所有分母均为 24。session recall/MRR 按 8 个候选 chunk 的 session 首次出现顺序计算；evidence-turn recall 按 6,000 字符内实际渲染 chunk 计算。

| arm | 正确 / 分母 | 准确率 | valid answer | valid judge | session recall@8 | session MRR | evidence-turn recall@8 | mean context chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `semantic_top8` | 9 / 24 | 37.50% | 23/24 | 24/24 | 0.7210 | 0.6244 | **0.6322** | 5,181.96 |
| `semantic_mmr_top8` | 9 / 24 | 37.50% | 23/24 | 24/24 | **0.8244** | **0.6476** | 0.6066 | 5,275.58 |
| `semantic_session_cap2` | **10 / 24** | **41.67%** | 23/24 | 24/24 | 0.7210 | 0.6244 | 0.6114 | 5,190.58 |

按类别：

| category | `semantic_top8` | `semantic_mmr_top8` | `semantic_session_cap2` |
| --- | ---: | ---: | ---: |
| multi-hop (n=8) | 3 | 1 | **4** |
| temporal (n=8) | 2 | 2 | 2 |
| single-hop (n=8) | **4** | **6** | 4 |

题目 `q_e6ae90a6b0834302c2f583a2` 的三个 answer 都在 1,024 cap 处返回 `length`，因此三臂各有一个无效 answer；三个 judge 均有效。该案例保留为错误，没有因结果重跑。

## 配对比较

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| top8 vs MMR | 2 | 2 | 4 | 1.000000 |
| top8 vs session-cap2 | 1 | 2 | 3 | 1.000000 |
| MMR vs session-cap2 | 2 | 3 | 5 | 1.000000 |

MMR 改变了部分正确答案但没有提高总数；session-cap2 比 top8 多 1 题，exact McNemar `p=1.0`。本样本不支持任一策略的稳定 answer 优势。MMR 的候选 session recall/MRR 高于 top8，而 evidence-turn recall 更低，说明会话级多样性与证据 turn 覆盖并不等价。

## 请求、trace 与耗时

| stage / total | requests | prompt tokens | completion tokens | provider total | summed request latency s |
| --- | ---: | ---: | ---: | ---: | ---: |
| 合计 | 144 | 98,212 | 29,965 | **158,257** | **729.348** |

144 次模型请求的实际首末跨度为 814.934 秒。前两次请求通过 required Langfuse 的 180 秒有界 metadata/usage 回读：2 个 distinct traces，每个恰 1 条 observation，额外模型请求 0；等待 1、2 秒，回读耗时 10.628 秒。

144/144 ledger 行均为 `ok`，unknown/inflight 为 0；owner close 的 dropped、export errors、close errors 均为 0，runtime 已退出。实际使用 158,257 accounting units、144 requests，均低于冻结预算。

## 证据和验证

- gold SHA256：`a0edc1f8fbd241506c51cf21933a1e38cce6858af487f642184198cdb39ef91e`；safe rows SHA256：`1a8242e6d03fed3bf612f535f2daa86a36c51eba2600a779aa79bcbe8020d356`。
- 安全导出逐行核对 72 个 answer+judge rows 与 144 个 ledger requests，并重验 plan、plan manifest、preflight、source/data/gold、Qwen revision 和模型 manifest。
- 逐题候选、MMR trace、session-cap 边界、usage、延迟和状态见 [rows.jsonl](rows.jsonl)；完整 retrieval receipt 见 [retrieval-preflight.json](retrieval-preflight.json)。

## 结论与边界

这是 2 个新 conversation、24 个类别平衡题上的小样本确认实验。MMR 没有提高 answer accuracy；session-cap2 比 top8 多 1 题，但配对检验不支持稳定优势。MMR 改善了 session recall/MRR，却降低了 evidence-turn recall，说明不同检索指标之间存在 trade-off。结果不外推到全部 LoCoMo、其他 embedding 模型、其他 lambda/cap 或调参后的方案。
