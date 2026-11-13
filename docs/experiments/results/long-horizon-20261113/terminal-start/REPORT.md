# Terminal-Bench 2.1 启动快照

2026-11-13 12:05 UTC，本机WSL正式启动。此目录记录启动条件，**不是最终成绩**，也不是完整SWE测试。后续结果另行归档，不覆盖本快照。

执行源码提交 `455915743ee687006698eaeefe3a98d43b55609e`；实际计划SHA256为 `8cb76c1479c97638eed024e627e13397a1fe63078b4209105886ac4442e16216`。原源码未稳定时的等待器已在freeze前停止、无模型trial；这次使用独立terminal-queued-v2调度阶段。

固定八题、16个on/off槽位，七题oracle通过；git-multibranch两个槽位保留blocked，不替换。首题cancel-async-tasks先off后on。只切artifact_projection，复用生产Agent/ContextManager/Session和shell+artifact工具栈，schema须实际一致；不代表普通CodingAgent全部工具的整体消融。

每个trial最多64请求（含摘要）、context32000、输出4096、时限按官方配置封顶；CPU1核/2GB/GPU0。首个完整配对有效、无疑似验证器基础设施故障后，调度器才继续剩余槽位。源漂移、未知用量或预算停止会封停，不重试已开始trial；普通代码失败保留原0分。

Memory结束时账本为480请求/$0.937546。代码阶段仍使用原账本，绝对累计上限$15.888355，外层原上限$30，不重新获得预算。执行器/Docker在本机，生成调用既有远端DeepSeek接口。

[plan-projection.json](plan-projection.json)列安全任务身份、顺序、约束、run_id和计划哈希；原完整计划、journal、日志和账本留本机私有运行目录。未公开凭据、代理地址或完整模型消息。

该源码提交的[CI run43422249290](https://github.com/294962silent/my-agent-harness/actions/runs/43422249290)已completed/success，四组Ubuntu/Windows × Python3.11/3.13逐组通过，包含Ubuntu3.13 SDK专项。CI通过不替代真实官方任务评分。
