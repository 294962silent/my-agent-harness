#!/bin/bash
export PATH=/root/miniconda3/bin:$PATH

echo "=== httpx2 装在哪 ==="
pip show httpx2 2>/dev/null | head -8

echo "=== 谁依赖 httpx2 ==="
pip show httpx2 2>/dev/null | grep -i required-by

echo "=== 所有引用 httpx2 的包 ==="
grep -rl "httpx2" /root/miniconda3/lib/python3.12/site-packages/*/METADATA 2>/dev/null | head -10

echo "=== site-packages 里的 httpx2 ==="
ls -d /root/miniconda3/lib/python3.12/site-packages/httpx2* 2>/dev/null

echo "=== 强制删除 httpx2 目录 ==="
rm -rf /root/miniconda3/lib/python3.12/site-packages/httpx2 \
       /root/miniconda3/lib/python3.12/site-packages/httpx2-*.dist-info
echo "删除完成"

echo "=== 验证 httpx2 不可导入 ==="
python -c "import httpx2" 2>&1 | head -2

echo "=== httpx 版本 ==="
python -c "import httpx; print('httpx', httpx.__version__)"

echo "=== 下载测试 ==="
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache
python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download("Qwen/Qwen3.5-9B", "config.json", cache_dir="/root/autodl-tmp/cache")
print("OK:", p)
PY
