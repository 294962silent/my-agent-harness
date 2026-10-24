# 工具审批协议（Permission Approval over JSON-RPC）

> 状态：待实现（本次重构的 P0）
> 涉及：`src/my_coding_agent/permissions.py`、`src/my_coding_agent/rpc_server.py`、`my-pi-tui/src/client.ts`、`my-pi-tui/src/interactive/*`、`my-pi-tui/src/components/permission-dialog.ts`

## 1. 目标与非目标

**目标**：让 `review` / `strict` 模式下，每一个需要授权的工具调用都真的停下来问用户，并把用户的选择回传内核；没有客户端应答时一律**拒绝**（fail-closed）。

**非目标**：不做持久化策略引擎（`settings.json` 里的规则匹配）——只做会话级记忆（scope=session）。

## 2. 消息流

```
模型 tool_call
   │
   ▼
PermissionGate.__call__(ToolCallHook)
   │  mode=autonomous/yolo 或 命中会话级白名单 → 直接放行
   │  只读工具（review 下）→ 直接放行
   ▼
confirm_callback(permission_request)  ──►  RpcServer._request_permission()
                                              │ 生成 id，登记 Future
                                              │ 写一行 JSON 到 stdout
                                              ▼
                                    TUI 渲染审批弹窗 → 用户选择
                                              │ 写一行 JSON 到 stdin
                                              ▼
                                    RpcServer.run_forever 识别为「响应」
                                              │ 解析 id → resolve Future
                                              ▼
                                    gate 返回 HookResult(block=...) 
```

## 3. 服务端 → 客户端请求

```jsonc
{
  "jsonrpc": "2.0",
  "id": "perm-1",                    // 服务端生成的字符串 id，与 request_id 相同
  "method": "permission_request",
  "params": {
    "request_id": "perm-1",
    "tool_call_id": "call_abc123",
    "tool_name": "bash",             // bash | write | edit | read | ...
    "action": "bash",                // 展示用动作名
    "target": "rm -rf build",        // 命令 / 路径
    "preview": null,                 // write/edit 的文件内容或 diff 片段（字符串，可截断到 4KB）
    "risk": "high",                  // low | medium | high，服务端给的风险标签
    "reason": "写入工作区外路径",      // 可选：为什么需要审批（人类可读）
    "details": { "command": "rm -rf build" },
    "timeout_ms": 300000
  }
}
```

## 4. 客户端 → 服务端响应

```jsonc
{ "jsonrpc": "2.0", "id": "perm-1", "result": { "approved": true, "scope": "once" } }
```

- `scope`：`"once"`（默认）| `"session"`（本会话内该 tool_name 不再询问）。省略按 `once`。
- 拒绝：`{ "approved": false, "reason": "用户拒绝" }`。
- **任何解析失败 / id 不匹配 / 超时 / 客户端断开 → 视为拒绝**。

## 5. 内核侧契约

1. `PermissionGate(mode=..., confirm_callback=None)` 在**非 autonomous** 模式下必须 **block**（不再放行）。需要放行的嵌入方显式传 `allow_without_callback=True`（默认 False）。
2. `mode="yolo"` 仍映射为 `autonomous`，永不询问。
3. `strict` 模式下只读工具也要询问；`review` 模式下只读工具 + 安全 shell 前缀直接放行（保留现有 `SAFE_BASH_PREFIXES` 逻辑）。
4. 会话级白名单只存在内存（`PermissionGate._session_approved: set[str]`），进程结束即失效。
5. 超时（`timeout_ms`）后返回 `HookResult(block=True, reason="审批超时，已自动拒绝")`。

## 6. 风险分级（服务端计算，客户端只负责展示）

| risk | 判定 |
| --- | --- |
| high | `bash` 命中危险模式：`rm -rf`、`sudo`、`curl ... \| sh`、`chmod 777`、`> /dev/sd`、`git push --force`、`taskkill`、路径在 workspace 之外 |
| medium | 其余 `bash`、`write`、`edit` |
| low | 只读工具（仅在 strict 模式下会走到弹窗） |

## 7. 客户端契约

1. `PythonKernelClient` 收到 `method === "permission_request"` 时 `emit("permission_request", params)`，并**不要**当成响应处理掉。
2. 必须提供 `respondPermission(id, {approved, scope})` 方法写回响应。
3. 弹窗未渲染完成/渲染失败时，必须回 `{approved:false}`（不可静默丢弃，否则内核等 300 秒）。
4. 弹窗支持按键：`↑/↓` 选择、`Enter` 确认、`Esc` = 拒绝。
5. 选项固定三项：`允许这一次`、`本会话都允许`、`拒绝`。

## 8. 兼容性

- 旧客户端不认识 `permission_request` → 会当成未知请求。因此服务端 `await` 必须有超时兜底（默认 300s 可配），保证不会永久挂起。
- `tests/coding/test_permissions.py::test_permission_gate_no_callback_allows_by_default` 必须改名为 `..._denies_by_default` 并断言 `block is True`。
