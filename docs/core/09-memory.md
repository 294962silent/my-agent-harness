# Memory 长期记忆系统设计规范 (`my_agent_core.memory`)

- **定位**：跨会话受控持久化记忆存储与快照注入引擎 (`src/my_agent_core/memory.py`)
- **核心类**：`MemoryStore`, `make_memory_tool`
- **主要实现**：`memory.py`

---

## 一、架构设计与定位

与记录对话流水的 `session` 不同，`memory` 解决的是**跨会话的长期事实、项目约定与用户偏好沉淀**。
系统对标 Hermes Agent 精简版，采用 **“双 Markdown 存储 + Frozen Snapshot 冻结注入 + 受控条目化工具”** 设计：

```text
               启动装配期 (Agent.__init__)
                         │
                         ▼
        MemoryStore(mem_dir) ➔ load_from_disk()
          ├── 读 MEMORY.md (2200 字符限制) ➔ 捕获冻结快照 _snapshot
          └── 读 USER.md   (1375 字符限制) ➔ 捕获冻结快照 _snapshot
                         │
                         ▼
         _init_messages() 注入 <MEMORY_CONTEXT> (冻结快照，本会话全程静止!)
         _register_tools() 自动挂载 make_memory_tool(store)
                         │
                         ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 运行交互期: 大模型调用 memory(target, action, content, ...) │
   ├─────────────────────────────────────────────────────────────┤
   │  1. store.add / replace / remove 校验与更新 live 内存列表    │
   │  2. tempfile + fsync + os.replace 原子写入磁盘              │
   │  3. 【关键不变式】_snapshot 绝对不动！(保护大模型前缀缓存)   │
   └─────────────────────────────────────────────────────────────┘
                         │
                         ▼
               下次启动新会话或 reset() ➔ 重新读取磁盘，新记忆生效！
```

---

## 二、核心类与数据结构

### 1. `MemoryStore` 存储引擎

- **双 Store 字符预算硬限制**：
  - `MEMORY.md`（上限 2200 字符）：存放 Agent 自己的客观笔记、环境事实、踩坑记录；
  - `USER.md`（上限 1375 字符）：存放用户画像、偏好风格、沟通习惯。
- **条目分隔符**：`ENTRY_DELIMITER = "\n§\n"`，条目内部支持多行 Markdown；
- **BOM 容错**：采用 `utf-8-sig` 读写，兼容 Windows 记事本特殊 BOM 头。

### 2. 受控维护方法 (`add`, `replace`, `remove`)

- **`add(target, content)`**：精确去重，超限时拒绝写入并输出当前全部条目，引导大模型先合并精简旧记忆（Consolidate）；
- **`replace(target, old_text, new_content)`**：唯一子串匹配；若匹配到多条不同条目（歧义），**立即拒绝修改并打印冲突项**，防止误改误删；
- **`remove(target, old_text)`**：唯一子串定位删除并原子落盘。

### 3. `make_memory_tool` 工具工厂

生成符合 OpenAI 标准的单工具多 Action 接口：

```python
@tool(
    name="memory",
    description="Manage long-term memory across sessions. Target 'memory' or 'user'..."
)
def memory(
    target: Literal["memory", "user"],
    action: Literal["add", "replace", "remove"],
    content: str | None = None,
    old_text: str | None = None,
    new_content: str | None = None,
) -> str:
    ...
```

---

## 三、核心设计不变式

1. **Frozen Snapshot（冻结快照）与 Prefix Cache 保护**：
   - 运行中所有写操作仅原子落盘，**绝不修改当前会话的 `_snapshot`**；
   - 保证当前会话的 System Prompt 全程绝对静止，大模型的 Prompt Prefix Cache 命中率保持 100%。
2. **子代理沙箱隔离**：
   - 派发子代理时强制设置 `memory_dir=False` 并在工具列表中剔除 `memory` 工具，防止子代理发生工具冲突或意外修改主 Agent 记忆。


---

## 四、检索层（v2，2026-10 新增）

冻结快照解决的是"前缀稳定"，但有一个代价：**记忆一旦超过字符上限就只能删，且每轮都在重复注入同一份内容**。v2 在不破坏快照的前提下补了一层按需检索。

### 4.1 存储

| 文件 | 内容 | 上限 |
| --- | --- | --- |
| `MEMORY.md` / `USER.md` | 自由文本条目（原有） | 2200 / 1375 字符 |
| `FACTS.jsonl` | 结构化事实 `{subject, key, value, valid_from, valid_to, importance}` | 无硬上限（按条检索） |

### 4.2 时序有效性（supersede）

同 `(subject, key)` 写入新值时，旧值的 `valid_to` 被置为新值的 `valid_from`：

```text
{key: "wifi_password", value: "1234", valid_from: "2023-01-01", valid_to: "2023-06-01"}
{key: "wifi_password", value: "9876", valid_from: "2023-06-01", valid_to: null}
```

旧事实保留在文件里可审计，但**检索只返回仍然有效的那条**——这让模型不必在"旧值 vs 新值"之间猜。

### 4.3 检索注入

- `MemoryStore.retrieve(query, budget_chars=1200)`：BM25（自由文本 + 事实）打分，
  叠加 `importance` 权重，在字符预算内取 top-k；
- Agent 在 `prompt_stream` 里按**本轮用户输入**检索，把命中结果包成
  `<RETRIEVED_MEMORY>` 追加到用户消息前；
- 系统提示里的 `<MEMORY_CONTEXT>`（冻结快照）**保持不变**，前缀依旧稳定。

### 4.4 工具

`memory` 工具新增 `action="remember"`，参数 `key` / `value`，写入结构化事实并自动处理覆盖。
原有 `add` / `replace` / `remove` 行为不变。

### 4.5 验证

- `tests/core/test_memory_retrieval.py`：覆盖 supersede、跨实例恢复、预算截断、
  无关查询不注入、Agent 注入位置、工具 `remember` 动作；
- LongMemEval 实测（96 题）：注入 token 105,316 → 11,983（−88.6%），总准确率持平
  （71.9% vs 69.8%，CI 重叠），Knowledge Updates 子项 68.8% → 81.2%。
  详见 `experiments/longmemeval/RESULTS.md`。
