#!/bin/bash
# 用更保守的配置重启 SFT：降低显存，加快速度
export PATH=/root/miniconda3/bin:$PATH

echo "=== 停止当前训练 ==="
ps aux | grep sft_server | grep -v grep | awk '{print $2}' | xargs -r kill
sleep 3
nvidia-smi --query-gpu=memory.used --format=csv,noheader

echo "=== 重启：batch 1, seq 1536, steps 200 ==="
cd /root
setsid nohup python -u sft_server.py \
    --steps 200 --batch 1 --accum 8 --max-len 1536 \
    > /root/sft_full.log 2>&1 < /dev/null &
sleep 60
tail -5 /root/sft_full.log
echo "--- GPU ---"
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader
