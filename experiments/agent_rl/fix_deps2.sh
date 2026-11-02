#!/bin/bash
set -e
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache

echo "=== 卸载冲突的 httpx2 ==="
pip uninstall -y -q httpx2 2>/dev/null || true

echo "=== 恢复 huggingface_hub 到 transformers 5.22 要求 >=1.31 ==="
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple 'huggingface_hub>=1.31,<3.0'

echo "=== 版本确认 ==="
pip list 2>/dev/null | grep -iE "^(httpx|huggingface)" || true

echo "=== 下载测试 ==="
python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download("Qwen/Qwen3.5-9B", "config.json", cache_dir="/root/autodl-tmp/cache")
print("OK:", p)
PY
