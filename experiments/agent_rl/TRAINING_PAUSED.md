# Agent RL 训练实验：暂停记录

> 暂停时间：2026-11-01
> 原因：本机内存（15.3 GB）/ 显存（8 GB）不足以支撑稳定训练

## 一、已完成

| 项 | 状态 |
| --- | --- |
| 训练环境（E 盘） | ✅ torch 2.11.0+cu128、transformers 5.22、TRL 1.17、PEFT、bitsandbytes 0.50 |
| 模型 | ✅ Qwen3.5-2B（4.3 GB）、Qwen3-1.7B（3.4 GB） |
| SFT 数据集 | ✅ 631 条（从 631 条成功 episode 还原） |
| 小模型 agent 循环 | ✅ `simple_loop.py`（文本 JSON 动作 + 沙盒执行） |
| 动作解析器 | ✅ 处理 `<think>` 块、多 JSON、括号配对 |
| SFT 训练流程 | ✅ 跑通，loss 2.74 → 0.39（约 50 步内） |

## 二、实测数据（Qwen3.5-2B）

| 指标 | 值 |
| --- | --- |
| 4bit 推理显存 | 1.61 GB |
| 生成速度 | 15.4 tok/s |
| 沙盒任务通过率 | **0–1/5（波动）** |
| 动作解析成功率 | **46%**（54% 的步骤无法解析成动作） |

结论：2B 模型格式稳定性太差，直接跑 agent 任务不可用。

## 三、阻塞点

### 1. Qwen3.5-2B 无法训练（架构兼容性）

```
RuntimeError: CUDA error: CUBLAS_STATUS_INTERNAL_ERROR
```

- 出现在线性注意力的反向传播（`torch_chunk_gated_delta_rule`）
- bf16 / fp16 均复现
- 基础 cublas 反向正常，说明是 **Qwen3.5 线性注意力 + Blackwell(sm_120) 的组合问题**
- **结论：Qwen3.5-2B 只能推理，不能训练**

### 2. Qwen3-1.7B 训练不稳定（资源限制）

```
torch.AcceleratorError: CUDA error: unknown error
```

- 训练约 50 步后崩溃，loss 已降到 0.39
- 同时伴随 `OSError: 页面文件太小 (os error 1455)`（加载阶段，偶发）
- `expandable_segments not supported on this platform`（Windows 限制）
- **结论：能启动、能学习，但撑不住完整训练**

### 3. 环境相关（可绕过）

| 问题 | 绕过方式 |
| --- | --- |
| 页面文件不足 | `device_map="cuda:0"` 字符串（别用 dict） |
| 内存占满 | 训练前关闭 ChatGPT / 浏览器 |
| 序列过长 OOM | `--max-len 768~1024`、`--batch 1` |

## 四、当前文件位置

```
E:\agent-rl\
├── .venv\            4.7 GB   训练环境
├── models\
│   ├── Qwen3-1.7B\   3.4 GB   ← 可训练
│   └── Qwen3.5-2B\   4.3 GB   ← 只能推理
├── datasets\
│   └── sft_train.jsonl        631 条 SFT 样本
└── outputs\
    └── sft-lora\              已跑通的 LoRA 权重（10 步验证版）
```

代码在仓库 `experiments/agent_rl/`：

| 文件 | 作用 |
| --- | --- |
| `build_sft.py` | 从 episode 构造 SFT 数据 |
| `sft_train.py` | LoRA SFT 训练 |
| `simple_loop.py` | 小模型 agent 循环 + 动作解析 |
| `probe.py` | 用本地模型跑沙盒任务测通过率 |
| `qwen_client.py` | 接入仓库 harness 的适配器（未使用） |

## 五、恢复实验的前提

| 资源 | 最低要求 |
| --- | --- |
| 显存 | **12 GB+**（8 GB 训练 2B 模型太紧） |
| 内存 | **32 GB**（15 GB 下页面文件反复告警） |
| 模型 | 用 Qwen3-1.7B（标准架构），不要 Qwen3.5（线性注意力训练有 bug） |

## 六、下一步（有资源后）

1. **SFT 完整跑完**（400 步，约 20 分钟）：预期格式解析率从 46% 提到 90%+
2. **测 SFT 后收益**：同一批任务，SFT 前 vs SFT 后 pass@1
3. **GRPO**：用 TRL 的 `GRPOTrainer`，奖励 = 沙盒判定结果
4. **对比**：base / SFT / SFT+GRPO 三组 pass@1 和 pass^k

## 七、不影响已完成成果

**Agent RL 的数据与评测基建仍然完整有效**：

- 115 个任务、1108 次真实沙盒执行
- pass^k / pass@k / gap 指标
- 7 种策略对比
- **信息可得性实验**：100% → 60% → 0% 的复现曲线

这些成果和报告（`results/REPORT.md`、`results/INFORMATION.md`）不受训练暂停影响。
