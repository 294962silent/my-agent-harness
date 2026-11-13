# 本地中文记忆与受控策略自优化

日期：2026-11-13。起点：`9b0810aedadbaa86ddbc893b06af16ffae01bb99`。

用户已确认：Mem0启用本地CPU中文向量模型；Dream首版做受控参数自动优化；暂无真实评分树，先完成闭环和功能验证。SWE及效果benchmark继续暂停。

## 项目定位

主线是**面向长程任务的Coding Agent Harness**，以长期记忆、可观测执行、受控策略自优化为能力。长程任务是目标场景，不承诺当前已经具有跨进程任务恢复或外部副作用exactly-once。自优化限定为Dream搜索策略参数，不是模型权重训练、任意Python自改或已经取得跨任务持续提升。

## A / SOL-1：Mem0本地CPU

拥有memory_local.py、memory_mem0.py必要派发、本地profile/setup模块与相关测试；Settings新Dream块也由SOL-1负责。主代理独占依赖、全局配置实际写入及文档。不同助手不得覆盖他人文件。

- [x] 固定fastembed0.9.1（CPU onnxruntime），模型BAAI/bge-small-zh-v1.5，512维/中文，MIT；官方模型repo Qdrant/bge-small-zh-v1.5，revision `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。ONNX文件94781076B、SHA256 `1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38`，其他所需文件各自核官方Git blob/LFS哈希。
- [x] 下载先走官方匿名HTTPS；Windows需要时在进程内采用系统代理，不输出认证、不读HF token/curlrc/netrc，不降低TLS。写临时文件、验证后原子入缓存，运行期只接受校验完的本地缓存，不暗中换模型。
- [x] 固定Mem0 f179局部构造真实Memory实例，绕开原fastembed provider忽略CPU/cache实参的问题；显式CPUExecutionProvider、cuda=False、threads=2、specific_model_path、local_files_only。NeverLLM拒绝任何抽取，不构造OpenAI客户端、不读取模型认证；不全局monkeypatch工厂。SDK首导入前禁遥测，不能宣称SDK重试已被强制终止。
- [x] 模型只读共享；候选SQLite/Qdrant缓存明确所有权，避免多个adapter争同一锁。原facts/tombstone权威、单worker/超时熔断、失效候选重验及caller-owned语义保持。
- [x] 真实模型下载成功后，用临时事实/向量库做生产链路功能smoke，屏蔽所有socket验证运行期本地；这是功能检查，不是记忆消融/召回率成绩。缺模型/篡改/错误设备必须明确拒绝，不能用假向量冒充。
- [x] 只在模型和smoke成功后准备本机启用。实际全局settings由主代理合并：保留未知/已有用户字段，原子写入并留私有备份，不把认证/备份提交Git。分发依赖保留optional，用户本机安装并启用mem0-local。

## B / SOL-2：受控Dream参数闭环

拥有Dream contract/optimizer/registry、replay/worker接线与专属测试。只用固定官方ParallelRefineMethod；不运行上游任意策略Python生成器。上游评价直接信任候选SimResult，不足以用作任意代码的权威晋升，因此首版按用户选择只允许参数。

```python
optimize(dataset_manifest, output, *, registry_path=None, budgets=(1, 2, 4),
         max_candidates=16, max_seconds=300, dry_run=False,
         min_gain=0, max_workers=1, timeout_seconds=30) -> dict
```

- [x] 白名单max_width为1..64、max_refine为0..64，严格int且非bool；按trace形状clamp并记录requested/effective。候选网格在结果前冻结，最多16候选/32树/4步数预算，不需要额外模型调用。
- [x] manifest区分synthetic/observed，记录source、task_id、train/holdout、相对路径及SHA；拒绝路径逃逸及同任务/同评分树跨split。来源声明不等于密码学认证，不拿Session/SWE单链编造树。
- [x] 只在train选择一个候选，holdout只验证该候选与baseline一次；不拿holdout选候选。可信指标定义为回放best相对已记录正数上界的归一化均值，评分和预算由固定执行器/真实action日志校验，不能接受候选自报reward；这不是论文AUC或任务成功率。实际TraceEnv.reset初始best为0，baseline只参与归一化上界，不能伪造初始已达分。
- [x] 总墙钟预算默认300秒，单worker不超过剩余时间；失败/超时/无效记录不丢样本、不扩大预算、不晋升。输出目录new-only，输入、计划、源码版本、每次回放和汇总hash可核。
- [x] synthetic必须dry-run，永不写生产active；observed、完整验证且holdout严格优于baseline才eligible。晋升前重验叶子产物/指标/文件hash，不只信顶层布尔。无真实数据返回needs_dataset，不伪称真实优化完成。
- [x] registry保存不可变版本、原子active/previous指针与FileLock，失败保持旧active，rollback也核hash。replay默认实际消费合法active参数；损坏则明确baseline fallback。优化内部强制use_active=False，避免污染基线。
- [x] CLI新增optimize/replay/status/rollback并兼容旧--trace。Settings全局字段为dream_optimization；显式CLI参数优先，未给值从全局配置读取，不读项目块。enabled控制默认优化入口，不阻止显式单次命令；关闭自动优化不撤销已晋升策略，baseline/rollback单独控制。

## C / SOL-1：配置小接缝

```python
DreamOptimizationSettings(
    enabled=False, dataset_manifest=None, registry_path=None,
    max_candidates=16, budgets=[1, 2, 4], max_seconds=300.0,
)
```

- [x] 加入Settings.dream_optimization及既有全局专属键，严格类型/范围；项目配置不能改数据集、registry或启用开关。默认分发关闭；本机用户请求启用，但无数据时status必须needs_dataset、active仍baseline。

## 主代理验收/交付

- [x] 两名Sol/medium只读核实际SDK、安装与运行时开关区别、官方数据缺失及任意代码评分漏洞；用户已作后端和优化范围选择。
- [x] 增加mem0-local optional extra、fastembed0.9.1和uv.lock；当前解析100包，不调用付费API。
- [x] 检查真实CPU功能和参数闭环回归、两人交叉review、CLI与打包、必要整体验证；真实数据/效果字段明确保持未测。
- [x] 合并本机配置、记录旧值备份路径与新状态，不打印私密字段；训练数据缺失时仅启用能力，不自动晋升合成策略。
- [x] 更新README定位、架构/面试文档及持续实验日志，解释与旧固定baseline的变化。
- [ ] 明确路径提交origin/PR2，核最终head CI，不自动合并；最终状态以PR/checks为准。

## 2026-11-13 交付前验收

本机配置已实际启用，Dream status为needs_dataset/no_active_policy；真实CPU模型功能与合成参数闭环已执行，生产active未创建。最终本地1302 passed、2 skipped（Harbor）、5 subtests passed；双方互审无阻断。wheel实际构建成功，188项，8个必需模块/TUI入口、Dream console与mem0-local metadata均核验；Ruff、lockcheck和按仓库行尾规则的diffcheck通过。没有启动SWE、付费模型或真实效果实验。
