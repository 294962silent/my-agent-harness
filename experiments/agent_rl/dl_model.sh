#!/bin/bash
# 逐文件下载（更可控），失败自动重试
export PATH=/root/miniconda3/bin:$PATH
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/root/autodl-tmp/cache
export HF_HUB_DISABLE_XET=1

DEST=/root/autodl-tmp/models/Qwen3.5-9B
mkdir -p "$DEST"
BASE=https://hf-mirror.com/Qwen/Qwen3.5-9B/resolve/main

echo "=== 下载小文件 $(date) ==="
for f in config.json generation_config.json tokenizer_config.json vocab.json merges.txt chat_template.jinja model.safetensors.index.json; do
  [ -f "$DEST/$f" ] && { echo "skip $f"; continue; }
  curl -sL --retry 5 --retry-delay 3 -m 300 -o "$DEST/$f" "$BASE/$f" && echo "ok $f" || echo "fail $f"
done

echo "=== 下载分片 $(date) ==="
for i in 1 2 3 4; do
  f=$(printf "model.safetensors-%05d-of-00004.safetensors" $i)
  [ -f "$DEST/$f" ] && { echo "skip $f"; continue; }
  echo "开始 $f"
  curl -L --retry 10 --retry-delay 5 --retry-all-errors -C - -m 3600 \
       -o "$DEST/$f" "$BASE/$f" 2>&1 | tail -1
  ls -lh "$DEST/$f" 2>/dev/null | awk '{print "  ->", $5}'
done

echo "=== 下载完成 $(date) ==="
du -sh "$DEST"
ls "$DEST" | head -20
