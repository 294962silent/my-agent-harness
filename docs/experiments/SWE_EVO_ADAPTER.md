# SWE-EVO 固定版本适配与复现输入

本接缝使用本机 WSL、Docker 和 Python 3.12；不会部署到 L20。它需要外部官方 Arrow、官方评分源码、已核验镜像和已授权现有账本，不能零配置运行。

官方源码固定为 [`SWE-EVO/SWE-EVO@9b83d5af943ba7a17567336f5b18239f73960219`](https://github.com/SWE-EVO/SWE-EVO/tree/9b83d5af943ba7a17567336f5b18239f73960219/SWE-bench)。仓库附带 [73 文件原始清单](SWE_EVO_SOURCE_MANIFEST.json)，SHA256 为 `8abe773cd47e75a0d0f7c778ece57c1919cef40fbbb7f8ef6c48614b9c6291f9`。清单只有相对路径、Git blob、SHA256 和大小，不含用户路径或凭据。

## 获取评分源码

从本仓库根目录在 WSL 运行下列命令，`WORK` 为新的实验目录。脚本只下载清单中的 73 个固定原文件，不克隆整个上游仓库、不安装依赖、不执行评分或模型请求。

```bash
export CUDA_VISIBLE_DEVICES=2
export WORK=/absolute/path/to/new-evo-work
python3.12 - "$WORK/scorer" <<'PY'
import hashlib, json, pathlib, sys, urllib.request
proof = pathlib.Path('docs/experiments/SWE_EVO_SOURCE_MANIFEST.json').read_bytes()
assert hashlib.sha256(proof).hexdigest() == '8abe773cd47e75a0d0f7c778ece57c1919cef40fbbb7f8ef6c48614b9c6291f9'
manifest = json.loads(proof)
assert manifest['revision'] == '9b83d5af943ba7a17567336f5b18239f73960219'
assert len(manifest['entries']) == 73
root = pathlib.Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
for entry in manifest['entries']:
    relative = pathlib.PurePosixPath(entry['path'])
    assert relative.parts[0] == 'SWE-bench' and '..' not in relative.parts
    url = 'https://raw.githubusercontent.com/SWE-EVO/SWE-EVO/' + manifest['revision'] + '/' + entry['path']
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read()
    assert len(raw) == entry['bytes']
    assert hashlib.sha256(raw).hexdigest() == entry['sha256']
    assert hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() == entry['git_blob']
    target = root.joinpath(*relative.parts[1:])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
(root / 'source-manifest.json').write_bytes(proof)
PY
```

该环境实测具有 `docker`、`datasets`、`tqdm`、`numpy`、`requests`、`unidiff`、`GitPython`、`beautifulsoup4` 和 `ghapi` 等依赖。上面的源码下载不解决 Python 第三方依赖安装；运行时解释器和 site-packages 是独立信任边界，并未作为任意代码沙箱封闭。

## 官方输入与既有预检

所需 48 条官方 Arrow 文件的 SHA256 必须为 `74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`；通过 `--source` 显式提供。该二进制输入不包含在本仓库中，不能用自造 JSON 或其他数据集替代。

两题身份、基础提交、镜像 config ID 和四份原始 gold/empty 报告的哈希固定在 `evo_agent.py`。公开的 [预检证据](results/long-horizon-20261113/preflight/evo-oracles.json) 及四份官方报告保留原分数：Dask gold 为 F2P 44/44、P2P 2861/2861，empty 为 F2P 0/44、P2P 2861/2861；Conan gold 的 P2P 有失败，因此保持 blocked，不替换题目。

冻结器需要以下报告布局；可从公开的原报告逐字节复制，无需重造评分结果：

```bash
mkdir -p "$WORK/preflight"/{conan-gold,conan-empty,dask-gold,dask-empty}
for stage in conan-gold conan-empty dask-gold dask-empty; do
  cp "docs/experiments/results/long-horizon-20261113/preflight/evo-${stage}-report.json" \
     "$WORK/preflight/$stage/report.json"
done
```

镜像必须是原官方实例镜像，不能用实例镜像伪装 base。真实 base 从固定 `TestSpec.base_dockerfile` 构建，其原文件 SHA256 为 `3a58534fdcffc93b1033411b47c0ecf8a4700c8c6deaa902a92afbe799d68bc6`，`FROM --platform=linux/x86_64 ubuntu:22.04`。本次构建的真实 base config ID 为 `sha256:f1dd887e3cdc9b9c43f90751aa65dbb4a1d71a4f9a6676035e38f46d6b30deda`；重建受软件源更新影响，不将新构建的 ID 自动说成相同镜像。

## 可取得的缓存适配器与执行方式

[公开 adapter](../../experiments/benchmark_live/evo_cache_adapter.py) 的 SHA256 为 `dda7435e019dde8468e3954b012f5abf637824f785a160f8a8ad4b576f15d855`。它通过显式 `EVO_SCORER_ROOT` 寻址，不含用户绝对路径；只对固定两张、config ID 已核验的实例镜像返回真实本地缓存对象，其他 pull 拒绝。

历史前置预检使用的 adapter SHA256 为 `60672af1f6605e048bd539ef130e812e92e7bf96ec7eb5f508bec2e373f5a56d`，旧文件和证据未改写。新旧 adapter 的 `cached_pull` 函数和镜像白名单保持相同机制；变化是官方源码根目录寻址和导入注释。前置 gates 的复用依据是同一份逐字节核验的官方源码、同一实例镜像和同一评分逻辑，并非两个 adapter 哈希相同。

在本机已准备的 Python 环境内，从仓库根目录执行：

```bash
export CUDA_VISIBLE_DEVICES=2
export PYTHONPATH=src:.
# OFFICIAL_ARROW 和 SHARED_LEDGER 必须是实际输入的绝对路径。
# 账本必须已经获得授权；禁止复制或重置现有账本来绕过费用或重跑限制。
python3.12 -m experiments.benchmark_live.evo_agent freeze \
  --source "$OFFICIAL_ARROW" --preflight "$WORK/preflight" --output "$WORK/plan" \
  --ledger "$SHARED_LEDGER" --scorer-root "$WORK/scorer"
```

`--cache-adapter` 可选，默认使用仓库内公开 adapter。冻结会拒绝活动中的未结算请求；模型设置为 64 请求、1800 秒、32000 context、4096 输出，累计 code 阶段绝对费用上限为 15.888355 美元，仍归原 30 美元账本。

`solve --plan-root ... --expected-sha ... --instance-id dask__dask_2022.9.2_2022.10.0 --arm on|off --output ...` 默认只预览。真正生成还需显式 `--execute` 和宿主 `LIVE_BENCH_API_KEY`；没有 auth 文件回退。`--resume` 只收割既有结果，不再调用模型。`probe` 为零模型请求的 fresh 容器基线检查。

`score --plan-root ... --expected-sha ... --instance-id ... --generation ... --output ...` 使用新的官方评分容器。评分前会从已验证的清单原 bytes 创建 owned clean snapshot，并复制已冻结的公开 adapter；不复制原目录的 `__pycache__`、游离字节码或其他额外模块。以 `python -I -B` 启动该副本；`-B` 本身不能阻止读取旧字节码，安全依据是副本最初没有旧字节码及原目录不在导入路径。解释器/site-packages 仍受信任。

唯一可接受的官方报告路径为 `logs/run_evaluation/<固定run_id>/<固定run_id>/<instance_id>/report.json`，报告外层必须恰好对应该 instance。错 namespace、错任务、额外报告或父级 symlink 都不会变成可用官方成绩。报告和清理证据保留原始哈希，失败不覆盖重跑。

生成容器已实证存在未来 Git 历史。适配器只在自己 fresh 容器的 `/testbed/.git` 内删除原 objects/refs/reflog/remotes，以相同原基线树重建固定身份、固定时间的单 root commit，并保留 ignored 依赖文件。该验证针对已观测 Git 历史通道，不声称消除镜像中的一切信息泄漏；官方评分仍使用原基础提交和独立 fresh 容器。
