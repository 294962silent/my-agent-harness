# 长程基准环境与评分预检

2026-11-13，本机 WSL Ubuntu-20.04 / Docker 26.1.3。此目录记录模型生成前的环境有效性检查，**模型请求为0**。不属于 Agent 成功率结果，也不把参考解通过当成框架收益。

## Terminal-Bench 2.1

固定官方 revision `12b7565679ce5006230b820666353673a133db93`，八题共81个源文件、154081字节，逐文件核官方 Git blob。全部为 CPU 任务，限制1核、2GB、GPU=0。

七题官方 oracle reward=1：cancel-async-tasks、large-scale-text-editing、log-summary-date-ranges、fix-git、regex-log、db-wal-recovery、custom-memory-heap-crash。

git-multibranch 的 oracle reward=0，CLI 未报 exception，但验证器的 uv release 下载受 TLS 错误阻断，随后 uvx 不可用。该题保留 blocked，不替换成另一道题，不计为模型失败。其余七题之后也可能发生验证器环境故障，每次正式 trial 仍需检查，oracle 并不保证未来运行必定有效。

初次 registry 失败、network/proxy/https 修复后的各次记录均保留。仅调整本批任务的网络路径，没有改官方 task、test 或 solution，也没有修改共享 Docker daemon。重做 oracle 属于基础设施诊断，不是重跑模型以挑最好成绩。

逐次结果身份、原始 result SHA256 与官方 reward 见 [terminal-oracles.json](terminal-oracles.json)。未复制可能含主机配置的原始 config。

## SWE-EVO

固定官方 fork revision `9b83d5af943ba7a17567336f5b18239f73960219`，数据 Arrow SHA256 为 `74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`。在模型运行前选定 Conan 与 Dask，不因预检结果替换样本。采用该 fork 原评分入口及原 test_patch，另有已记录的缓存传输适配器，不用 stock SWE-bench 或自制简化评分器替代。

- Dask gold：FAIL_TO_PASS 44/44，PASS_TO_PASS 2861/2861，resolved=true；empty：0/44、2861/2861，resolved=false。允许进入模型生成。
- Conan gold：FAIL_TO_PASS 72/72，PASS_TO_PASS 640/649，9项回归，resolved=false；empty：0/72、630/649。保留 blocked，不发模型，不将其回归归因于 Agent。

四份官方 report 原 bytes、逐份 SHA256 和计数见 [evo-oracles.json](evo-oracles.json)。empty 的代码补丁为空串，由原入口合并官方 test_patch；gold 使用数据中的 patch，Dask 的 all_patch 为 null。原评分使用 fresh 容器；已清理本批自有预检容器。

原 Dask 镜像含基线之后的 Git 历史。生成接缝另行采用同工作树、固定单 root 的 Git 重建，并检查旧对象不可读；这项防泄漏探针不属于本目录的模型成绩，也不意味着镜像中所有潜在泄漏已被排除。

## 可核验范围

[execution-source.json](execution-source.json) 是本机核验的123份源码/uv.lock补充清单，生成版本为 `16bdae0e4ec7fb8511ea8bd86d52e26d84d04c18`。新增 EVO 接缝将在独立计划中另行冻结。该清单不是第三方 site-packages、Python 运行时或服务端模型权重的完整认证。

[manifest.json](manifest.json) 绑定本目录机器可读证据，不包含本说明。导出只读取现有产物，没有重跑评分、调用模型或修改原始结果。原始运行保留在本机私有缓存；公开报告不含凭据、完整历史、gold patch 或模型 prompt。

正式模型结果将另存目录，沿用原累计预算与失败记录，不覆盖这次预检。
