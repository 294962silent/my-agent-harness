#!/bin/bash
export PATH=/root/miniconda3/bin:$PATH

echo "=== 检查 zstd 解压入口 ==="
sed -n '180,200p' /root/miniconda3/lib/python3.12/site-packages/httpx2/_decoders.py

echo "=== 检查 zstandard 版本 ==="
python -c "import zstandard; print('zstandard', zstandard.__version__)" 2>&1 | head -2
python -c "import zstd; print('zstd ok')" 2>&1 | head -2
