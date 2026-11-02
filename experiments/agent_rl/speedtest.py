"""多源并行下载测试：找最快的镜像。"""

import os
import time
import urllib.request

FILE = "config.json"
SOURCES = [
    ("hf-mirror", "https://hf-mirror.com/Qwen/Qwen3.5-9B/resolve/main"),
    ("modelscope", "https://modelscope.cn/models/Qwen/Qwen3.5-9B/resolve/master"),
    ("hf-cn", "https://hf-mirror.com/Qwen/Qwen3.5-9B/resolve/main"),
    ("aliyun", "https://modelscope.cn/api/v1/models/Qwen/Qwen3.5-9B/repo?Revision=master&FilePath="),
]


def test(name: str, base: str, fname: str = FILE) -> tuple[str, float, str]:
    url = f"{base}/{fname}" if "FilePath=" not in base else f"{base}{fname}"
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        dt = time.time() - t0
        speed = len(data) / dt / 1024
        return name, speed, f"OK {len(data)}B {speed:.0f}KB/s"
    except Exception as exc:
        return name, 0.0, f"{type(exc).__name__}: {str(exc)[:60]}"


print("=== 小文件测速 ===", flush=True)
for name, base in SOURCES:
    n, speed, msg = test(name, base)
    print(f"{n:12s} {msg}", flush=True)

print("\n=== 大文件测速（前 50MB）===", flush=True)
BIG = "model.safetensors-00001-of-00004.safetensors"
for name, base in SOURCES[:3]:
    url = f"{base}/{BIG}"
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5"})
        with urllib.request.urlopen(req, timeout=30) as r:
            got = 0
            while got < 50 * 1024 * 1024:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        print(f"{name:12s} {got/1e6:.0f}MB in {dt:.1f}s = {got/dt/1e6:.2f} MB/s", flush=True)
    except Exception as exc:
        print(f"{name:12s} FAIL {type(exc).__name__}: {str(exc)[:60]}", flush=True)
