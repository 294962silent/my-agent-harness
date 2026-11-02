#!/bin/bash
# 稳定的下载 + 验证：断点续传，进度可查
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache
export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0

cd /root
echo "=== 开始下载 $(date) ==="

python -u - <<'PY'
import os, time
from huggingface_hub import snapshot_download

t0 = time.time()
path = snapshot_download(
    "Qwen/Qwen3.5-9B",
    local_dir="/root/autodl-tmp/models/Qwen3.5-9B",
    max_workers=2,
    resume_download=True,
)
print(f"下载完成: {path} ({time.time()-t0:.0f}s)", flush=True)
PY

echo "=== 下载结束 $(date) ==="
ls -lh /root/autodl-tmp/models/Qwen3.5-9B/ 2>/dev/null | head -5

echo "=== 开始反向传播验证 $(date) ==="
python -u verify_trainable.py /root/autodl-tmp/models/Qwen3.5-9B 2>&1 | grep -vE "^(Loading|Fetching|Downloading|\s+[0-9]+%)" | tail -30
echo "=== 全部结束 $(date) ==="
