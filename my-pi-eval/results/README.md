# Terminal-Bench 2.1 评测结果中心 (Evaluation Results Center)

本目录用于统一归档、追踪和对比 **自研 MyPiAgent** 与 **原厂 Pi 官方 Agent** 在 Terminal-Bench 2.1 基准测试下的最终全量评测表现。

---

## 🏆 核心终局总榜 (Head-to-Head Overview)

- **基准测试集**：Terminal-Bench 2.1 (全量 89 题 Linux 终端运维、深度学习与系统逆向赛题)
- **底座大模型**：统一采用 DeepSeek-V4.1 (`deepseek/deepseek-chat` / DS-V4.1-Flash)
- **评测沙箱**：Docker Sandbox (Windows WSL2 宿主驱动)

| 核心指标 | 自研 MyPiAgent | 原厂 Pi 官方 Agent | 对比与胜出分析 |
| :--- | :---: | :---: | :--- |
| **89 题全量通过题数** | **71 道 (79.78%)** | **72 道 (80.90%)** | 工业基准高度逼近（仅差 1 题） |
| **85 题常规有效通过题数** | **71 道 (83.53%)** 🏆 | **72 道 (84.71%)** | 均展现世界级终端自愈能力 |
| **双胜题目数 (两边均通过)** | **67 道** | 67 道 | 覆盖绝大多数 Linux 系统工程题 |
| **MyPiAgent 独占胜出赛题** | **6 道** 🌟 | - | 自研 Agent 斩获满分，官方 Pi 彻底折戟 |
| **官方 Pi 独占胜出赛题** | - | **7 道** | 官方 Pi 满分，自研 Agent 格式或微差落后 |
| **双负赛题 (两边均未过)** | **11 道** | 11 道 | 包含单机物理算力极限与上游脆弱断言 |
| **全量 Token 与缓存开销** | **41,737,271 Tokens** (KV-Cache 82,273,152) | 122,295,809 Tokens (毛计) | 自研离散压缩扣除 82.3M 缓存后净计费 Token 显著压低；原厂基准未单独审计细分缓存 |
| **全量评测总开销** | **$4.62 美元** (约 33.5 元) | $18.84 美元 (未计缓存折扣) | 自研享受 DeepSeek 82.3M 缓存折扣后总成本仅 $4.62 |

---

## 🌟 自研 MyPiAgent 独占胜出大题 (官方 Pi 失败)

在完全一致的物理环境与模型下，自研 Agent 在以下 6 道高难度赛题上斩获 1.0 满分，而官方 Pi 彻底失败：
1. **`make-mips-interpreter`**：纯手写 MIPS 寄存器机与指令集模拟器（自研 Agent 鏖战 50 分钟单步调试满分通过，官方 Pi 超时失败）；
2. **`cancel-async-tasks`**：高并发异步任务优雅取消状态机（自研 Agent 满分通过，官方 Pi 失败）；
3. **`polyglot-c-py`**：编写同时符合 C 语法与 Python 语法的同源文件（自研 Agent 满分通过，官方 Pi 失败）；
4. **`pytorch-model-cli`**：PyTorch 模型 CLI 动态装配推理（自研 Agent 满分通过，官方 Pi 失败）；
5. **`git-multibranch`**：多分支自动化部署与 Hook 脚本调度（自研 Agent 满分通过，官方 Pi 失败）；
6. **`model-extraction-relu-logits`**：深度学习模型网络权重逆向提取（自研 Agent 满分通过，官方 Pi 失败）。

---

## 💡 评测局限性与真实性声明 (Transparency & Disclosures)

实事求是地复盘，本次大规模评测在执行过程中客观存在以下工程局限性与非全量重跑说明：

1. **官方宣称 90% 与本地实测 ~80% 的客观环境鸿沟**：
   DeepSeek-V4.1-Flash 官方报告在相似题集上宣称达 ~90% 通过率，而自研与原厂 Pi 在本地单机环境下均收敛在 80%~84%（71/72 胜）。经归因排查，主要差距来自本地环境的物理瓶颈：
   - 本地 Windows WSL2 缺少 `/dev/kvm` 硬件虚拟化直通（QEMU 纯软件指令模拟开机耗时 25 分钟，导致 `install-windows-3.11`、`qemu-alpine-ssh` 超时）；
   - 本地 Docker 缺少 GPU 硬件算力直通（纯 CPU 跑 Vision Transformer 推理导致 `sam-cell-seg` 超时）；
   - 海外网络专线与依赖高速下载差异。但可以保证的是，**Pi 与 MyPiAgent 在本地的物理沙箱与运行环境是 100% 完全一致的**。
2. **中断单题重跑**：早期部分任务因本地虚拟化配置或网络偶发故障中途崩掉，采取的是单题补跑，未全量推倒重来。
3. **架构优化后未全量推倒重跑**：早期 Harness 上下文压缩机制存在前缀失效问题，在重构为离散 Epoch 块级压缩后，仅对此前未通过的任务进行了重跑攻坚，已通过的任务未全量重跑。
4. **Pi 缓存数据说明**：原厂 Pi 评测时外层脚本未将细分缓存字段结构化沉淀至上层汇总表，故在公开对比中标记为未审计（Unmeasured），仅保留其实测总 Token 消耗。
5. **未全量重跑的经济与时间约束**：89 题全集镜像体积超 120GB，单题耗时最高可达 1 小时且高频消耗 API 预算，出于个人开发者现实成本约束，无法支撑多次全量推倒重测。

---

## 📁 核心评测报告索引

- 📄 [`my_pi_agent_terminal_bench_2.md`](./my_pi_agent_terminal_bench_2.md)：**自研 MyPiAgent 终局战报**（71 道满分赛题英雄榜、18 道未过题目根因归类、41.7M Tokens 详细账单）。
- 📄 [`pi_official_terminal_bench_2.md`](./pi_official_terminal_bench_2.md)：**原厂 Pi 官方 Agent 终局战报**（72 胜官方基准数据）。
- 📄 [`comparison_matrix.md`](./comparison_matrix.md)：**89 题逐题全量横向对比矩阵**（两边详细状态对比、独占胜出分析与技术反思）。
- 📊 [`my_pi_agent_progress.json`](./my_pi_agent_progress.json)：自研 Agent 89 题全量真实机器可读评测进度记录（0 Token 盲区彻底消除）。
- 📊 [`official_pi_progress.json`](./official_pi_progress.json)：原厂 Pi 89 题真实机器可读评测进度记录。
