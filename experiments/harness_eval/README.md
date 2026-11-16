# 离线工程消融

在仓库根目录设置 `PYTHONPATH=src;.`（Windows）或 `PYTHONPATH=src:.`（POSIX），运行：

```text
python -m experiments.harness_eval --suite all --repeats 3 --output <结果父目录> --seed 23
```

默认只运行离线合成夹具，不提供真实模型选项。每次新建唯一目录，保留 manifest、raw.jsonl、summary.json 和 report.md；不会覆盖旧结果。`--suite` 支持 tools/session/context/memory/permission/runtime/observation/model，可指定多个。

场景调用实际生产 Registry、Session、ContextManager、ArtifactStore、MemoryStore、Agent、PermissionGate、LLM 门面及 observation API。假 Provider 仅通过单个 LLM 实例注入；不替换全局 Provider 注册表。工具依赖和分支关闭组是明示的有限 comparison wrapper，不是不存在的生产功能开关。owner、流关闭/取消是 contract，没有假造关闭组。

`passed` 表示预期干预/契约成立，**不表示任务质量或安全成功**：关闭门禁组预期出现一个未授权副作用，关闭依赖保护组预期读到缺失值；必须同时看 metrics。fixture recall 仅对应两条公开标签，不能外推真实记忆召回质量；并发 sleep 仅验证重叠与依赖，不代表真实吞吐。

每次重复计入时间样本；unique_cases 按模块与case去重。时间包括夹具和文件操作，提供 median 和最近秩 p95；少量重复不能支持稳定性能结论。逐条结果包含实际观测、断言、生产路径和干预描述，汇总从记录计算；失败不会被丢掉。错误只记类型以免泄露输入。usage/token、cache 和真实质量不可用时为 null。

manifest 记录 git SHA、dirty 状态、源码内容 hash 与范围、运行前后源码是否变化、Python/平台、seed、配置与许可证文件。结果含 source_changed_during_run=true 时应重跑。夹具为本项目生成，不包含外部 benchmark 数据。

`record.schema.json` 供其他 runner 复用数据形状（允许 mode=live），本 CLI 不处理真实请求；live 的预算、官方任务及 token/评分聚合应由真实 runner 提供，不能使用此处 offline 的 null 总量作为真实统计。不要把本目录合成结果混入官方 benchmark 通过率。
