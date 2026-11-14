# 效果验证 v1：SOL-2 第0阶段与新 Memory 冻结

2026-11-14。本阶段不运行 API 或 embedding，不启动 Qwen/BGE 模型，不修改共享日志、研究生产代码或正在运行的校准入口。Memory runner 已落盘，**prepare/verify 已实际通过；retrieve/paid/summarize 尚未测试或正式执行，不能据此启动72请求整批实验。**

## 1. SOL-1 校准入口交叉只读 review

审查 `experiments/research_validation/calibration.py` 与 `tests/experiments/test_research_validation.py`，未重跑主代理已通过的8项测试。Spec：8候选迭代、16384输出、64000上下文、180秒请求、父20请求/400000tokens/1200活跃秒/4评分、width1/refine1、一轮最多2候选、memory none，符合主代理最终批准配置。

Quality：prepare/run 直接调用生产 ResearchTask/ResearchCampaign/research_cli；拒绝既存task/campaign根；任何prepared之外事件阻断再次run；没有final/resume命令，未知用量停止继承生产runtime。report 分别计算正常终止、预测有效、代码hash变化和完整trace，未以结束文本代替数值评分。未发现阻断启动的问题。wrapper并不把model/provider/base_url写死为常量，而由生产manifest固定并验请求配置；操作者应核实际manifest为批准的DeepSeek和原端点。未修改正在使用的入口。未发现必须更正的report metadata读取错误。

## 2. 新 Memory 样本和现有接口

旧入口 `experiments/longmemeval/harness_ablation.py:prepare_plan/verify_plan` 固定6类型×5题；`harness_corpus.select_records` 用seed23按类型选题；`harness_retrieval.retrieve_question` 直连生产MemoryStore、create_mem0_index(local_cpu_config)，同一800字符历史块、top8、6000字符。`harness_paid.paid_phase` 复用生产BudgetLedger/GuardedLLM，回答/判分分别1请求；judge复用官方prompt但模型为DeepSeek，只接受正文yes/no，非法/unavailable不重试。

新 `experiments/memory_validation/runner.py` 复用这些生产组件/语料/判分模板，另建12题协议，不改旧入口的30题约束。新题按6类型各2题、seed24，先排除旧30题，有suffix `_abs` 的类型先选1题，其余普通题；只看类型与ID，不看结果。旧排除源为已提交的 `docs/experiments/results/long-horizon-20261113/memory-v2/rows.json`，SHA256 `5158a8a796c13cbe1da0303b5c52a2788d687c7c0094780eb1e3de6861b0c96e`。

实际新plan位于主仓库私有 `.git/codex-delivery/effect-validation-20261114/memory-v1/plan`，plan SHA256 **`b127a3faae8d10a4a40ec004f7cfc8cc60f9ef8bf7e7b9b049a924b3325563a3`**。安全投影见 [sample-v1.json](../../../experiments/memory_validation/sample-v1.json)。12个ID与旧30题交集为0：

- knowledge-update：5831f84d、6aeb4375_abs
- multi-session：2e6d26dc、6456829e_abs
- single-session-assistant：352ab8bd、488d3006
- single-session-preference：0a34ad58、b6025781
- single-session-user：19b5f2b3_abs、c8c3f81d
- temporal-reasoning：gpt4_85da3956、gpt4_93159ced_abs

