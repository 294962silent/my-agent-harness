# 隔离产物回读能力探针

2026-11-12，模型前冻结。不是 SWE 成绩或缓存性能对照。

两份合成日志的随机值仅位于隐藏中段；fixture manifest SHA256 为 `e0babe0f79fa847f79eea3f42a6d0d71bdb00d75cfe6db4ac9e24fc2795c223d`。输入为无用户数据的合成内容，模型只能通过 fetch_log 与生产 artifact_read / artifact_search 获取信息。开启投影后必须先收到隐藏目标的预览，再真实回读、最终答对，才算通过；猜中不计。关闭投影后从全文答对即可。

四个 trial，每个最多四次 API、1024 输出 token、60 秒；温度 0.7，memory 关闭、不摘要、不自动重试。两臂工具 schema 相同。审计在实际 Provider 方法入口、SDK 序列化之前，仅保存 hash、工具名和可见性布尔；不是 wire 抓包。Session、完整响应及产物正文仅在独立临时目录存在，结束删除。

共享原 $30 账本；阶段前累计 $0.572059，本阶段新增至多 $0.25，绝对准入上限 $0.822059。之后三题 SWE 对照最多另加 $1.75，合计新增不超过 $2。用量未知保留预留并停止整批，保守冷价记账不等于服务端账单。

离线冻结：`python -m experiments.benchmark_live.artifact_probe freeze-fixtures --output <全新目录>`。
执行：`python -m experiments.benchmark_live.artifact_probe run --fixtures <fixtures.json> --fixtures-sha256 <批准的SHA> --ledger-path <原账本> --spend-ceiling-usd 0.822059 --output <全新结果目录>`。凭据只由宿主环境提供，不放命令行或容器。

此文件写入时尚未发出新模型请求；结果和生成源码版本另行追加，失败不换 fixture。

实际执行补充：生成源码 `c69ff37ed148f53d526b458b9b36233860b85f26`，模型前 CI run43241787757 四组全部通过。probe plan SHA 为 `b0f50a8e40bfd33c65edda3800e3020ce0b2879e6a7528e763bf21aeec03f366`。四 trial 共12次已结算请求：on各4次，off各2次，费用 $0.009106，累计 $0.581165。

on两次均完整观察到隐藏fetch预览及handle→真实回读→目标进入后续Provider输入，没有提前泄漏；最终回答正确为0/2。off两次从全文答对，2/2。on在第4次响应仍调用工具，随后触及原4请求上限；Agent终态为budget_exhausted，不能把“已读到”算成“已交付”。没有为完成答案追加请求或换日志。审计未保存响应正文、推理和finish reason，不能解释模型继续读取的动机。

该结果支持真实生产回读链路可用，四请求下端到端成功率不支持投影收益；不能据此推断SWE质量、长工程任务或服务端KV表现。两臂schema SHA一致（`68c9031d4f22abaf2d4ec24aa07344ca44ea6c032e3e9fa2405985e9c72b54b7`），所有usage已结算，unknown为0。独立子代理已核四份audit/result及Agent终态边界；本轮没有重跑。

原plan/summary及四份audit/result仅含元数据，归档于[统一原始证据](../swe-budget-visibility-20261112/raw/probe/)，受[同一manifest](../swe-budget-visibility-20261112/raw/manifest.json)绑定。完整来源、费用分桶及离线复算见[配套报告](../swe-budget-visibility-20261112/REPORT.md)。用户之后已将优先级调整为三项集成，不继续benchmark或补跑探针。
