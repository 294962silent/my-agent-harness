#!/bin/bash
export PATH=/root/miniconda3/bin:$PATH

echo "=== SFT 训练状态 ==="
tail -6 /root/sft_full.log 2>/dev/null
echo ""
echo "=== GPU ==="
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader
echo ""
echo "=== 进程数 ==="
ps aux | grep sft_server | grep -v grep | wc -l
echo ""
echo "=== 错误检查 ==="
grep -iE "error|traceback|exception" /root/sft_full.log 2>/dev/null | tail -3 || echo "无错误"
