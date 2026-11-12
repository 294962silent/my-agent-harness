# SWE 通用任务入口与 v2 小样本执行计划

日期：2026-11-12。用户已明确要求继续“扩展评测入口、完成 v2 真实小样本”。保留此前 $30 总预算、两名 GPT-6.1 Sol / medium 助手和主代理设计 / 集成 / 验收分工；不重新申请已获授权的操作。

## 设计与范围

选择“固定官方 catalog + 独立 campaign 调度”，保留现有 solver 与独立官方评分。继续逐题改硬编码会阻碍扩展；直接接受任意 CLI 镜像又缺少可核验绑定。catalog 记录固定官方来源与 revision、任务安全元数据、base、image/config digest；可信宿主准备后冻结 SHA256，执行器每次核对。这个 hash 是变化检测与来源记录，不是第三方签名或抗恶意宿主证明。

本批最多 6 个独立 SWE-bench Lite CPU 任务，on/off 各一次，最多 12 trials；先完成一个 v2 smoke，再执行余下冻结清单。任务与 arm 顺序在模型试验前冻结，用 seed23 排列并平衡先后；seed 不控制供应商采样。每 trial 16 次请求、4096 输出、600 秒；保持相同模型、温度0.7、工具、上下文与限制。只有 Context 产物投影开关不同。

沿用同一持久账本，总上限 $30，历史已记 $0.068597；不会重置累计消费。192 次请求按每次32K输入的规划和全冷价估算约 $2.79，本批以小额成本完成管线验证；这不是严格的供应商账单保证，实际请求仍必须预留 / 结算。缺失 usage、预算不足或协议错误停止相应运行，禁止隐式重试。任务环境失败保留，不因某一 arm 的模型结果换题。

不改生产 Agent / Context / Memory，不接入 Mem0、Langfuse 远端或 Dream-RSI，不运行完整300题，不运行GPU或DSH。

## A. SOL-1：catalog 与 solver 解耦

文件所有权：`experiments/benchmark_live/swe_catalog.py`、`swe_agent.py`，`tests/experiments/test_benchmark_live_swe.py` 和新 catalog 测试。其他文件只读。

- [x] 先增加缺失接口、第三题与篡改绑定的失败回归；保存真实失败结果。
- [x] `load_catalog(path, expected_sha256=...)` 验证冻结文件；`Catalog.get(instance_id)` 返回明确 TaskSpec。
- [x] catalog 只允许安全字段，拒绝 patch / test_patch / solution 等进入 solver 输入；保留完整元数据与安全投影的来源关系。
- [x] `gated_task(metadata, evidence, *, catalog=...)` 按 catalog 绑定 base/image/config/hash，仍核官方 gold 成功结果及报告hash。
- [x] DockerTask 接受已验证 TaskSpec；CLI 增加 `--catalog`、`--catalog-sha256`。不传 catalog 保持原两题兼容。
- [x] 相同 v2 工具 schema、受限回读、禁网无宿主挂载、新容器、重置固定base、凭据只在host等约束不变。
- [x] 窄范围回归与 Ruff；把精确接口和证据交给 SOL-2。主代理集成后由另一助手复核。

## B. SOL-2：campaign 与证据汇总

文件所有权：新 `experiments/benchmark_live/swe_campaign.py`、其独立测试与本批专用辅助脚本 / 报告。A 文件与共享实验日志不改。

- [x] 先验证冻结清单、稳定顺序、resume 和失败分类的回归确实覆盖缺口。
- [x] campaign manifest 固定 catalog SHA、任务清单、完整 arm 顺序、模型 / 参数 / 限制、账本路径与官方评分版本；运行前校验。
- [x] 每 trial 新输出目录；完成项有输入/源码指纹且原始文件完整才允许跳过。中断与未完成项保留，重试必须新 attempt 明确记账，不覆盖历史。
- [x] solver 与评分使用独立容器；官方报告缺失 / 环境异常 / budget_exhausted / resolved 分开，不将环境失败或未知值填零。
- [x] 汇总 eligible、blocked、attempted、scored、resolved 与 paired 差异，同时记录实际 artifact 数量和回读。没有激活的干预不计作有效因果对照。
- [x] 离线测试无收费请求；交叉复核 catalog / solver 后才允许主代理启动真实运行。

## C. 主代理：数据、真实执行与 GitHub

- [x] 冻结官方 taskrepo revision `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`、选择规则与具体清单；保存环境预检失败。
- [x] 验证原官方任务文件字节与镜像 config；先跑原 gold / oracle，通过后冻结逐题证据。六题中五题通过，一题固定窗口超时保留blocked。
- [ ] 冻结代码和 campaign manifest，确认共享账本累计消费和 API 模型；凭据不写 CLI、容器、日志或Git。
- [ ] 执行 v2 smoke 与冻结小样本，独立官方评分，逐 trial 保存原始结果、费用、停止原因和产物激活信息。
- [ ] 集成适当回归、Ruff、证据hash与隐私检查；跨代理复核。源码有变则重新做相应集成验收。
- [ ] 追加 `HARNESS_EXPERIMENT_LOG.md` 与面试主张 / 开发问题，明确真实 / 离线 / 未验证边界；仅暂存明确路径，推自己的 origin，更新 PR #2 并核最终CI，不自动合并。

## 验收结论的边界

通用入口不等于300题运行完成。小样本不报告榜单级成功率，不把样本重复当独立任务。Artifact未激活时，token差异只作轨迹观测；cache字段缺失不排名完整命中率。运行没有因果收益也要如实交付，不能用追加题目或调限制美化结果。
