# LoCoMo raw-retrieval confirmation v1

执行日期为 2026-11-15（Asia/Shanghai），状态为 `complete`。本实验在预注册的新 conversation 上比较三个固定的 raw-retrieval arm；选择、索引和协议在付费 answer/judge 前冻结。本文件为独立研究记录，不修改共享 `HARNESS_EXPERIMENT_LOG.md`。

## 预注册选择

- 数据固定为 `snap-research/locomo` revision `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`，source SHA256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，CC BY-NC 4.0。
- seed 固定为 `20261115`，排除本轮已使用的 `conv-41`、`conv-42`、`conv-47`。选择器只读取 `sample_id`、`qa.category` 和 `qa.evidence`，不使用任何模型分数或结果。
- 选中 `conv-30`、`conv-43`；每个 conversation 从 category 1/2/4 各取 4 个 evidence-labeled 官方题，共 24 题，类别为 multi-hop 8、temporal 8、single-hop 8。
- 两次 conversation 共 48 个唯一 session、274 个 raw chunk；每题三个 arm 均有 8 个候选，context 上限 6,000 字符。
- plan SHA256 为 `6d4a41d19308526ae91dfa6bbc42cc3a972ec7084ce6b2def6b6753d970f4cbe`，preflight SHA256 为 `5eed0e3aef8c1e7def362e5454719ac5955a42eb5dc0e9c98aa56dae8d5525d1`。

## 固定 arm 与模型

| arm | 检索定义 |
| --- | --- |
| `raw_lexical` | 同一 raw chunk 语料；BM25 `k1=1.5,b=0.75`；top-8；6,000 字符渲染上限 |
| `raw_semantic_qwen` | 同一 raw chunk 语料；本地 Qwen3-Embedding 0.6B revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`；dense top-8；同一渲染上限 |
| `raw_hybrid_rrf` | 对 lexical top-8 与 semantic top-8 做 RRF，固定 `k=60`，按 `1/(60+rank)` 合分、key 决胜，输出 top-8 |

三个 arm 共用相同 answer prompt、`deepseek-v4.1-flash`、temperature 0、answer cap 1,024 和原 judge prompt/model；judge cap 固定为 1,024，自动重试 0。

retrieval-only preflight 已完成 24 题 × 3 arm 的候选边界检查、298 次本地 embedding invocation，付费模型请求为 0；最大实际 context 为 5,981 字符。当前宿主只暴露一张 `NVIDIA GeForce RTX 5060 Laptop GPU`，映射为 `CUDA_VISIBLE_DEVICES=0`；GPU 2 不存在。Qwen model revision 未改变，WSL credential-free seccomp worker 通过握手。

## 主结果

length judge 仍按错误计，所有分母均为 24。MRR 为官方 evidence session 的 session MRR。

| arm | 正确 / 分母 | 准确率 | valid answer | valid judge | session recall@8 | session MRR | evidence-turn recall@8 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_lexical` | 10 / 24 | 41.67% | 24/24 | 23/24 | 0.8264 | **0.8194** | 0.6500 |
| `raw_semantic_qwen` | **12 / 24** | **50.00%** | 24/24 | 24/24 | 0.8542 | 0.7431 | **0.7403** |
| `raw_hybrid_rrf` | 10 / 24 | 41.67% | 24/24 | 22/24 | **0.8819** | 0.7951 | 0.7611 |

72 个 answer 全部为 `valid`。judge 为 69 `valid`、3 `length`、0 malformed：`raw_lexical` 1 个、`raw_semantic_qwen` 0 个、`raw_hybrid_rrf` 2 个。即使把 cap 提高到 1,024，仍存在少量 reasoning-only length，按预注册规则保留为错误，不重试或二次补评。

按类别：

| category | `raw_lexical` | `raw_semantic_qwen` | `raw_hybrid_rrf` |
| --- | ---: | ---: | ---: |
| multi-hop (n=8) | 1 | **2** | 0 |
| temporal (n=8) | 6 | 6 | 6 |
| single-hop (n=8) | 3 | **4** | **4** |

## 配对比较

| comparison | left only | right only | discordant | McNemar exact p |
| --- | ---: | ---: | ---: | ---: |
| lexical vs semantic | 0 | 2 | 2 | 0.500000 |
| lexical vs hybrid | 1 | 1 | 2 | 1.000000 |
| semantic vs hybrid | 2 | 0 | 2 | 0.500000 |

semantic 比 lexical 多 2 题、比 hybrid 多 2 题，但每题只有 2 个 discordant pairs，McNemar 精确检验均未达到 p<0.05。lexical 的 session MRR 最高，而 semantic 的 evidence-turn recall 更高；本样本不足以把任一差异解释为稳健优势。

## 请求、trace 与耗时

| stage | requests | prompt tokens | completion tokens | provider total | summed request latency s |
| --- | ---: | ---: | ---: | ---: | ---: |
| answer | 72 | 118,440 | 12,377 | 131,201 | 322.409 |
| judge | 72 | 12,630 | 19,447 | 32,077 | 381.858 |
| 合计 | 144 | 131,070 | 31,824 | **163,278** | **704.267** |

144 次模型请求的实际首末跨度为 785.446 秒。前两次请求通过 required Langfuse 的 180 秒有界 metadata/usage 回读：2 个 distinct traces，每个恰 1 条 observation，额外模型请求 0；等待 1、2 秒，回读耗时 11.161 秒。144/144 ledger 行均为 `ok`，unknown/inflight 为 0；owner close 的 dropped、export errors、close errors 均为 0。

预算为 180 requests、2,000,000 accounting units、2 小时、retry 0；实际 144 requests，明显低于请求与容量上限。

## 证据和验证

- gold SHA256：`92a19d57814f0fabca6b884e7f2fb098afce8a2d013bbb1108a62b0005860372`；safe rows SHA256：`0421c3e88b83e72f2a3fd33fceef11975e4bd61b22e61330ea9ddd5697c4a49f`。
- ledger SHA256：`05a504179bf55e0e6f3b25a4f32c5e4b3296d2ac3a81af8b3fdda91bc66a43cb`。
- 安全导出逐行核对 72 个 answer+judge rows 与 144 个 ledger requests，并重验 plan、plan manifest、preflight、source/data/gold、Qwen revision 和模型 manifest。
- 新增协议测试、既有 `test_memory_validation.py` 和 `test_langfuse_sdk.py` 合计 30 passed；Ruff 通过。

## 结论与边界

这是 2 个新 conversation、24 个类别平衡题上的小样本确认实验。semantic 以 12/24 高于 lexical 和 hybrid 的 10/24，但配对差异只有 2 题，不能据此声称语义检索或 RRF 有稳定优势。三个 arm 的 3 个 length judge 仍按错误保留，没有因结果不理想重跑。该结果不外推到全部 LoCoMo、其他 embedding 模型或参数调优后的方案。
