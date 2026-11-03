# 服务器连接方法（重要）

## 问题

之前用 `sshpass` 连接 AutoDL 服务器一直失败：

```
sshpass -p '<password>' ssh -p 19015 root@connect.bjb1.seetacloud.com
→ Permission denied (publickey,password).
```

**误判为「密码失效」，实际是 `sshpass` 的问题。**

## 原因

环境里装的是 **winget 的 Windows 移植版 `sshpass.exe`**
（`xhcoding.sshpass-win32`），
它在 Git Bash 下无法正确把密码注入 ssh 的交互式提示。

端口是通的、密码是对的，只是密码没被送达。

## 正确的连接方式：`SSH_ASKPASS`

```bash
# 1. 创建密码脚本
cat > /tmp/askpass.sh <<'SCRIPT'
#!/bin/bash
echo 'YOUR_PASSWORD'
SCRIPT
chmod +x /tmp/askpass.sh

# 2. 用它连接
export SSH_ASKPASS=/tmp/askpass.sh
export SSH_ASKPASS_REQUIRE=force

ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -o NumberOfPasswordPrompts=1 \
    -p 19015 root@connect.bjb1.seetacloud.com "echo OK"
```

`SSH_ASKPASS_REQUIRE=force` 是关键——强制 ssh 从脚本读密码，
不需要 tty（Git Bash 下没有 `setsid`，所以不能走传统 askpass 路径）。

## 上传/下载文件

```bash
export SSH_ASKPASS=/tmp/askpass.sh SSH_ASKPASS_REQUIRE=force
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o NumberOfPasswordPrompts=1 \
    -P 19015 local_file.py root@connect.bjb1.seetacloud.com:/root/
```

注意是 **`-P`**（大写）指定端口，不是 `-p`。

## 服务器环境

| 项 | 值 |
| --- | --- |
| GPU | RTX 4080 SUPER 32GB（sm_89） |
| Python | `/root/miniconda3/bin/python`（**不在 PATH 里**） |
| torch | 2.8.0+cu128 |
| transformers | 5.22.1 |
| trl | 1.17.1 |
| 数据盘 | `/root/autodl-tmp` |

| 路径 | 内容 |
| --- | --- |
| `/root/autodl-tmp/models/Qwen3.5-9B` | 基础模型 |
| `/root/autodl-tmp/outputs/sft-9b` | SFT adapter（已完成） |
| `/root/autodl-tmp/datasets/grpo_prompts.jsonl` | 631 条 GRPO prompt |
| `/root/autodl-tmp/datasets/sft_train.jsonl` | 631 条 SFT 数据 |

## 教训

**「连接失败」不等于「密码失效」。**
在 Windows 上做 SSH 自动化，先怀疑工具兼容性，再怀疑凭据。
