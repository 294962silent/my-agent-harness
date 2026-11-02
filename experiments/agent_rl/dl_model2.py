#!/bin/bash
# 用 Python requests 逐文件下载，带进度与超时重试
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com

DEST=/root/autodl-tmp/models/Qwen3.5-9B
mkdir -p "$DEST"

python -u - <<'PY'
import os, sys, time
import urllib.request

DEST = "/root/autodl-tmp/models/Qwen3.5-9B"
BASE = "https://hf-mirror.com/Qwen/Qwen3.5-9B/resolve/main"

FILES = [
    "vocab.json",
    "merges.txt",
    "tokenizer.json",
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "tokenizer_config.json",
    "model.safetensors-00001-of-00004.safetensors",
    "model.safetensors-00002-of-00004.safetensors",
    "model.safetensors-00003-of-00004.safetensors",
    "model.safetensors-00004-of-00004.safetensors",
    "model.safetensors.index.json",
]


def download(name: str, attempts: int = 5) -> bool:
    path = os.path.join(DEST, name)
    url = f"{BASE}/{name}"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                # 已有文件大小
                have = os.path.getsize(path) if os.path.exists(path) else 0
                if total and have == total:
                    print(f"skip {name} ({have/1e6:.1f}MB)", flush=True)
                    return True
                mode = "ab" if 0 < have < total else "wb"
                if mode == "ab":
                    req2 = urllib.request.Request(url, headers={"User-Agent": "curl/8", "Range": f"bytes={have}-"})
                    resp = urllib.request.urlopen(req2, timeout=60)
                written = have
                with open(path, mode) as fh:
                    last = time.time()
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                        if time.time() - last > 5:
                            pct = 100 * written / total if total else 0
                            print(f"  {name}: {written/1e6:.0f}/{total/1e6:.0f}MB ({pct:.0f}%)", flush=True)
                            last = time.time()
            size = os.path.getsize(path)
            if total and size < total:
                print(f"  {name} 不完整 {size}/{total}", flush=True)
                continue
            print(f"ok {name} ({size/1e6:.1f}MB)", flush=True)
            return True
        except Exception as exc:
            print(f"  {name} 第{attempt}次失败: {type(exc).__name__} {str(exc)[:80]}", flush=True)
            time.sleep(3)
    return False


for name in FILES:
    ok = download(name)
    if not ok:
        print(f"FAIL {name}", flush=True)
PY

echo "=== 结果 ==="
du -sh "$DEST"
ls -lh "$DEST" | tail -15
