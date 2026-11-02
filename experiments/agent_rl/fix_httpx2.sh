#!/bin/bash
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache

echo "=== 重装 httpx2（换版本避开 zstd bug）==="
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple 'httpx2==2.15.0' 2>&1 | tail -3
echo "--- 或退而求其次 ---"
pip list 2>/dev/null | grep -iE "^(httpx|huggingface)"

echo "=== 下载测试（关闭 xet / zstd 路径）==="
python - <<'PY'
import os
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
from huggingface_hub import hf_hub_download
try:
    p = hf_hub_download("Qwen/Qwen3.5-9B", "config.json", cache_dir="/root/autodl-tmp/cache")
    print("OK:", p)
except Exception as e:
    print("FAIL:", type(e).__name__, str(e)[:300])
PY
