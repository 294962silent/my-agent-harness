# 本机 CPU 代码实验 Agent

本入口复用现有 CodingAgent、SharedLLM、分支评分树和父预算；没有额外安装 AIDE/autoresearch 或复制其实现。设计依据见[固定来源研究](../research/2026-11-14-scenario-selection.md)，验收证据见[本轮目录](../experiments/results/research-agent-20261114/LIVE_SMOKE_PLAN.md)。

## 前置条件

Python 3.11+、可用的本机 Docker daemon、一份带数值 target 的 CSV，以及已在宿主配置的模型凭据。Windows 可以使用本机 WSL Docker，默认发行版名称为 Ubuntu-20.04，也可用 `--docker-command-json` 指定命令参数数组。没有容器时直接阻断，不退回宿主运行候选代码。

镜像必须提前安装 Python 标准库并以 `sha256:...` image ID 或 `repo@sha256:...` 固定在本机；本工具不自动拉取。首版不安装 sklearn、numpy 或任意 Agent 依赖，不使用 GPU。模型只可修改 `train.py`，只能调用工作区文件工具；候选训练代码由宿主在受限容器中执行。

## 最小流程

在仓库根目录运行（会调用真实模型的步骤为 run/resume）：

```sh
uv run --no-sync python -m my_coding_agent.research_cli --help

uv run --no-sync python -m my_coding_agent.research_cli prepare /absolute/new-campaign \
  --csv /absolute/input.csv --target target_column --image sha256:FIXED_IMAGE_ID \
  --provider openai --model deepseek-v4.1-flash --base-url https://YOUR_PROVIDER/v1 \
  --rounds 2 --width 2 --refine 1 --max-attempts 3 --memory-mode lexical \
  --max-requests 32 --max-tokens 200000 --max-seconds 900 --max-evaluations 20 \
  --max-output-tokens 4096 --attempt-max-turns 4

uv run --no-sync python -m my_coding_agent.research_cli run /absolute/new-campaign \
  --pause-after-round 1 --auth-provider openai --auth-profile default

# 新进程继续；只恢复完整已提交 round 之后的工作。
uv run --no-sync python -m my_coding_agent.research_cli resume /absolute/new-campaign \
  --auth-provider openai --auth-profile default

uv run --no-sync python -m my_coding_agent.research_cli status /absolute/new-campaign
uv run --no-sync python -m my_coding_agent.research_cli final /absolute/new-campaign
```

示例使用 POSIX 换行；PowerShell 可将每条命令写成一行。替换 CSV、target、镜像和 provider URL；不得将示例中的占位符当成已存在的资源。默认通过现有 AuthManager 取凭据，endpoint 必须匹配冻结配置；也支持单独的 `RESEARCH_API_KEY`，它不会写进 manifest。CLI 构造宿主客户端后清除本进程凭据环境，子进程不继承模型密钥；不会修改父 Shell 配置。

`prepare` 产生独立 task 与 campaign 目录。固定行划分、数据/评分器/代码/策略 hash；原 CSV 不放进 Agent 源码。`run` 在每个请求和评分前记账，`status` 重建累计预算；已发出而没有完整结算的工作不会自动重试。`final` 只能在所有计划 round 完成后执行一次，之后不允许继续搜索或优化。本入口不提供绕过不确定费用的 reset。

## 三项集成

- `--memory-mode none|lexical|mem0` 明确本次模式；train 可写已验证 dev 经验，val/test 仅读冻结快照。用 `--memory-source` 指定已有训练经验目录。Mem0需要已下载且验证过的本地CPU中文模型与 `--mem0-model-dir`；未安装SDK或模型无效应报告实际失败/回退，不能记作成功的语义召回。
- run/resume 的 `--langfuse` 请求使用现有全局观测配置；本地调用证据不依赖云服务。没有凭据或未回读远端 trace 时，云上传状态保持 unverified。观测只存元数据，不导出原始数据集、预测标签或密钥。
- `optimize` 接收显式、完整、任务身份独立的 train/holdout 评分树；从 train 唯一选择策略，再独立验证，baseline 允许胜出。发布策略只作用于新的 campaign，不能悄悄改变已冻结任务。参数搜索成本目标是可核验的回放评分调用代理，真实请求/tokens单独记录。

策略集合的 train/val/test 和每个 CSV 内部的 train/dev/final 是不同划分。不能把同 CSV 不同随机划分冒充独立任务；不能从 final 反馈生成记忆或反复重选参数。固定/随机搜索顺序与 runtime 随机行动调度也分别记录。

## 资源与限制

容器使用无网络、只读根、非 root、CPU/内存/pids限制、固定只读输入和单预测文件写挂载。宿主只接受精确 ID 的有限数值 CSV，自行计算 RMSE，不相信 stdout 分数、不加载候选pickle。容器/daemon与宿主评分代码是可信基础，不能宣称已覆盖所有内核逃逸或并发攻击。

恢复支持完成 round 边界；文件hash链是完整性证据，不是外部签名。Windows没有本实现对应的目录fsync。不宣称通用任意时刻恢复或exactly-once。真实效果应按同预算独立任务对照，本入口运行成功本身不证明 Mem0 或 RSI 优于固定方案。
