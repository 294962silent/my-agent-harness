# my-agent 设计精读：逐小节学习与横向比较

本目录服务于 AI 应用 / Agent 业务开发面试准备。以用户截图中的项目亮点为学习顺序，每个小节单独生成详细 Markdown，先解释设计问题，再沿本机源码说明实现，并比较其他开源项目在同一问题上的选择。

## 已展开的小节

1. **从这里开始：[01A｜事件是什么：读文件案例](01A-事件是什么-读文件案例.md)**。先用一个具体例子理解实际工作、过程通知和界面的分工。
2. [01｜只读事件与执行观察设计](01-只读事件与执行观察设计.md)：作为进阶参考，包含 14 类事件、分发语义及六项目源码对照，按需要查阅。

## 当前讲解节奏

一个关键词可以继续拆成几个小单元。每次只展开当前单元，先讲故事和具体步骤，再引入一个必要术语；详细源码与横向比较放在后面选读。

事件主题的推荐顺序是：01A“事件是什么” → 下一小节“事件怎样从工具传到屏幕” → “只读是什么意思” → “为什么有 14 类” → 设计取舍与横向比较。后续文件在实际讲到时生成。

## 固定比较对象

- 本机 [my-agent-harness](https://github.com/294962silent/my-agent-harness)。
- 用户指定的 [Pi：earendil-works/pi](https://github.com/earendil-works/pi)。
- 用户指定的 [oh-my-pi：can1357/oh-my-pi](https://github.com/can1357/oh-my-pi)。
- [Hermes Agent：NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。
- [DeepSeek Harness：deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。
- [OpenCode：anomalyco/opencode](https://github.com/anomalyco/opencode)。

Pi 与 oh-my-pi 分别按上述仓库核验，不与 oh-my-opencode、oh-my-openagent 或 oh-my-agent 混用。DeepSeek Harness 指 DeepSeek 的开源项目，不是当前 Codex 调用 DS 助手的 MCP 执行器。

## 每份小节的内容标准

1. 每份围绕一个能独立理解的小问题，用具体用户场景解释，遇到术语立即翻译成日常语言。
2. 先把案例拆成步骤，再讨论设计选择与取舍；不在开头堆叠多个抽象概念。
3. 按真实调用或事件顺序串起输入、状态变化、输出与异常边界。
4. 引用原作者博客解释设计意图；用当前源码核对实际实现。
5. 围绕同一问题比较其他项目，注明仓库、commit、文件与行号。
6. 区分编译期类型、运行时保证、文档描述和未核验范围。
7. 提供面试表达、追问和一个小型复述任务。

横向比较不按事件数量、语言、项目规模或星数排序，也不把未读取某项实现推断成整个项目没有这项能力。比较结论优先回答“什么场景适合哪种选择”。

## 后续顺序

依次讨论：五个生命周期 Hook、Extension、TUI 流式呈现、双层 Loop、ReAct、自动续写、Steering、Task / Follow-up；随后进入上下文与会话、记忆检索、工具执行和评测。后续条目在实际展开时才生成文件，不提前填充未核验课程。

## 与已有材料的关系

- 原 [tutorial.md](../../tutorial.md) 保留既有关键词课程，作为阅读队列与历史笔记。
- [tutorial-interview.md](../../tutorial-interview.md) 保留面向业务场景的学习路线。
- 本目录承载当前采用的“每个小节一份详细 MD”正文；讨论时优先按这里的当前小节推进。
- 原作者博客：[my-pi-agent 分类](https://zxj-2023.github.io/categories/agent实战/my-pi-agent/)。

每一节的代码快照和核验范围单独记录。学习当前项目与陈述个人职责分别处理，不能由仓库内容推断某项工作一定由个人独立完成。
