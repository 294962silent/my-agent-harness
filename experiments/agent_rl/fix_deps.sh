#!/bin/bash
set -e
export PATH=/root/miniconda3/bin:$PATH

echo "=== 当前版本 ==="
pip list 2>/dev/null | grep -iE "^(httpx|huggingface)" || true

echo "=== 修复依赖 ==="
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple 'httpx<0.28' 'huggingface_hub>=0.26,<1.0'

echo "=== 修复后版本 ==="
pip list 2>/dev/null | grep -iE "^(httpx|huggingface)" || true

echo "=== 测试连通性 ==="
export HF_ENDPOINT=https://hf-mirror.com
python - <<'PY'
from huggingface_hub import hf_hub_download
try:
    p = hf_hub_download("Qwen/Qwen3.5-9B", "config.json", cache_dir="/root/autodl-tmp/cache")
    print("下载成功:", p)
except Exception as e:
    print("下载失败:", type(e).__name__, str(e)[:300])
PY
