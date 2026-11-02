#!/bin/bash
# 关闭 xet 传输，回退到普通 HTTP 下载
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache
export HF_HUB_DISABLE_XET=1

cd /root
echo "=== 环境 ==="
echo "HF_ENDPOINT=$HF_ENDPOINT"
echo "HF_HUB_DISABLE_XET=$HF_HUB_DISABLE_XET"

echo "=== 启动验证 $(date) ==="
python -u verify_trainable.py Qwen/Qwen3.5-9B 2>&1 | grep -vE "^(Loading|Fetching|Downloading|\s+[0-9]+%)" | tail -40
echo "=== 结束 $(date) ==="
