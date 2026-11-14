# Agent / 大模型应用实习：简历与面试交付稿

更新时间：2026-11-14。定位为 **Agent / 大模型应用开发实习生，突出 Python 工程、工具边界、可信评测、预算与恢复**。进取定位为 Agent 平台研发实习，需先确认个人负责模块并完成源码讲解；目前不写算法研究、独立自进化系统或性能领先。

本稿按 [great-resume 技能](C:/Users/MLL/.codex/skills/asu-skills/skills/great-resume/SKILL.md) 整理；机器事实基线见 [research-career-claims.json](research-career-claims.json)。项目事实可引用，个人职责与掌握程度尚未确认；以下不把 AI 协作产物归为独立手写。投递前只需补真实身份/时间及个人负责模块，不需要继续加功能。

## 简历顶部摘要

求职方向：Agent / 大模型应用开发实习。项目展示重点为 Python Agent 的受控工具执行、可信评测、调用预算与完成边界恢复；可提供源码、定向回归、Docker 边界验证及失败实验记录。个人负责模块与技术熟练程度以面试演示和本人确认为准。

## 项目条目：可直接选用的三条能力描述

**项目：my-agent-harness｜代码实验 Agent 工程验证**

技术：Python、异步调用、JSONL journal、Docker、Mem0、Langfuse；Dream-RSI 受控参数搜索。

时间：**【待补：本人真实参与时间】**；职责：**【待补：本人负责模块及设计/实现/验收范围】**。以下采用项目客观描述，确认个人动作后再改成“负责/设计/实现”。

- 构建固定数据上的代码实验流程：Agent 仅通过受限文件工具修改训练脚本，在无网络、只读根目录及资源限制的 Docker 环境生成预测，由宿主校验预测并重算 RMSE，降低候选程序自报分数造成的误判；有 8 项 Docker 边界验证证据。
- 通过追加式 journal 记录请求预留、真实用量结算、评估与 round checkpoint；真实模型完成两轮、新进程恢复和一次独立 final，累计预算保持连续，未知调用或未完成 round 阻断重放，final 不回流选优与经验；最终保留 baseline，未声称质量提升。
- 接入 Mem0 本地 CPU 经验检索入口、Langfuse 本地 SDK 观测与 Dream-RSI 受控策略参数，固定经验和策略来源并保留 baseline；核心提交 `160118f` 已推送 origin，[PR #2 协作中](https://github.com/294962silent/my-agent-harness/pull/2)，附本地回归、Docker 边界及 wheel 验证材料。

以上描述系统能力与工程价值，未声称个人独立完成、生产部署、云端观测已上传或质量提升。只有确认自己的具体工作后，才将对应条目改为第一人称主动动词。

## 60 秒口述

“我希望找 Agent 或大模型应用开发实习。这个项目关注模型改代码之后，如何保证评分可信、调用不越预算以及中断可恢复。候选脚本在受限容器中只输出预测，宿主重算 RMSE；调用前记录预算，完整 round 才提交检查点，final 不回流选参和记忆。真实 red 实验完成两轮、新进程恢复和独立 final，但最终保留原均值 baseline，没有模型改进。首版 MemoryError 失败和全部成本也保留，不能只挑成功运行。我的个人职责是【待补：真实模块和动作】，实现采用 AI 协作，我能讲清【待补：已掌握的一条调用链】。”

## HR 开场白

您好，我正在寻找 Agent / 大模型应用开发实习。有一项 Python Agent 工程项目可交流，重点是受限工具执行、宿主可信评分、调用预算和完成边界恢复，附本地回归、Docker 验证与源码。PR 目前协作中，真实实验的失败和限制也有记录。希望结合岗位要求介绍个人负责的具体工作。

## 五个可深挖问题

1. 为什么不直接读取候选 stdout 的 RMSE？请沿预测 CSV → ID/有限值校验 → 宿主重算这条链解释；指出标签在哪个进程可见。
2. 调用发出后进程中断，为什么不能直接重试？解释 reservation、settlement、未知 usage 和完成 round 边界；说明 hash chain 没有外部锚点的限制。
3. Docker 到底限制了什么？说明只读输入、单文件输出、网络/资源限制，以及共享内核和宿主 daemon 仍属于可信基础。
4. Mem0、Langfuse、Dream-RSI 分别负责什么？区分经验候选召回、本地观测、受控参数；为什么接上 SDK 不代表云上传或质量提升？
5. 为什么最优候选仍可能比原脚本差？说明 baseline 纳入选优、同分保留原代码；为什么 dev 不下降不代表 final 或 RSI 提升？

答题材料见 [13-代码实验Agent的可信评分与恢复.md](13-代码实验Agent的可信评分与恢复.md)；真实结果见 [LIVE_REPORT.md](../experiments/results/research-agent-20261114/LIVE_REPORT.md)。须区分真实流程完成、无效候选与算法质量收益。

## 结果槽位与投递前最后核对

- **live-v1 已知失败（必须保留）**：真实 red 首次 1 API / 1252 tokens 已结算；MemoryError；1 baseline evaluation；0 committed rounds；未执行 final。最小离线工具重放未复现，不能断言根因已修复。
- **live-v2 已独立核验**：两轮提交、新进程恢复、独立 final valid=true；24 请求/106826 reported tokens，8 dev+1 final 评分。dev RMSE=0.8467093297076379；final RMSE=0.7554124334834056。两轮各 2 个 budget_exhausted 无效 attempt、1 个有效同分候选，最终均保留原均值 baseline；完整 optimizer trace 为 null，没有质量提升。与 v1 合计 25 请求/108078 tokens/10 评分，USD unavailable。Mem0 第二轮 1 个有效语义候选/3 次本地 embedding/fallback=false，仅验证功能。Airfoil/white 与正式三组未启动，optimize blocked、未晋升。证据：[live-summary.json](../experiments/results/research-agent-20261114/live-summary.json)。
- **个人职责【待本人确认】**：选定亲自做过的设计、审查、复现、修改或验收动作，并能解释上述至少一条调用链；未确认前不写主导、Owner、独立从零实现、精通。
- PR 状态随时间变化；当前为 open、未合并，不能写“被上游采用”。没有 Mem0/RSI 质量提升证据，没有 Langfuse 云上传证据；历史 HumanEval 47/48 不作为本场景、长程能力或个人独立实现成绩。

快速验收：先看 [SOL-2 恢复报告](../experiments/reports/research-sol2-20261114.md)、[Docker 边界结果](../experiments/results/research-agent-20261114/docker-boundary-smoke.json)、[主计划](../plans/2026-11-14-research-agent-runtime.md)，再用一条调用链做个人讲解。更多材料无需进入简历正文。
