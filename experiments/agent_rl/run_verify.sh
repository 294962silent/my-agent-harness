#!/bin/bash
# 关健验证：Qwen3.5-9B 在 sm_89 (4080) 上能否反向传播
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache

cd /root
echo "=== 启动验证 $(date) ==="
python -u verify_trainable.py Qwen/Qwen3.5-9B 2>&1 | grep -vE "^(Loading|Fetching|Downloading)" | tail -40
echo "=== 结束 $(date) exit=$? ==="
