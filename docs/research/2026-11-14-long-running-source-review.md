# Claude autonomous-coding 固定源码核查

2026-11-14；SHA `4a4224bdf0e9cfb3332167b69c90457403813625`。仅核三文件，0模型调用、未运行/安装。仓库metadata为非archived、MIT；[README 161–163](https://github.com/anthropics/claude-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/autonomous-coding/README.md#L161-L163)另写“Internal Anthropic use.”，许可范围未核清。

**已核host：** 同一驱动按提示词分初始化/编码职责，不是并发执行器。以`feature_list.json`存在判续接，每轮新建SDK上下文。首轮flag在调用前置false，会话返回error后下轮仍转编码；无全部通过停机门，仅有迭代限额。[agent.py 124–181](https://github.com/anthropics/claude-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/autonomous-coding/agent.py#L124-L181)

**已核prompt：** 要求200项功能/样式测试步骤，初始`passes=false`，由Agent改true；禁改描述/步骤。`init.sh`、git提交、进度文件和收尾也交给Agent。所读host无独立判分门，不能把flag当验收。[initializer 12–101](https://github.com/anthropics/claude-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/autonomous-coding/prompts/initializer_prompt.md#L12-L101)

**未核：** 浏览器验收只见步骤/截图描述，host只打印工具事件；client、安全配置、后续提示词未读，不能确认浏览器装配、真实E2E或沙箱效果。文件续接不等于崩溃恢复保证。

**已核依赖：** 使用`ClaudeSDKClient`的query/receive_response及生命周期，DeepSeek适配应迁移协议，不能只换模型名。[agent.py 12–49](https://github.com/anthropics/claude-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/autonomous-coding/agent.py#L12-L49)

**推断最小借鉴：** 做跨前端/API/持久化的工单流程，5–10项固定验收、至少两次新Session。补场景清单、分角色驱动、可重入启动/健康检查、检查点（HEAD/清单哈希/游标）、宿主API/Playwright验收器；初始化不合格不续跑。宿主写完成状态，Session留决策，Artifact留日志/截图绑定commit，沿用总预算、unknown即停，保留失败成本。该设计尚未实现。

Mem0可选存领域事实，不取代进度真值；Langfuse观测调用/成本/失败；Dream-RSI等候选及目标有区分度后选策。首版固定/贪心协议即可。
