#!/bin/bash
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache

echo "=== 当前 brotli 版本 ==="
pip list 2>/dev/null | grep -iE "brotli"

echo "=== 测试 brotli 是否支持 output_buffer_limit ==="
python - <<'PY'
try:
    import brotli
    d = brotli.Decompressor()
    print("brotli 版本:", getattr(brotli, "__version__", "?"))
    sig = d.process.__doc__ or ""
    print("process 支持 output_buffer_limit:", "output_buffer_limit" in sig or "unknown")
    try:
        d.process(b"", output_buffer_limit=1024)
        print("调用成功")
    except TypeError as e:
        print("调用失败:", e)
except Exception as e:
    print("brotli 不可用:", type(e).__name__, e)
PY

echo "=== 升级 brotli ==="
pip install -q -U -i https://pypi.tuna.tsinghua.edu.cn/simple brotli 2>&1 | tail -2
pip list 2>/dev/null | grep -iE "brotli"

echo "=== 再次测试 ==="
python - <<'PY'
import brotli
d = brotli.Decompressor()
print("brotli:", getattr(brotli, "__version__", "?"))
try:
    d.process(b"", output_buffer_limit=1024)
    print("✅ 支持 output_buffer_limit")
except TypeError as e:
    print("❌ 不支持:", e)
PY
