"""逐文件下载 Qwen3.5-9B（带进度、断点续传、超时重试）。"""

import os
import time
import urllib.request

DEST = "/root/autodl-tmp/models/Qwen3.5-9B"
BASE = "https://hf-mirror.com/Qwen/Qwen3.5-9B/resolve/main"

FILES = [
    "model.safetensors-00001-of-00004.safetensors",
    "model.safetensors-00003-of-00004.safetensors",
    "model.safetensors-00004-of-00004.safetensors",
    "model.safetensors.index.json",
    "vocab.json",
    "merges.txt",
    "tokenizer.json",
    "chat_template.jinja",
    "config.json",
    "tokenizer_config.json",
]


def download(name: str, attempts: int = 6) -> bool:
    os.makedirs(DEST, exist_ok=True)
    path = os.path.join(DEST, name)
    url = f"{BASE}/{name}"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5"})
            resp = urllib.request.urlopen(req, timeout=60)
            total = int(resp.headers.get("Content-Length", 0) or 0)
            have = os.path.getsize(path) if os.path.exists(path) else 0
            if total and have == total:
                print(f"skip {name} ({have/1e6:.1f}MB)", flush=True)
                return True
            if total and 0 < have < total:
                req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5", "Range": f"bytes={have}-"})
                resp = urllib.request.urlopen(req, timeout=60)
                mode = "ab"
            else:
                have = 0
                mode = "wb"
            written = have
            last = time.time()
            with open(path, mode) as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if time.time() - last > 10:
                        pct = 100 * written / total if total else 0
                        print(f"  {name}: {written/1e6:.0f}/{total/1e6:.0f}MB ({pct:.0f}%)", flush=True)
                        last = time.time()
            size = os.path.getsize(path)
            if total and size < total:
                print(f"  {name} 不完整 {size}/{total}，重试", flush=True)
                continue
            print(f"ok {name} ({size/1e6:.1f}MB)", flush=True)
            return True
        except Exception as exc:
            kind = type(exc).__name__
            if "404" in str(exc):
                print(f"  {name} 不存在，跳过", flush=True)
                return True
            print(f"  {name} 第{attempt}次失败: {kind} {str(exc)[:80]}", flush=True)
            time.sleep(3)
    return False


failed = []
for name in FILES:
    if not download(name):
        failed.append(name)

print("=== 结果 ===", flush=True)
print("失败:", failed if failed else "无", flush=True)
total_size = sum(
    os.path.getsize(os.path.join(DEST, f)) for f in os.listdir(DEST) if os.path.isfile(os.path.join(DEST, f))
)
print(f"总计: {total_size/1e9:.2f} GB", flush=True)
