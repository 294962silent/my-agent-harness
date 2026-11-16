# 静态预算可见性：三题诊断对照

日期：2026-11-12。结果为 **on 1/3、off 1/3**，四次空补丁。此面板没有提供静态预算提示改善成功率的证据；保留为可选功能，默认关闭。

## 范围与修改前后

此前v2的7次空补丁最支持未进入编辑，但没有逐步差异记录；系统要求编辑，却不告知16次API上限。本轮新增初始静态预算提示、每shell响应后的宿主只读工作树计数，以及独立的隐藏日志回读探针。没有增加请求上限、编辑策略或强制回读指令。

本报告的 **on/off表示预算提示**，两组artifact_projection都为on。旧v2的on/off表示产物投影，两个实验不能合并排名。三题按已知症状选择，详见[模型前选择记录](SELECTION.md)，不是未见题或代表性抽样；每题每条件仅一次，seed23只固定顺序，不控制服务端采样。

生成源码：`c69ff37ed148f53d526b458b9b36233860b85f26`。模型前[CI run43241787757](https://github.com/294962silent/my-agent-harness/actions/runs/43241787757)四组成功。任务源仍为SWE-bench/swe-bench-tasks@`3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，复用原catalog、镜像及成功gold绑定；模型只见安全metadata，不见gold/test patch、hints或完整evaluator dataset。原许可证和预检证据保留在[上批档案](../swe-v2-pilot-20261112/)。

端点为OpenCode Go兼容接口，实际请求模型deepseek-v4.1-flash；温度0.7、每trial 16请求（含辅助）、4096输出上限、600秒。两组工具及宿主观察相同，唯一提示差异为初始system的一句真实请求上限信息，运行中不改前缀。全部CPU/WSL Docker执行，没有使用L20/GPU或DSH。

## 实际结果

- `pytest-dev__pytest-5227`：先off后on，两组官方resolved，均实际执行测试。首次“响应后可见工作树差异”分别在第14、11次API请求后；该指标包括未跟踪文件，不等同首次源码编辑。
- `sympy__sympy-11400`：先on后off，两组官方empty_patch，未执行测试，所有响应后观测均没有可见差异。
- `pytest-dev__pytest-5103`：先on后off，两组官方empty_patch，未执行测试，所有响应后观测均没有可见差异。

共6个已知官方结果、3个完整配对、2次真实测试执行、2次resolved、4次empty_patch，环境blocked和未知结果均0。六trial各16请求且终态均budget_exhausted，不能仅凭共同终态认定原因是预算不足。

工作树观测100/100 available，两条件各50条；观察/持久化失败0。四条空补丁轨迹没有修改保留到响应后的差异，削弱“宿主最终提取遗漏”的解释；不能排除单个命令内部修改后撤回。没有保存模型推理来解释其动机。

## 费用、产物和用量覆盖

- off：48请求，完整输入337003 token、输出39583，保守账本$0.148623；8份产物/75999B，read/search响应0/0。
- on：48请求，完整输入403012 token、输出31372，保守账本$0.158567；15份产物/140052B，read/search响应0/0。
- SWE合计96请求、输入740015、输出70955，费用$0.307190，所有请求usage已结算。normalized prompt字段单独保留，未把它误当全部输入。
- cache_read仅off47/48、on45/48，总92/96覆盖；cache_write为0/96覆盖（缺失，不是观测到零）。完整缓存总量为null，不给任务级KV命中率排名。

两组实际模型轨迹、输入及产物数量不同，不能把费用差当成提示的稳定成本效果。工具响应计数来自canonical Session元数据，覆盖6/6；Session只留hash/大小、产物只留数量/字节/hash，不公开正文，也不称为wire抓包。原SWE任务未要求必须查隐藏中段，0回读不能独立证明接口缺陷。

独立[回读探针](../artifact-capability-20261112/README.md)另计12请求/$0.009106：on两次均回读到目标，但四请求内最终答对0/2；off答对2/2。它支持生产链路可调用，不支持端到端收益，不并入SWE成功率。

原账本此前192请求/$0.572059；加probe和SWE，本阶段新增108请求/$0.316296，累计300请求/$0.888355（300是请求数，不是300道SWE题）。原总政策$30不重置；probe新增上限$0.25，随后SWE另加$1.75、绝对ceiling$2.331165。所有数字为按高峰冷价逐请求向上取微美元的客户端保守记账，未核供应商账单。没有使用剩余额度补题或重试。

## 可复核证据

[raw/manifest.json](raw/manifest.json) SHA256：`18888467ad123eb0b70f850e40ab1d2c93fa974f1fcefd17a02e02a2ba4883dd`。60个列入文件，含manifest共61；采集前验证148项冻结输入、100项原结果，108个runtime .py另核Git规范化blob与生成提交一致。原字节hash也同时验证，允许Git换行规范，不忽略实际内容差异。manifest是本地批准锚点，不是数字签名。

SWE plan SHA：`89377797e869d1ef662a7fd298c5ca91e8acd60bc726e9eaac611ebf1d28f1f5`；probe plan SHA：`b0f50a8e40bfd33c65edda3800e3020ce0b2879e6a7528e763bf21aeec03f366`。生成与事后分析分开，parser为swebench-5.0.2-outcome-v1，SHA为`c690d265d9380331cfeb54cc702d4b90f6e9807b7f03cbd9dbd71e07501fd3a5`。旧v2 raw保持不变。

无需模型、凭据、Docker或原宿主绝对路径即可重算：

```sh
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_results \
  --archive docs/experiments/results/swe-budget-visibility-20261112/raw \
  --manifest-sha256 18888467ad123eb0b70f850e40ab1d2c93fa974f1fcefd17a02e02a2ba4883dd
python docs/experiments/results/swe-v2-pilot-20261112/analysis/recompute_metrics.py \
  --raw-dir docs/experiments/results/swe-budget-visibility-20261112/raw \
  --expected-manifest-sha256 18888467ad123eb0b70f850e40ab1d2c93fa974f1fcefd17a02e02a2ba4883dd
```

第二条复用前批的通用stdlib账本解析器，不修改旧档案；其historical表示不属于本SWE plan的全部204请求，包含本次probe，细分在raw/summary.json。主代理已独立重算，与[analysis/outcomes.json](analysis/outcomes.json)及[analysis/ledger-metrics.json](analysis/ledger-metrics.json)一致；archive_modified=false。

## 设计价值、代价与下一步边界

借鉴DSH的稳定前缀组织方式，提示在初始system静态追加；借鉴oh-my-pi/Hermes的受限产物回读思路，用隔离探针验证必要回读。本轮不运行DSH对照，也不借用其他Harness分数。新增factor解耦、观测覆盖和可复算档案改善的是工程可解释性；宿主Git观察与额外回读都有开销。

默认行为继续none。面板上的空补丁问题仍存在，不能说已经解决，也不能由三题推断静态提示永远无效。本批不扩成完整SWE，不接入Mem0、Dream-RSI或Langfuse远端；后续若研究交付策略或步数，需要另立单变量计划并保留本批失败。
