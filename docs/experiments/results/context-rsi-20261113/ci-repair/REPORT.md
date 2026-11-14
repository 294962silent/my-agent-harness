# 默认依赖与Linux清理修复：保留首次CI失败

源码24aeca1推送后的[CI43444322518](https://github.com/294962silent/my-agent-harness/actions/runs/43444322518)四矩阵失败，不把之前Windows all-extras1424通过称为跨平台通过。

三项真实Mem0 SDK用例无条件import可选包，在默认安装报ModuleNotFoundError。主代理独立干净Windows/Py3.13复现3失败/6通过；仅这三项改局部importorskip，七项profile/IPC等协议测试继续默认执行。all-extras CI专项仍实际执行全部SDK用例。

Linux另有生产缺陷：超时正确结束worker，但TextIO stdin.close再次flush残余buffer，BrokenPipeError覆盖TimeoutError。SOL-2在本机WSL和Windows注入反例先红后绿；修复只忽略**实际owned worker确认退出后的stdin BrokenPipe**，其他流、其他异常或未确认退出继续报错。TimeoutError断言未放宽。SOL-1只读复核异常传播、native-worker确认与线程清理；不声称存在独立“其他close错误”负例成绩。

## 修后验证

- 作者Windows及WSL的SDK环境各10项通过，0GPU/API；[诊断和源码前后hash](repair-diagnostic.json)。
- 主代理干净默认依赖Py3.13：7 passed、3 skipped in4.81s；完整all-extras：**1425 passed、11 skipped、5 subtests passed in112.77s**，均使用`--import-mode=importlib`，不与早先批次相加。
- 主代理最终源码真实GPU复验：禁词法回退、受控Python Internet socket拒绝，1024维两事实中英文检索成功、close_complete与实际worker退出。startup19.21秒，两查询0.522/0.073秒，短文本allocator峰值仍1,207,842,816B；与先前运行的时间差不是性能对照。quality=unmeasured，付费API为0。
- Ruff、diff check、最终wheel11源码bytes与真实autocrlf=true checkout一致；[最终源码指纹](validation.json)。新[GPU指标](gpu-final-metrics.json)单独保存，不改原GPU/RSI档案。

`failure-summary.json`是首轮失败；修后本机、作者WSL、最终远端CI各有自己的证据。远端新head结果在PR checks核验后更新PR正文，不用本机通过替代。代码只改memory_qwen.close和指定测试；本机配置、预算账本、原benchmark和模型权重保持。
