# Memory 2026-11-14：实验时源码快照

本目录保存完成新十二题实验时的精确研究源码字节。实验后修复了 prepare 的可变账本路径守卫，当前 runner 的 SHA 因此不同；旧 plan、结果与旧 source pins 保持原值。

13 项 source pins 中，8 项可从 Git 基线 `0b63386a4800a16611ed03fa3a831ff7952b23bb` 直接取回；另 5 项的实验时字节不在该基线，也未出现在本机已知 remote-tracking 历史，故仅为这 5 项保存 `files/<原路径>`。逐文件路径、字节数、SHA-256、取得方式及基线差异均在 [manifest.json](manifest.json)。核对范围是当前本地 remote-tracking refs，未重新联网 fetch。

基线的取得方式为 `git show <base_commit>:<原路径>`。在独立 checkout 中恢复时，先检出该基线，再按 manifest 的 snapshot_path 将五个文件复制回 original_path；逐项重算 SHA-256 后应与全部 13 项 pins 一致。不要将历史快照覆盖到正在开发的工作树。

历史 runner SHA：`8792a9ae16985c20318521ee3382a7be67238f609c8a3628bc8bde0de8e57d05`。已完成 v3 plan SHA：`e45e2ca7d9453787136243a9779b438760400a5461161199683d5c96b1a39727`。当前 runner 的后续守卫覆盖 ledger 本身、`.lock`、`.tokens.lock`、`.study.lock`；其修复与 24 项离线回归属于实验结束后的工程改动，不能回写成旧实验的源码版本。

快照仅含通用研究源码；原始问答、历史、gold、模型答案、数据文件、session、凭据、认证桥和私有账本均不在本目录。公开结果目录另有匿名计数及 SHA 清单。完整效果重跑仍需要固定的数据和模型、私有参考隔离及新的累计预算授权；本目录不启动模型或网络请求。
