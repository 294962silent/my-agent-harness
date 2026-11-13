# Terminal首组：保留原分数，停止无效配对与未知费用扩批

2026-11-13，本机WSL。冻结计划 `8cb76c1479c97638eed024e627e13397a1fe63078b4209105886ac4442e16216` 的首题cancel-async-tasks off/on均已结束。**科学有效配对为0，收益差值不可用；没有启动剩余14槽或EVO模型生成。** [启动快照](../terminal-start/REPORT.md)保留原条件。

## 实际结果分层

- off：Agent正常end_turn；原官方reward=0。验证器下载uv0.9.5时发生curl35/SSL_ERROR_SYSCALL，随后env文件和uvx缺失，无ctrf、未观察到测试执行，因此suspected_verifier_infra=true。这不是有效的模型答错证据。
- on：Harbor记录AgentTimeoutError，Agent取消；验证器确实执行、ctrf存在，**原官方reward=1**。分类器因执行超时将结果列execution_or_verifier_error，分类后的official_reward为null。原1分与超时同时保留，不把通过测试等同Agent在时限内完成。

两个条件均不满足完整有效配对资格；尤其不能用原reward的0→1宣称artifact_projection提升。on更多请求/耗时也不能在此基础上推导普遍性能结论。

## 已知费用与未知预留

本组53个请求，其中52个已结算、1个在900秒时限取消后未返回完整usage。已知结算281062微美元（$0.281062），完整任务费用为unavailable。取消请求的45131微美元预留仍保留，不能以0或预留额伪装成实际结算。

全项目此时533请求，已知结算$1.218608，另有未知预留$0.045131；客户端已知加预留为$1.263739。它们是三种不同口径，不是供应商账单或完整实际消费。原总上限$30、代码绝对ceiling $15.888355均不重置。该未知请求status=cancelled、usage/cost=null，不是在途pending。

调度器在首组收割后核到unknown即停止，源未漂移；原账本和run_id保持。独立EVO队列只完成0API门控模拟、未启动；不能在此状态下通过换campaign或复制账本继续付费。恢复完整费用认定需要供应商可核验用量记录，本文不制造该记录。

## 网络定位与边界

已核off实际job→Harbor→trial的HTTPS代理设置，与此前reward1 oracle完全一致；Harbor verifier/env.exec确实传递该环境。没有发现漏配代理证据。

另建同镜像/同Harbor env.exec的fresh运输探针，1CPU/2GB/GPU0，不执行test.sh、不评分、0LLM。环境匹配、NO_PROXY不绕过；无重试curl经过CONNECT github.com、302、CONNECT release-assets.githubusercontent.com、最终HTTP200/exit0。正文丢弃到/dev/null，未安装或验证下载内容的字节hash。当前通道可用，**不能从当前成功追溯原trial的TLS35原因**。两探针容器均清理；首个私有结果解析器对None stderr的失败记录保留，新探针单独保存。

这说明oracle预检是必要条件，仍需对每个trial单独验证。后续若准备固定依赖缓存或改网络协议，应新建预注册批次并重新做零模型预检；不补跑此trial以挑最好结果、不修改官方测试来消除失败。

## 证据

- [metrics.json](metrics.json)：原reward与分类结果、终态、原result/日志hash、缺失费用和预留分列。
- [requests.json](requests.json)：53条原usage/cost对应记录，保留null，无模型正文或密钥。
- [transport-probe.json](transport-probe.json)：脱去主机IP后的运输元数据及原文件hash，未作为正式模型成绩。
- [manifest.json](manifest.json)：绑定三份机器文件，不含本说明。

原官方result、ctrf、stdout、Session及账本留在本机私有目录，可按hash回查。公开元数据能复算数量/费用与资格判断，不能还原或重判未公开的模型代码。完整Memory三臂已独立完成，见[Memory报告](../memory-v2/REPORT.md)，不受本次Terminal配对失效改写。
