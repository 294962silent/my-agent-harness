# 长期记忆使用说明

长期记忆保存跨会话事实与偏好；它不同于当前聊天记录或压缩摘要。启用记忆后，Agent 固定注册写工具 `memory` 和只读工具 `memory_read`。

## 目录与作用域

- `memory_dir` 决定作用域：共享目录即共享记忆；不同目录才相互隔离，不按模型、会话或工作区名称自动隔离。
- `Agent(memory_dir=False)` 禁用；传入路径明确启用；`None` 仅在当前目录的 `.my_agent_core/memory` 已存在时启用。
- `CodingAgent` 未显式传参时，优先使用工作区已有的 `.my-agent-harness/memory`，否则使用 `AgentPaths().memory_dir`（通常为用户目录下的 `.my-agent-harness/memory`，受 `MY_AGENT_HOME` 与旧目录兼容规则影响）。
- 自由条目存于 `MEMORY.md`、`USER.md`；结构化事实存于 `facts/` Markdown 文件。索引是可重建的内存缓存，Markdown 是事实来源。
- 修改目录下的文件后，应重新加载 `MemoryStore` 或重建 Agent；不保证其他进程或已有实例自动看到修改，也不提供跨进程写锁。

## 工具与完整回读

`memory` 保留 `add`、`replace`、`remove` 和 `remember`，新增 `delete`：

```json
{"target":"user","action":"remember","key":"preferred_port","value":"8080"}
```

`remember` 写结构化事实；完全相同的 subject/key/value 不重复写入，不同值保留替换历史。`target="user"` 映射 subject `user`；`target="memory"` 映射 subject `memory`。

```json
{"key":"preferred_port","subject":"user"}
```

以上是 `memory_read` 参数：按精确 key/subject 返回完整 JSON，包含 value、有效时间、importance、source 与 history；未知事实明确返回 not found。参数不是文件路径，不支持路径穿越。

```json
{"target":"user","action":"delete","key":"preferred_port"}
```

以上是 `memory` 删除参数：移除该精确身份及其替换历史，立即更新检索，并写入删除标记以阻止旧 JSONL 再迁移复活。显式重新 `remember` 可以创建新事实。`remove` 仍仅删除自由文本条目，不能替代结构化 `delete`。其他 subject 可通过 `MemoryStore.delete(key, subject=...)` 操作。

检索默认使用 1200 字符预算、最多八条；结果提供 subject/key，超长首条显示有界预览与 `[truncated; use memory_read]` 提示。需要全文时调用 `memory_read`。超长工具结果仍可能由上下文层投影成产物预览，按提示回读产物。

## 可运行的本地 API 示例

以下示例只访问本地文件，不请求模型：

```python
import json
from pathlib import Path
from my_agent_core.memory import MemoryStore

store = MemoryStore(Path(".my-agent-harness/memory"))
store.load_from_disk()
store.remember("preferred_port", "8080", subject="user")
fact = json.loads(store.read_full("preferred_port", subject="user"))
assert fact["value"] == "8080"
print(store.retrieve("preferred port"))
print(store.delete("preferred_port", subject="user"))
```

## 自动抽取与关闭

`Agent` 与 `CodingAgent` 支持 `memory_autodistill=False`（默认关闭）、`memory_idle_seconds=30`、`memory_distill_timeout=15`，单位为秒；这些是 Python 构造参数，不是新增 CLI 参数。

启用自动抽取后，空闲抽取与压缩前抽取一起启用；压缩前抽取为 best-effort，使用同一超时。抽取只使用带来源标记的真实用户原始输入，不把检索结果、摘要、工具输出或旧的未标注 user 消息自动当作证据。候选文本有限额并在请求模型前脱敏；显式启用会产生模型请求。

任务在当前进程内运行，不是持久后台 worker；退出可能丢失尚未执行的任务。宿主可 `await agent.wait_memory_idle()` 等待抽取，结束时 `await agent.aclose()` 取消并回收任务；CodingAgent 的异步上下文管理器也会关闭记忆任务及 MCP 连接。取消仍会向调用方传播，不保证退出前一定完成抽取。

## 脱敏、兼容与限制

统一脱敏函数在落盘前、自动抽取外发前、检索与完整读取出口处理常见 provider/API token、Bearer、私钥块及命名凭据赋值；敏感结构化字段（如 `api_key`、`access_token`、`refresh_token`、`wifi_password`）的短值也会隐藏。普通 `port`、`max_tokens`、`token_budget`、`context_token_budget` 等配置与中文偏好保留。

这是正则启发式，存在漏报与误报；不认识的凭据格式、编码或命名不能保证识别。脱敏不是加密，不应主动把密码交给记忆。历史文件仅在读取和输出时脱敏，不批量清洗原文件；旧 JSONL 备份未擦除，删除标记也不等于对备份做安全擦除。

相比旧行为，本轮补齐完整事实回读、显式事实删除、首条长结果预览、同值去重、写失败上报与内存回滚；自由条目修改后检索立即更新，系统提示词冻结快照仍保持稳定。中文 key/subject 使用稳定 hash 后缀避免规范化碰撞，并兼容旧 `untitled` 文件的精确身份读取与删除。

设计参考固定快照 OMP `fec500b7a891`、Hermes `5df966a97a2d`；Mem0 仅参考论文中的 DELETE 概念。实现为本项目独立代码，不宣称移植上游实现。本轮没有进行 KV 缓存提升或性能测量。
