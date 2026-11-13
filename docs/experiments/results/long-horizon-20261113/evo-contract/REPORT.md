# SWE-EVO clean评分接缝的真实契约

2026-11-13，本机 WSL，**0次LLM请求**。用新公开adapter、73份已核原始官方文件的clean snapshot，在fresh官方Dask评分容器中执行空代码补丁。

结果：FAIL_TO_PASS 0/44，PASS_TO_PASS 2861/2861，resolved=false，符合原empty预检。CLI退出0，报告位于固定run/model/iid目录且身份匹配，无评分/清理错误。75份快照文件逐hash复验，pyc=0；只清理本批自有容器。

新adapter SHA256 `dda7435e019dde8468e3954b012f5abf637824f785a160f8a8ad4b576f15d855`，旧预检adapter `60672af1…` 保留。它们使用相同cached_pull、镜像白名单及固定官方评分逻辑，寻址方式不同，未声称hash相同。通过此检查说明新评分装配可执行，不代表Agent已完成Dask，也不是效果消融结果。

首次尝试在评分启动前因私有启动脚本漏建output_final目录失败。产品score已有目录创建；只修私有脚本并使用新v2输出目录，首失败记录保留。未重跑模型、改账本或覆盖历史gold/empty。

[metrics.json](metrics.json)记录状态、输入/镜像/adapter及原产物hash；[official-report.json](official-report.json)保留官方报告原bytes；[scorer-files.json](scorer-files.json)列实际执行快照；[manifest.json](manifest.json)绑定三份机器证据，不含本说明。原命令和日志留本机私有缓存，公开证据不含凭据或主机绝对路径。

解释器和第三方site-packages仍为独立信任边界，不将clean snapshot表述为任意代码安全沙箱。复现入口见[SWE-EVO适配说明](../../../SWE_EVO_ADAPTER.md)。
