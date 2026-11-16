# Qwen3 本机 GPU 功能验收，2026-11-13

本批使用 RTX 5060 Laptop GPU（8151MiB，cuda:0，sm_120），没有迁移 L20，没有生成模型 API 调用。官方模型为 Qwen/Qwen3-Embedding-0.6B，revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`；10个运行文件共1,207,470,234B经官方 Git blob/LFS hash 校验。

## 独立复核的结果

- 实际路径：生产 factory → owned Mem0 → MemoryStore.aretrieve；词法回退替换为抛错，中英文两查询都返回语义候选。
- 两向量均1024维，L2 norm 为1.00000007/1.00000003。短文本 fixture 的 allocator 峰值1,207,842,816B；不代表最大长度、并发或整机显存需求。
- 这次冷构造16.45秒，batch2文档向量1.30秒，后续两检索0.467/0.053秒。样本仅2事实/2查询，首次索引与缓存状态不同，不能当延迟 benchmark、吞吐或质量优势。
- 应用 Python 未导入 Torch；独立运行环境 Torch2.9.1+cu128、Transformers4.57.6，真实 CUDA 推理。
- host 与 worker 拒绝 Python Internet socket；host 有1次被拒的 urllib3 本地IPv6能力探测。本次受控 Python 路径未发生远端连接或模型下载；这不是 OS 网络沙箱或整机流量审计。
- close_complete=true、实际 worker_exited=true；launcher/worker身份与读写线程回收已核。owned termination 的 returncode=1，不称正常0退出。GPU用量2437→2435MiB，桌面/驱动显存波动不等于泄漏。

[metrics.json](metrics.json)保留源码hash、模型/SDK方法身份、资源元数据及独立helper/result hash；不含历史正文、密钥或模型权重。原始helper/result留本机私有交付目录。作者先前验证与主代理复跑是不同运行，不叠加成独立质量样本。

## 配置与兼容代价

全局 settings 以保留未知字段的方式合并 layered（output reserve16384）和新 `local_qwen_gpu` profile；原CPU配置、模型及settings备份保留。独立1024维Qdrant路径，不复用BGE512维库；memory_index等待上限60秒，worker请求30秒/启动120秒。超时等待、进程回收有不同窗口，不是全程硬墙钟保证。

实际读取 Settings → RPC integration options → CodingAgent → ContextManager 与 GPU factory 后关闭，通过[配置证据](configuration-wiring.json)。验收LLM禁止生成，没有偷偷发 API。新配置须使用本分支/本worktree；旧 checkout 不识别新 profile，切回旧版应恢复CPU配置。

Mem0原SDK可能懒下载BM25与spaCy。Qwen-owned派生对象固定SDK源码身份，单独隔离函数globals禁NLP、dense-only建库禁BM25；不改其他实例全局。这是兼容层的语义代价，需要在效果实验中披露。

追加历史边界更正：旧BGE profile具备可选BM25/NLP路径，30题没有记录是否触发，不能无条件称旧Mem0臂纯稠密。原负结果不改，尚未进行Qwen/BGE/混合召回/rerank的质量比较。

## 验证

主代理最终组合命令：

```powershell
uv run --frozen --all-extras python -m pytest tests/test_rsi_runtime.py tests/experiments/test_rsi_closed_loop.py tests/core/test_memory_qwen.py tests/core/test_memory_local.py tests/core/test_memory_semantic.py -q -o addopts= --import-mode=importlib --tb=short
# 70 passed in16.37s；其中RSI44项、Qwen/BGE/semantic26项，不与整套累加。
```

最终整套1424 passed、11 skipped、5 subtests passed；Ruff、锁检查、wheel新worker打包与源码bytes比对通过。GPU实测与CPU fake-worker回归分别记录，不把前者外推为CI设备覆盖。

追加：首提交CI后来发现默认依赖测试和Linux stdin关闭缺陷。原metrics与上面的验证数字保留；修后新源码、最终GPU复验和1425项整库结果单独存[ci-repair](../ci-repair/REPORT.md)。其中冷启动19.21秒，不与原16.45秒组成性能benchmark。
