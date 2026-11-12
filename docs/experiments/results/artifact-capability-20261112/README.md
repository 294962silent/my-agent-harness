# 隔离产物回读能力探针

2026-11-12，模型前冻结。不是 SWE 成绩或缓存性能对照。

两份合成日志的随机值仅位于隐藏中段；fixture manifest SHA256 为 `e0babe0f79fa847f79eea3f42a6d0d71bdb00d75cfe6db4ac9e24fc2795c223d`。输入为无用户数据的合成内容，模型只能通过 fetch_log 与生产 artifact_read / artifact_search 获取信息。开启投影后必须先收到隐藏目标的预览，再真实回读、最终答对，才算通过；猜中不计。关闭投影后从全文答对即可。

四个 trial，每个最多四次 API、1024 输出 token、60 秒；温度 0.7，memory 关闭、不摘要、不自动重试。两臂工具 schema 相同。审计在实际 Provider 方法入口、SDK 序列化之前，仅保存 hash、工具名和可见性布尔；不是 wire 抓包。Session、完整响应及产物正文仅在独立临时目录存在，结束删除。

共享原 $30 账本；阶段前累计 $0.572059，本阶段新增至多 $0.25，绝对准入上限 $0.822059。之后三题 SWE 对照最多另加 $1.75，合计新增不超过 $2。用量未知保留预留并停止整批，保守冷价记账不等于服务端账单。

离线冻结：`python -m experiments.benchmark_live.artifact_probe freeze-fixtures --output <全新目录>`。
执行：`python -m experiments.benchmark_live.artifact_probe run --fixtures <fixtures.json> --fixtures-sha256 <批准的SHA> --ledger-path <原账本> --spend-ceiling-usd 0.822059 --output <全新结果目录>`。凭据只由宿主环境提供，不放命令行或容器。

此文件写入时尚未发出新模型请求；结果和生成源码版本另行追加，失败不换 fixture。