数据原文为LongMemEval英文S历史，277383467B，SHA256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`，URL `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json`；main只接受相同LFS字节，不伪造未核revision。当前license字段明确unverified/no redistribution，正式执行前仍需补许可证据，不能把此占位当授权结论。

### prepare/verify 已执行

工作目录为本工作树，均经cmd/login=false：

```text
uv run python -m experiments.memory_validation.runner prepare --source C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114/data/longmemeval_s_cleaned.json --output C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114/memory-v1/plan --old-rows docs/experiments/results/long-horizon-20261113/memory-v2/rows.json --model-dir C:/Users/MLL/.cache/my-agent-harness/models/bge-small-zh-v1.5/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59 --source-revision main-exact-LFS-SHA-unversioned --source-license unverified-upstream-license-no-redistribution --ledger-path C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114/memory-v1/ledger.json --campaign-id memory-effect-v1-disjoint12
uv run python -m experiments.memory_validation.runner verify --plan C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114/memory-v1/plan
```

两命令exit0；verify返回valid=true/questions12/limits72requests+3000000tokens。只校验模型文件hash，没有构建索引或执行embedding。plan冻结source/旧排除清单/问题与corpus/判分references/研究实现/BGEmanifest/模型/预算/回退规则。回答与judge每槽一次、1024输出/180秒、零重试。新TokenLedger继承既有持久BudgetLedger，加全study请求/token预留与unknown/inflight阻断；旧USD30仅兼容proxy护栏，不表示新增30美元或真实费用。没有在此阶段创建付费请求。

未完成项：新定向测试尚未执行（主代理API校准运行，遵守串行）；retrieve仅允许Linux seccomp worker，WSL生产SDK/模型路径尚未实测；paid及汇总需离线边界回归和交叉review；本次prepare冻结Windows路径，runner含Windows/WSL `/mnt/c` 定位转换，但未在WSL演练；更改runner会使现plan实现pin失效，必须新建版本目录，不能覆盖旧plan。正式执行许可、模型/输出预算校准后由主代理启动，不直接盲跑72请求。

## 3. 预算校准、backend与观测边界

旧已提交90个回答元数据：90/90 answer_status completed，90/90 finish_reason stop，无空回答记录；90个judge为89 stop、1 length，89 completed、1 unavailable/invalid_judge_format，比例1/90。因此1024预算并非已知大范围length，但仍有一次空正文截断；新12题不能预填有效判分，任何length/空正文须保留。当前只从安全元数据统计，未输出问答正文。

建议本批固定旧CPU BGE：512维、固定revision、2线程，无GPU常驻，保持backend与旧实验可比，但中文encoder处理英文有失配；每题临时建库，时间包含冷启动/索引/查询，旧向量TemporaryDirectory已关闭删除，不能复用为warm查询。只复用相同模型文件与确定性语料构块规则，不写旧目录。Qwen3-Embedding-0.6B为另一1024维GPU backend，带CUDA常驻/模型加载/批大小等变量；即使已安装也没有新效果证据，应单独冻结实验，不能把模型更换收益归因于Mem0算法。本次未启动二者。

安全presence检查：global settings文件存在，observation节缺失、enabled=false；默认/配置public与secret env均不存在，LANGFUSE_HOST/LANGFUSE_BASE_URL也不存在。未打印密钥或读取auth缓存，未修改服务。现阶段只能做本地覆盖/token对账和故障隔离；远端需要用户项目host/public/secret配置以及明确上传/回读验收，不能宣称成功。

可复用专项入口：`tests/llm/test_observation.py`、`test_observation_runtime.py`、`test_observation_export.py`（bounded exporter、关闭/flush失败隔离、缓存token映射）、`tests/integrations/test_langfuse_sdk.py`（本地SDK）。本阶段未重跑它们；应在主代理校准结束后的串行测试窗口执行。

## 4. DS-2 接管与 memory-v2 冻结链路核验（2026-11-14）

运行时工具元数据已核实执行助手为 `deepseek-v4.1-flash` / `max`，sandbox=`danger-full-access`、permission profile=`disabled`；无强制只读限制。readiness 已通过真实写入与 JSON 回读验证。证据为私有 `effect-validation-20261114/ds2-executor-ready.json`。

接管时旧 WSL runner/worker 已退出，Windows 精确命令元数据也未发现旧实验进程。现场状态为 5 completed、1 started、6 尚无状态；专用 memory ledger 尚不存在，answer/judge 产物为 0。未启动过本轮付费请求。五题已提交记录及 manifest 全部验 hash 后保持原字节；只将中断的免费检索 started 记录移到 phase 外的追加 recovery 目录，并复用同一 plan 恢复剩余七题。没有覆盖既有 completed 结果，也没有改冻结源码。

本批使用的新冻结 plan 为私有 `effect-validation-20261114/memory-v2/plan.json`，SHA256 `cc2a53c54bceb9e78e3f2ed4634bd029725e733db0d705d5fca4be3a1a671ee9`；旧 memory-v1 占位许可记录仅作历史保留。数据 HF revision 固定 `98d7416c24c778c2fee6e6f3006e7a073259d48f`，MIT 证据在私有 data/memory-metadata.json；数据字节 SHA 与第 2 节相同。BGE manifest SHA 固定 `03283f4998f67e65623a88a408716bd8e8f22af9753c4c3132c28e3e59a6ab42`。Windows、WSL 两端 verify 均 exit 0、valid=true、12 questions、72 requests / 3,000,000 tokens。

真实执行命令：

```text
uv run --no-sync python -m pytest tests/experiments/test_memory_validation.py -q
uv run --no-sync python -m ruff check experiments/memory_validation/runner.py tests/experiments/test_memory_validation.py
uv run --no-sync python -m pytest tests/llm/test_observation.py tests/llm/test_observation_runtime.py tests/llm/test_observation_export.py tests/integrations/test_langfuse_sdk.py -q
```

分别 14 passed、Ruff All checks passed、21 passed，全部 exit 0。路径往返与冻结篡改检测已在 14 项中覆盖；没有运行全库 pytest、真实效果跑分以外的 benchmark 或 GPU。

已阅读本机 Langfuse 官方 skill 及 instrumentation reference，并实时获取官方 best-practices、Python SDK、metadata 文档核对。当前安装 Langfuse 5.2.0 / OpenTelemetry SDK 1.45.2，本轮保持版本不升级。现有 bounded queue / single worker / metadata-only generation / flush / 自有 client shutdown 与本地 21 项验证相符；云端环境变量 presence 仍为空，EU host 固定 https://cloud.langfuse.com，未尝试从聊天读取 key，云上传和读回保持 pending。未来最小建议是增补 campaign/session、task/branch 和非敏感 arm/stage 元数据，需根代理设计批准后在新版本实施，本轮冻结源码不改。完整安全审阅在私有 langfuse-audit-ds2.json。

看板已修正实际导出的 H2/Row/Stack 导入及 ArmRow 类型别名，更新为 14 项真实测试。私有 tsconfig 映射实际 SDK 与真实 React 19.3.0 类型，TypeScript 7.0.2 检查：严格 SDK 检查仅剩 SDK hooks.d.ts 自身缺失 `./internal/canvas-action-dispatch.js`；`--skipLibCheck true` 的作者源码类型检查 exit 0。没有伪造 any 声明，未获取宿主运行/渲染诊断，不能宣称宿主可运行。文件：`C:/Users/MLL/.cursor/projects/c-Users-MLL-Desktop-my-agent-harness/canvases/harness-experiments.canvas.tsx`。

付费前安全预检已通过生产 `AgentPaths().auth_path` + `AuthManager` openai profile 确认既有凭据 presence、内存解析成功及端点与冻结协议一致；无秘密值进入任务、代码、日志或 Canvas。将先运行一题三臂并核对 durable reservation/usage settlement，再继续本轮上限内的回答和 judge。

## 5. 十二题真实检索已封存；付费前发现账本布局阻碍（2026-11-14）

Linux/WSL 同一冻结协议下累计 12/12 题完成、36/36 臂 valid、36/36 close_complete，所有 worker 已退出；没有语义到词法的静默回退。旧五题结果字节保持原 hash，中断的第六题 started 记录已追加归档，后续仅恢复七个尚未提交的免费检索。进程 exit 0，stdout status=completed。最终检索 manifest SHA256 为 `2095a80b3ef8742f3d4804ab22898e7e2e1d0c1ad3ae2ba503126588873cb624`，25 个文件逐一校验。私有冻结凭据为 `memory-v2-run/retrieval-frozen.json`。

| arm | 有效检索 | 会话标签分母 | 平均 session recall@8 | 平均 session MRR | 平均每题冷检索秒 | 中位每题冷检索秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 12/12 | 8 | 0 | 0 | 0.000004 | 0.000004 |
| lexical | 12/12 | 8 | 0.8125 | 0.8750 | 4.3291 | 4.2934 |
| mem0 | 12/12 | 8 | 0.84375 | 0.8125 | 99.9319 | 96.8300 |

四题为 abstention，官方 session label 召回无证据分母；八题普通样本用于宏平均。上述是会话命中/会话标签召回，不是回答证据块召回，也不是回答准确率。answer-block evidence recall 因无标注 answer-bearing chunks 保持 unavailable。index_and_query_seconds 合并语义建索引与查询，不可作为纯 warm-query 延迟；上表包含每题冷建库/索引/查询/关闭。完成样本的 Mem0 冷检索总时长 1199.1828 秒不含被监督器中断的历史尝试开销。中文 BGE 处理英文及 N=12 的探索性限制仍有效。

公开安全产物新增在 `docs/experiments/results/memory-effect-20261114/`：`retrieval-summary.json`、`retrieval-rows.jsonl`（36 行匿名元数据）、`retrieval-pairs.json`（12 题 paired + 6 类型分组）、`metadata-manifest.json`。已检查行数、匿名 chunk/session ID 形状和禁止字段，无原始问题、历史、回答、gold 或密钥。Canvas 已改为这些真实 session 指标、冷开销及限制，作者源码类型检查通过，SDK 内部声明缺失和宿主渲染未验证的限制保留。

**重要阻碍：尚未发出任何研究回答/judge 请求。** 固定 v2 plan 的 ledger_path 是 `memory-v2/ledger.json`，位于精确清单校验的不可变 plan 内。`runner.py:213` 调用严格 verify_inventory；`runner.py:431` 首次创建 TokenLedger 后会增加 ledger/lock；`harness_ablation.py:80` 随后拒绝额外文件。因此虽然当前 verify 有效，先跑三请求再恢复会破坏计划的可恢复性。该问题在实际付费前发现，专用 ledger 仍不存在，0 研究请求、0 settled/held tokens、无 unknown/inflight。生产 AgentPaths/AuthManager 已验证可提供匹配端点的凭据，凭据缺失不再是阻碍。

供根代理批准的最小修订已落盘 `ds2-plan-ledger-blocker.json`：保持 source/question/corpus/embedding/model/arms/budget/implementation/campaign_id 全部不变，新建 memory-v3 计划版本，仅将 ledger_path 移到计划外的 memory-v3-run/ledger.json；把 12 题既有 result/state 原字节复制到新 phase，更新 phase 对新 plan hash 的引用并校验全部 hash，旧 plan/结果不覆盖，不重新 embedding，不重试付费。工具 `migrate-memory-ledger-layout.py` 已在 proposal-only 模式 exit 0；尚未执行迁移。批准后才运行带 `--execute-approved-bookkeeping-amendment` 的同一工具，随后在修订后的同一科学协议先一题三臂，再核账继续回答/盲 judge。当前冻结 runner 与全部固定源文件未修改；prepare-time 账本路径守卫及回归留待研究结束后另批修复。

## 6. 已批准账本迁移与真实回答／盲判完成（2026-11-14）

上一节记录的是付费前阻碍，后续由根代理批准仅修改记账位置的 v2→v3 amendment。迁移前再次确认无研究付费请求、无 held/unknown；保留 v2 plan 和失败记录，新建 v3，把 ledger_path 移到 plan 外的 memory-v3-run/ledger.json。问题、数据、encoder、提示、三臂、预算、campaign identity 与 13 项 source pins 均不变；24 个 result/state 文件逐一原字节复制，phase 只改 plan 引用，未重建索引。

| 冻结对象 | SHA-256 |
| --- | --- |
| memory-v2 plan | cc2a53c54bceb9e78e3f2ed4634bd029725e733db0d705d5fca4be3a1a671ee9 |
| memory-v3 plan | e45e2ca7d9453787136243a9779b438760400a5461161199683d5c96b1a39727 |
| v3 检索 manifest | 53a312bd80cb4b9188822f68d6deaa006916a21376c56c051d5f1326db5f4b9d |
| answer manifest | fdfecaa75c1f11c03781aa8d309f74ad083361980e8ae9ae11fbe564d0710c76 |
| judge manifest | d11b9fb2257a79f84815b6d1015bb3ef0bd2f93e852352dd5b08814a05918c3c |
| 最终专用 ledger | 162763f701aa072c48a1591033663d9ed7fac4323795656c2c6669fdeb1be929 |

先一题三臂共 3 次请求、2748 actual tokens 全部 settled，再用同一持久账本完成其余 33 回答及 36 judge。最终为 **36 answer + 36 judge = 72 settled requests，41,852 actual tokens（prompt 36,211 / completion 5,641）**；held=0、unknown=false、retry=0。每槽 1 请求、1024 输出上限、180 秒超时、零重试，累计上限 72 请求／3,000,000 tokens。全部回答和 judge finish_reason=stop，length、invalid、unavailable 均为 0；未用重试筛选答案或判分。供应商真实 USD 未核验，保持 null；旧 USD30 ceiling 是兼容代理，不能作真实费用。

使用 LongMemEval 官方判分模板，判分模型为 DeepSeek V4.1 Flash，judge 看不到 arm；gold/reference 只在宿主判分阶段。结果是本机固定条件下的模型判分，不能称官方模型成绩或独立人工复判。

| arm | correct / expected | accuracy | 有效 judge | 平均回答秒 | 平均 judge 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| none | 4/12 | 33.33% | 12/12 | 3.4020 | 3.0365 |
| lexical | 11/12 | 91.67% | 12/12 | 4.2153 | 2.8786 |
| mem0 | 9/12 | 75.00% | 12/12 | 3.8913 | 2.8208 |

lexical↔mem0 逐题配对：lexical-only correct 2、mem0-only correct 0、共同正确 9、共同错误 1，即 lexical 2 胜／0 负／10 平。这 12 题没有支持 Mem0 优于词法。

| 固定题型（每类 2 题） | none correct | lexical correct | mem0 correct |
| --- | ---: | ---: | ---: |
| knowledge-update | 1 | 2 | 2 |
| multi-session | 1 | 2 | 2 |
| single-session-assistant | 0 | 2 | 1 |
| single-session-preference | 0 | 1 | 0 |
| single-session-user | 1 | 2 | 2 |
| temporal-reasoning | 1 | 2 | 2 |

另外按冻结宿主 references.json 的 is_abstention 分项：四题拒答三臂均 4/4；八题非拒答 none 0/8、lexical 7/8、mem0 5/8。因此 none 的 4/12 来自正确拒答，不能解读为无历史仍回答了四个事实。prepare 的固定分类规则为原始 has_answer=false 或官方 question_id 以 _abs 结尾；本样本全部 12 条冻结标记与 suffix 一致，未仅凭公开匿名名字猜测分类。安全分项见 [subgroup-summary.json](../results/memory-effect-20261114/subgroup-summary.json)。

根代理独立读回 36 行／12 unique，重算 correct=4/11/9、逐行 answer+judge token 合计 41852、所有 judged_answer_sha==answer_sha，并核对四个公开文件的 bytes/SHA。该证据是 root readback/recomputation，未发生独立人工重判。

公开 [effect-summary.json](../results/memory-effect-20261114/effect-summary.json)、[effect-rows.jsonl](../results/memory-effect-20261114/effect-rows.jsonl)、[effect-pairs.json](../results/memory-effect-20261114/effect-pairs.json)、[bookkeeping-amendment.json](../results/memory-effect-20261114/bookkeeping-amendment.json) 与其 [manifest](../results/memory-effect-20261114/effect-metadata-manifest.json) 已固定；不含原始问答、历史、gold、模型回答或凭据。

## 7. 三例匿名人工诊断与指标边界（2026-11-14）

只对两题 lexical-only correct 及一题 lexical/mem0 共同错误，内部对照冻结 reference、原始 corpus chunk、实际渲染上下文和已生成回答。没有新增 API、重判或改分；公开只保留匿名 ID、类型与位置事实。三例是定向人工抽查，缺少全体 answer-bearing chunk 标签，不能据此计算量化 answer evidence recall。完整安全元数据在 [case-diagnostics.json](../results/memory-effect-20261114/case-diagnostics.json)，两份诊断文件的 hash/bytes 在 [diagnostics-manifest.json](../results/memory-effect-20261114/diagnostics-manifest.json)。

| 匿名题 ID／类型 | 结果与 session 命中 | 实际上下文与人工抽查 | 可支持的诊断 |
| --- | --- | --- | --- |
| lme_f16814d7f87b624a7f8d84dd／single-session-assistant | lexical 正确、mem0 错；两臂均命中标注 session | lexical 6000 字符／7 chunks／有全局截断，决定性事实在 rank1 chunk lme_a7762d64a8e3c9a100d05726 中完整保留；mem0 4828 字符／8 chunks／未截断，未返回该 chunk | Mem0 在 top8 选择阶段遗漏支持块；6000 字符上限未挤掉 Mem0 已返回的证据。session 命中不能保证答案事实进入上下文 |
| lme_da00a576cac93baec68c34e5／single-session-preference | lexical 正确、mem0 错；两臂均命中标注 session | lexical 3173、mem0 2494 字符，各 8 chunks，均未截断；lexical rank5 的偏好块 lme_118b93a7d4232134db27aeb3 与 rank8 的相关建议块 lme_8e93abf999006b4785c0f73d 均不在 Mem0 返回集合 | 有用偏好／建议材料进入 lexical，上述支持块未进入 Mem0，后者拒答；没有断言 lexical 覆盖参考答案全部细节 |
| lme_f7c6b941b12a5bb4092f3c00／single-session-preference | 两臂均错，均未命中标注 session | lexical 3643、mem0 3350 字符，各 8 chunks，均未截断；corpus 中相关个性化块 lme_571231bd3f84d630a4621510 未被任一臂返回，两者均拒答 | 与检索遗漏一致；单题轨迹不能分别确定 encoder、排序及生成器的因果责任 |

没有保存全量 pre-top8 向量排序与分数，所以不声称被遗漏块位于第 9 名，也不将三例归因为某一确定 encoder 缺陷。总体 session recall@8 的八题宏平均为 lexical 0.8125、mem0 0.84375，而回答为 11/12、9/12；二者衡量对象不同，该差异是保留块级证据验收的理由。四题 abstention 的 session 指标 unavailable，不纳入八题分母。

本轮 Mem0 使用 infer=False，把相同 800 字符历史块交给索引；未测自动事实抽取、更新、巩固。固定中文 BGE 处理英文历史和十二题小样本均限制外推。lexical 平均冷建库／检索／关闭 4.3291 秒，Mem0 99.9319 秒，Mem0 十二题合计 1199.1828 秒；它们包括冷建索引且不包含被中断的历史开销，不能用作 warm 查询性能、线上 SLA 或完整账期成本。后续模型或记忆提取协议变化必须新建 lane，保留本轮负结果。

## 8. 研究结束后的 prepare 守卫修复与 24 项回归（2026-11-14）

本节为真实研究完成后的工程修复；不改旧 plan、source pins、回答、判分或检索产物。prepare 在规范化 ledger 与 plan 路径后，提前拒绝 ledger 本体及其派生锁与 immutable plan 相同、位于 plan 内，或成为 plan 祖先；同前缀的 sibling run 目录合法。

第一次实现按 ledger、.lock、.study.lock 增加七个冲突负例，先观测七项 DID NOT RAISE，再实现守卫，完整专项为 22 passed。根代理随后指出 TokenLedger 的真实 study_lock 是 .tokens.lock，初版漏守卫；追加“plan 正好等于 ledger+.tokens.lock”与“plan 位于该派生路径下”两个负例，先观测两项失败，再补入实际 suffix。最终守卫集合为 ledger 本体、.lock、.tokens.lock、.study.lock。首次 22 项作为过程记录保留，最终验收数量为 **24 passed**。

审查真实写入路径：BudgetLedger.lock 为 ledger+.lock，TokenLedger.study_lock 为 ledger+.tokens.lock，付费阶段外层 FileLock 为 ledger+.study.lock。BudgetLedger._write 在 ledger.parent 使用 tempfile.mkstemp(prefix='.budget-')，随后 os.replace 到 ledger，失败时 unlink；不存在另一个固定临时 suffix。合法 sibling 回归实际创建 TokenLedger 并 register/reserve/settle 三个合成 tokens，核对全部真实锁属性，以 spy 记录每个 .budget- 临时文件均位于 sibling ledger parent 且在 plan 外，最后再次严格校验 plan 清单。这里只使用合成账本事件，无真实 provider。

验证命令：

```text
uv run --no-sync python -m pytest tests/experiments/test_memory_validation.py -q
uv run --no-sync python -m ruff check experiments/memory_validation/runner.py tests/experiments/test_memory_validation.py
```

作者执行最终 24 项全部通过、Ruff All checks passed，均 exit 0；根代理独立重跑同一专项和 Ruff，同样通过。未并行全库 pytest、embedding 或真实模型。最终 runner SHA 为 8cdabbe2457996ecd68b2b5f0098b900687d29b7e1db36e4d02d1a0e8d8860eb，测试 SHA 为 dfc467d899bfe3ee1597002921416e85f68fae3fb40fa3d2cc58952855d999d5；实验时 runner SHA 仍为 8792a9ae16985c20318521ee3382a7be67238f609c8a3628bc8bde0de8e57d05。

## 9. 可复查源码与本批交付边界（2026-11-14）

已逐项比对全部 13 个旧 source pins 与公开 Git 基线 0b63386a4800a16611ed03fa3a831ff7952b23bb；8 项可用 git show 精确取回，另 5 项的实验时字节不在基线，也未出现在本机已知 remote-tracking 历史。仅为这 5 项保存无秘密研究源码 [source-snapshot](../results/memory-effect-20261114/source-snapshot/README.md)：judge.py、memory.py、memory_index.py、memory_safety.py 与旧 runner.py。每份按原路径保存并重算 SHA/bytes，与冻结 pins 一致；[manifest.json](../results/memory-effect-20261114/source-snapshot/manifest.json) 列全 13 项的 Git／snapshot 取得方式、基线 hash 与恢复步骤。此次未联网 fetch，Git 历史核对范围已明示。

源码快照只保存研究代码，不含数据、原始问答、历史、gold、模型答案、session、认证桥或凭据。它解决“实验时未提交 runner 在修复后不可查”的问题；历史 runner 中的路径问题保留为原始证据，当前 runner 的修复另有红绿回归。不会以当前源码 SHA 冒充旧 study pin。公开效果四文件及原 manifest 的 bytes/hash 保持不变，新增诊断与源码各有单独清单。

本批已完成受控 Mem0 infer=False 索引、gold 宿主隔离、seccomp 检索、原字节恢复、累计账本与 source pins、真实回答和盲判，以及研究后布局守卫。Langfuse 21 项本地 SDK 验证与云上传 pending 的边界保留，Canvas 宿主渲染仍未验证。RSI 为独立 DS-1 lane；本报告不宣称任何策略学习提升。新 Memory 长程／多轮方案只在另行冻结协议后执行，不修改这批十二题或追选获胜样本。
