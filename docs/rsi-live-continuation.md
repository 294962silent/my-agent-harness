# 真实校准的精确运维例外：2026-11-14

用户已授权继续真实测试；主代理基于完整结算和非付费环境检查，决定对原未启动槽登记一次运维例外。它不是修复已知权限根因，不是无修订完成原协议，也不允许重跑旧失败。当前状态以逐批实验日志为准。

## 触发与取舍

v2在`test-HumanEval-6-s23-C`出现`PermissionError`，原sealed结果未保存路径、errno或WinError。298次请求全部结算，pending/unknown/held均0；原产物可读，离线复制成功，未见本批runner/worker在运行。证据不能定位或证明权限根因已经消失。

直接重跑failed会破坏失败分母并重复消费；清除全局blocked会把其他异常一起放过。采用独立controller，只对这一项已经审查的历史失败做精确、可验证的开始门控例外，后续仍执行原任务。

## 不变条件

- 原117份执行源码、模型、11题材料、四臂策略、额度、评分器及顺序不变。新增controller另存源码hash与协议身份，不能冒称仍是原runner无修订执行。
- 原journal全部旧行、原失败result/final侧证据、research-policy与停止态分析不改；失败槽永不retry、换ID、补final或改completed。
- 原`Campaign._state`和`summary`保持原义，原summary仍blocked。独立operational gate与原状态分别报告。
- 续跑仅限事前已计划、尚未started的37个原槽。每episode父额度仍为64请求、1M reservation tokens、1800秒、6评估，attempt12轮；用户放宽总费用限制不取消可靠性守卫。

## 接口与封印

独立`continue_live.py`提供register与run。register必须0模型调用，在campaign外新建不可覆盖的incident目录，绑定原journal前缀、唯一失败的terminal/result/final证据、plan/task/source/policy、37待跑槽及controller bytes。

run只调用原`run_prepared_trial`，不调用原`run_batch`。`start_trial`在原文件锁内做CAS：其他slot/元数据规则不变，仅允许精确封印的这一项旧failed共存，不改变旧状态。付款与评分前保留全部原验证，额外核incident和新增controller的封印。不同进程不能并发接管；已started槽绝不重采。

每个新槽后要求所有非completed项恰为原已审查失败，且pending/unknown/held为0。任何新host失败、用量缺失、封印漂移、清理错误立即停止。首次仅放行1个新槽，核完整证据后再继续剩余集合。异常诊断仅保留固定类型、数值错误码和可安全表示的工作区相对路径；不输出任意异常字符串、prompt或凭据。

## 验证与科学解释

先用离线回归覆盖重复槽、额外failed、unknown/pending、prefix/来源/策略/controller漂移、注册写盘失败与环境凭据拒绝，再由另一位助手独立审查；主代理实际运行后才记录成功。

报告仍以原53槽为完整分母，区分原batch和incident后执行，并保留原失败。C仍为reference fallback，学习贡献不可识别。即使37槽全部运行，仍只是短代码真实链路校准，不能证明长程能力或RSI提升；中断和恢复也是本批外部有效性的限制。
