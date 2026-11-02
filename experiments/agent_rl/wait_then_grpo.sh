#!/bin/bash
# 等 SFT 完成后自动测 GRPO 3 步（验证脚本可运行）
export PATH=/root/miniconda3/bin:$PATH
cd /root

echo "=== 等待 SFT 完成 ==="
while ps aux | grep -q "[s]ft_server"; do
    sleep 30
done
echo "SFT 已结束"
grep -E "SFT_DONE|峰值|完成" /root/sft_full.log | tail -3
du -sh /root/autodl-tmp/outputs/sft-9b 2>/dev/null

echo ""
echo "=== 测试 GRPO 3 步 ==="
python -u grpo_train.py --steps 3 --num-generations 2 --batch 1 > /root/grpo_test.log 2>&1
echo "exit=$?"
tail -15 /root/grpo_test.log
