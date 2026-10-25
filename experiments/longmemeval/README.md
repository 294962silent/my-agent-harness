# LongMemEval 评测：记忆系统 v2 vs 基线

## 测什么

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)（ICLR 2025）是公开的长期记忆基准：500 道题、6 种题型，每道题附带一段**带时间戳的多会话历史**，要求系统答出历史里的事实。

| 题型 | 对应能力 | 题数 |
| --- | --- | --- |
| `single-session-user` / `single-session-assistant` / `single-session-preference` | 信息抽取 | 156 |
| `multi-session` | 跨会话推理 | 133 |
| `temporal-reasoning` | 时序推理 | 133 |
| `knowledge-update` | **知识更新（新值覆盖旧值）** | 78 |

数据：`xiaowu0162/longmemeval-cleaned`（HuggingFace）。
- `longmemeval_oracle.json`：只含证据会话，历史中位 ~24k 字符（约 7k token）
- `longmemeval_s_cleaned.json`：约 48 会话/题，历史中位 ~512k 字符（约 146k token）

## 对照臂

| arm | 做法 | 说明 |
| --- | --- | --- |
| `full` | 整段历史塞进上下文 | 上下文够大时的上限参照 |
| `truncate` | 只保留最近 N token | 朴素滑窗，代表"不做记忆" |
| `rag` | BM25 检索 top-k 会话片段 | 不做 LLM 抽取，长历史下最便宜的强基线 |
| `v2` | 全文抽取事实 → 时序巩固 → 检索 | 抽取成本高，未用于主结果 |
| `v2p` | 画像 + 时间线 + 检索 | 补 preference / temporal |
| `v2s` | 检索后局部抽事实 + 检索 | 补 Knowledge Updates |
| `v3` | 画像 + 时间线 + 事实 + 检索 | **完整三层** |

`v2` 的三个动作：
1. **抽取**：一次 LLM 调用把历史抽成 `{subject, key, value, valid_from, importance}` 原子事实；
2. **巩固**：同 `(subject, key)` 的新事实把旧事实 `valid_to` 置为今天（时序有效性）；
3. **检索**：`0.6×BM25 + 0.3×时间衰减 + 0.1×重要度`，在 token 预算内打包注入。

## 判分

复用官方 `evaluate_qa.py` 的 prompt 模板（`judge.py` 里逐字保留），只替换 judge 模型为 `deepseek-v4.1-flash`。

> ⚠️ 论文默认用 gpt-4o 判分，因此**绝对分数不能与论文表格直接对比**；
> 但同一次实验内 baseline 与 v2 使用同一 judge，**相对比较有效**。

## 跑法

```bash
# 1) 数据（放到 experiments/longmemeval/data/）
#    longmemeval_oracle.json / longmemeval_s_cleaned.json

# 2) 跑一个臂（每类抽 8 题，共 48 题）
uv run python experiments/longmemeval/run_eval.py --arm v2 --per-type 8 --budget-tokens 2000
uv run python experiments/longmemeval/run_eval.py --arm full --per-type 8

# 3) 判分
uv run python experiments/longmemeval/judge.py experiments/longmemeval/results/hyp_v2_pt8.jsonl
```

## 已知局限（必须写进任何引用）

1. **抽取成本是每题的**：LongMemEval 每题历史互不相同，fact 抽取无法复用。生产环境里记忆是增量构建、按会话摊销一次的，因此"总 token"这一列在 benchmark 上被高估。
2. **oracle 版历史本来就塞得下**：全量上下文是近似上限，记忆系统在 oracle 上的主要价值是"同准确率下更少 token"，不是更高准确率。
3. **judge 不是 gpt-4o**：见上。
4. **样本量**：默认每类 8 题（48 题），置信区间约 ±10%；要下强结论需跑到每类 20 题以上。
