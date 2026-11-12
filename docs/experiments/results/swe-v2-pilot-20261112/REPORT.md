# SWE v2 固定小样本：结果与边界

日期：2026-11-12。本批完成通用catalog入口与v2真实模型验证；没有运行完整SWE-bench Lite 300题，也没有与其他Agent做榜单对比。

## 实际结论

固定选定6题，5题通过环境/gold预检，1题保留blocked。5个可运行题各做on/off一次，共10次真实模型运行；**开启组解决2/5，关闭组解决1/5**。3次运行实际通过官方测试，另7次为空补丁、已知未解决且没有执行测试。所有运行都在16请求预算处结束，不把budget_exhausted改称正常完成。

本批160次请求全部结算，保守冷价账本 **$0.503462**。11个产物只在on组产生，两个组的artifact_read/search响应均为0。说明投影确实激活，但没有真实模型回读使用证据；回读正确性仍由已有离线往返测试支持。

这些数字不足以证明整体性能或因果收益。开启组输入较少、输出较多，保守总费用也没有下降；不能把单题多通过一次写成稳定提升。

## 冻结设计

- 任务来源：官方[SWE-bench/swe-bench-tasks](https://github.com/SWE-bench/swe-bench-tasks/tree/3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d)，固定revision `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`。52个原文件在下载、复制和gold后核Git blob SHA1/SHA256，不改官方测试或镜像。
- 选择：[SELECTION.md](SELECTION.md)。两个CPU项目、一个已跑过v1的已知回归题，其余按数字编号与Lite标签选择；不是随机代表性样本，不因本批模型成绩换题。
- 模型生成源码：[7652168](https://github.com/294962silent/my-agent-harness/commit/765216830b1972ba3b3c0d20171315343c63d288)。该版本的四组跨平台[CI](https://github.com/294962silent/my-agent-harness/actions/runs/43218293170)通过。生成后发现的统计修复单独交付，不将新源码冒充实际生成版本。
- 协议v2：两组注册相同生产shell/artifact_read/artifact_search schema，只有Context产物投影开关不同。每次新建禁网容器，无宿主/金标挂载，先reset固定base并核clean；评分使用独立原官方容器。
- 请求模型：`deepseek-v4.1-flash`，现有OpenCode Go兼容端点。温度0.7、16请求/4096输出/600秒、无隐式SDK重试；所有辅助模型调用也计数。seed23只排任务和eligible首发臂（3 on/2 off），不控制供应商采样或服务器缓存。
- 环境：本机Windows下WSL Ubuntu-20.04、Python3.12.14、Docker26.1.3、SWE-bench5.0.2；CPU单模型并行，无GPU、无L20、无DSH。
- 首请求前冻结[plan](raw/plan.json) SHA256 `b7a3ddfbd871743026fa76601805f24a138b1beac93a2e48882d5fcb22c1398a`，catalog SHA256 `fe6f15e9887c5e07243c54e91b6f5e93cff56f8c3841d387cb312c2db43f126c`。184项输入/源码hash在归档前全部复核。

## 逐题官方结果

以下每个臂都是一次模型trial；费用为客户端保守账本，不是供应商核账。

- **pytest-5103**：on/off均为空补丁，均未解决；分别$0.050700/$0.070654。没有执行官方测试，不算评分环境失败。
- **sympy-11400**：on/off均为空补丁，均未解决；分别$0.033795/$0.035465。
- **pytest-5227**：on/off均经原官方测试resolved；分别$0.062584/$0.045532。
- **pytest-5221**：on/off均为空补丁，均未解决；分别$0.071703/$0.049072。
- **sympy-20590**：on resolved、off为空补丁；分别$0.034061/$0.049896。该题是已知回归题，不充当未见任务。
- **sympy-11870**：原gold在固定300秒测试窗口超时，318.39秒含清理，completed0/resolved0/error1；没有给模型发送请求、不替换题目、不纳入五题模型分母。

gold阶段20590复用上轮成功证据并重绑安全输入，其余成功题本轮原官方gold通过；预检通过只证明环境，不是模型成绩。

## 用量、费用与缓存

- **on**：80请求；完整输入583228 token，输出64864，总648092；保守账本$0.252843。
- **off**：80请求；完整输入617804 token，输出54369，总672173；保守账本$0.250619。
- 完整输入按provider total_tokens−completion_tokens计算，合计1201032；总token1320265、输出119233。归一化prompt_tokens合计140552，不能把它误称全部输入，它不含另列的cache部分。
- 费用按输入$0.30/M、输出$1.20/M、缓存也计全冷价，逐请求向上取整到微美元。不能用本地估计冒充OpenCode订阅扣费或服务端余额。
- 继续同一30美元账本：历史32请求$0.068597，本批160请求$0.503462，累计192请求$0.572059；本批绝对客户端准入ceiling为$3.068597，未重置旧消费。
- cache_read字段只覆盖158/160请求（on78/80、off80/80），已观察值1060480仅是部分观测；完整总量为null。cache_write为0/160覆盖，总量同样null。**不补零、不报告完整命中率排名，也不与DSH比较。**

## 暴露的问题与处理

首配对完成后，自动汇总把所有非completed结果误标为environment_or_execution_error。主代理先停后续收费，直接核两份原aggregate发现empty_patch=1、error=0，于是更正为“预算内没有修改”。没有重跑模型/评分、修改补丁或放宽限制；随后按原冻结计划完成余下运行。

[原state/报告](raw/manifest.json)保留字节与原错误分类；[derived-outcomes.json](raw/derived-outcomes.json)另存版本化派生，不覆盖旧记录。10个已知官方结果、3次实际测试、5个完整已知结果pair、1个双臂都执行测试的pair分别统计。源码分类修复在整批生成/评分/归档之后进行。

7次空补丁说明当前固定预算下交付不足；11个外置产物而0次回读说明“可调用”尚未变成“模型使用”。这两项是后续要诊断的实验发现，不把其原因臆断为某个模块，不承诺加预算或加提示必然解决。

## 可复核材料

- [raw/manifest.json](raw/manifest.json)：81个列入文件及其SHA256/大小，manifest自身SHA256 `7321985b32f571baa37d96361b45ab51c1a91ea5343b1dd381b6e6219f69e282`；复制前核92项原结果文件，复制后再核81项。
- [raw/summary.json](raw/summary.json)、[原始账本](raw/ledger.json)、[派生分类](raw/derived-outcomes.json)、[源码指纹](raw/source-hashes.json)。仅复制白名单材料，不包含gold/test patch、hints、模型session、完整artifact正文或认证数据；未复制正文的产物记录hash/大小。
- [许可证索引](licenses/index.json)：按六题各自base固定的SymPy BSD-3-Clause和pytest MIT，未用HEAD代替。
- [预检快照](preflight/preflight-summary.json)与[预检文件清单](preflight/manifest.json)：6份原gold报告、6份绑定证据、52官方文件hash。它们是模型前阶段快照，其准备时Git head/源码指纹不冒充最终生成版本；生成版本以765及raw plan为准。
- [可移植指标脚本](analysis/recompute_metrics.py)与[metrics.json](analysis/metrics.json)：从归档plan/ledger按run_id分本批/历史和on/off，字段缺失保留null，先验证所有归档hash再计算。
- 全部原始记录仍在本机缓存中；公开归档保留必要证据与路径/hash，不保证这些绝对路径能在别的机器直接执行。重新生成实验须重新准备官方环境与新的冻结目录。

不重新调用模型即可在仓库根复算：

```sh
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_results \
  --archive docs/experiments/results/swe-v2-pilot-20261112/raw \
  --manifest-sha256 7321985b32f571baa37d96361b45ab51c1a91ea5343b1dd381b6e6219f69e282
python docs/experiments/results/swe-v2-pilot-20261112/analysis/recompute_metrics.py \
  --raw-dir docs/experiments/results/swe-v2-pilot-20261112/raw \
  --expected-manifest-sha256 7321985b32f571baa37d96361b45ab51c1a91ea5343b1dd381b6e6219f69e282
```

当前纯parser版本为 `swebench-5.0.2-outcome-v1`，分析代码SHA单独输出；原生成版本765不变。相同aggregate hash可能来自相同的单题统计，独立run_id、原state和测试stdout仍分别保存，不用hash相同推断重复运行或借用了gold评分。

先固定空补丁与回读使用问题的假设，再另做预注册小样本；当前结果不支持直接扩成完整300题或宣传稳定质量/费用/KV收益。
