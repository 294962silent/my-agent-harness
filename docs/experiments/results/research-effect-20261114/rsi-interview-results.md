# Dream-RSI 真实实验：面试可读结果

## 结论先行

本轮完成了 **7/8 个 test final 臂**。AutomPG 的四个原始臂全部完成；QSAR 完成 fixed、random、greedy，frozen 臂在第三个候选达到 1,200 秒边界后出现 provider 用量未知，因此按 fail-closed 停止且未重放。

训练/晋升结论是 **不可识别**：Wine holdout 在第二轮出现 provider unknown；优化器没有运行。于是 `trained-frozen` 显式回退到 fixed-reference 的 `2/1` 参数，而不是伪造一个已学习参数。

## 已完成的 test 结果

| 数据集 | 臂 | round dev RMSE | final RMSE | final / 均值基线 | requests | tokens | evals | active sec |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| QSAR | fixed-reference | 1.759991 | 1.432272 | 1.0000 | 12 | 136655 | 5 | 1154.5 |
| QSAR | random | 1.759991 | 1.432272 | 1.0000 | 12 | 166542 | 5 | 1154.6 |
| QSAR | fixed-greedy | 1.759991 | 1.432272 | 1.0000 | 4 | 26650 | 3 | 241.6 |
| AutomPG | fixed-reference | 2.574058 | 2.515738 | 0.3516 | 12 | 141057 | 5 | 979.1 |
| AutomPG | random | 2.778874 | 2.437353 | 0.3407 | 12 | 137147 | 5 | 1151.4 |
| AutomPG | trained-frozen（fixed fallback） | 2.724798 | 2.495291 | 0.3488 | 12 | 128325 | 5 | 1067.6 |
| AutomPG | fixed-greedy | 8.041614 | 7.154405 | 1.0000 | 4 | 23482 | 3 | 207.4 |

QSAR 的三个已完成臂最终都保留均值基线。AutomPG 中 fixed、random、frozen fallback 都显著优于均值基线；random 的 final RMSE 最低，但“frozen”与 fixed 使用相同参数，二者差异只能说明采样波动，不能证明训练收益。greedy 的单一候选无效，最终保留基线。

## 训练与故障边界

- Airfoil：两轮均来自旧 v3 已提交历史，只导入、不重跑。
- Yacht round0：旧 baseline，RMSE `16.662447`；compat2 已结算 12 requests / 130458 tokens / 3 evals。
- Yacht round1：compat3 新跑 12 requests / 128344 tokens / 3 evals / 966s。两个候选 known-invalid，最终保留 baseline，RMSE `16.662447`。
- Wine：compat3 新跑 10 requests / 3 evals / 1200.05s；request #10 超时，花费 112662 known tokens，另保留 27818 reservation。provider usage 未知后立即停止，round 未提交，旧 attempt 未重放。
- 优化器：因没有完整 Wine holdout trace，显式不运行；`promoted=false`。
- QSAR frozen：compat5 新跑 12 requests / 3 evals / 1200.05s；最后一个请求超时，保留 149017 known tokens 和 87839 reservation，无 final。

## 成本与观测

最终总账持有 **151 requests / 1,798,894 accounting units / 56 evaluations / 12,776.24 active seconds**，其中历史未知请求 2 个、compat3/compat5 新增未知请求 1 个，共 **3 个 unknown requests**。Known settled tokens 为 `1,655,475`；unknown 实际 token 与 USD 保持 unavailable，**不把 reservation 冒充账单**。

所有 compat3、compat4、compat5、compat6、compat7 模型调用都走 required Langfuse raw owner；元数据回读窗口为 180 秒，只读观测 API 使用有界指数退避。模型请求 retry 固定为 0；任何 provider unknown 都停止后续派发。旧调用没有任何重放。

compat2 的 14 个调用在 STOP 后只读回读全部匹配；本批没有再上传历史内容。Langfuse 仅记录元数据与 usage，不宣称保存模型正文或 gold。

## 证据与复现

- compat3 协议：`aeb6fd636029cf7459ed76a7b0aff797acb06f9a7fc77faa60dd232a7fe2adef`
- compat4：`e0a5c0c4d31bf21c4e9e1a245ac49965ac0fc29798df05bf7f52cd57e93e0f17`
- compat5：`f1ff2264be620682949275f88d53c9d28e6f8e613613c21cf54a762d39564762`
- compat6：`52b5fab0ce456e099e90711b9ab897b2513ef4784ecc02d1304f50e680679dab`
- compat7：`361e9cd267dabe6810a40e0863c270c91f58e685c8e4402cce3cae5c010f016a`

机器可读成绩与故障状态见 [rsi-compat7-summary.json](rsi-compat7-summary.json)。私有运行目录保留逐请求 journal、calls、evaluation artifacts、command evidence 和 STOP；公开摘要不包含凭据、模型正文或 gold。

## 适用边界

这是仅两个 test 数据集、单 seed、单次真实运行的小样本。不能据此宣称 RSI 普遍优于 fixed/random，也不能把 frozen fallback 与 fixed 的采样差异说成学习效果。Wine/optimizer 缺失和三个 unknown requests 是本轮最关键的残余风险。
