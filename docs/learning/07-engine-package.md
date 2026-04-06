# `engine/` 包全景分析 — Agent 的大脑

> **本文档对 `engine/` 包的 6 个文件进行完整分析**，重点在于理解各文件的职责边界、
> 数据如何在文件间流动、以及整个包与外部的交互接口。
>
> **前置阅读**：[06-runtime-and-agent-loop.md](06-runtime-and-agent-loop.md)、[08-engine-compact-and-cost.md](08-engine-compact-and-cost.md)

---

## 一、包内文件一览

```
engine/
├── __init__.py          (77行)  包入口：延迟导入 + 公共 API 导出
├── messages.py          (109行) 数据模型：消息 + 3 种内容块
├── stream_events.py     (50行)  数据模型：4 种流事件
├── cost_tracker.py      (25行)  Token 用量累加器
├── query_engine.py      (149行) 对话管理器：历史 + 成本 + 调度
└── query.py             (244行) Agent 循环：LLM → 工具 → 循环
```

**总计 654 行代码**——这是整个项目最核心的 654 行。

---

## 二、文件间的依赖关系

```
__init__.py ·········· 延迟导出，不含逻辑
     │
     │ 导出以下类型给外部使用
     ▼
messages.py ←──────── 被所有其他文件依赖
     │
     │ TextBlock, ToolUseBlock, ToolResultBlock, ConversationMessage
     ▼
stream_events.py ←─── 依赖 messages.py (ConversationMessage) + api/usage.py
     │
     │ AssistantTextDelta, AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
     ▼
cost_tracker.py ←──── 依赖 api/usage.py (UsageSnapshot)
     │
     │ CostTracker
     ▼
query_engine.py ←──── 依赖上面三个 + query.py
     │
     │ QueryEngine（对外接口：submit_message / continue_pending）
     ▼
query.py ←─────────── 依赖 messages + stream_events + 外部组件
                       run_query()（Agent 循环）+ _execute_tool_call()
```

**关键洞察**：依赖是**单向向下**的，没有循环依赖。`messages.py` 在最底层，`query.py` 在最顶层。

---

## 三、每个文件的详细职责

### 3.1 `__init__.py` — 延迟导入门面

```python
def __getattr__(name: str):
    if name == "QueryEngine":
        from openharness.engine.query_engine import QueryEngine
        return QueryEngine
    if name in {"ConversationMessage", "TextBlock", ...}:
        from openharness.engine.messages import ...
        return ...
    raise AttributeError(name)
```

**职责**：让外部代码能写 `from openharness.engine import QueryEngine`，而不需要知道内部模块结构。

**延迟导入的好处**：`oh mcp list` 等不需要引擎的命令，不会触发 engine 相关模块的加载，启动更快。

### 3.2 `messages.py` — 消息数据模型（109 行）

**这是整个 engine 包的「地基」**——定义了 LLM 对话中的所有数据结构。

#### 3 种内容块（ContentBlock）

```
┌─────────────────────────────────────────────────────┐
│ TextBlock          纯文本内容                         │
│   type: "text"                                       │
│   text: "I'll fix the bug"                           │
├─────────────────────────────────────────────────────┤
│ ToolUseBlock       模型请求调用工具                    │
│   type: "tool_use"                                   │
│   id: "toolu_abc123"    ← 唯一标识，关联工具结果       │
│   name: "read_file"     ← 工具名                     │
│   input: {path: "main.py"}  ← 工具参数               │
├─────────────────────────────────────────────────────┤
│ ToolResultBlock    工具执行结果                        │
│   type: "tool_result"                                │
│   tool_use_id: "toolu_abc123"  ← 对应哪个 ToolUse    │
│   content: "file contents..."   ← 结果文本           │
│   is_error: false               ← 是否出错           │
└─────────────────────────────────────────────────────┘
```

**`ToolUseBlock.id` ↔ `ToolResultBlock.tool_use_id` 的配对关系**是理解消息流的关键：

```python
# 模型回复（assistant 消息）
assistant_msg.content = [
    TextBlock(text="Let me read the file"),
    ToolUseBlock(id="toolu_abc", name="read_file", input={"path": "main.py"}),
]

# 工具结果（user 消息）
user_msg.content = [
    ToolResultBlock(tool_use_id="toolu_abc", content="<文件内容>"),  # ← id 配对
]
```

#### ConversationMessage — 一条对话消息

```python
class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]    # 只有两种角色
    content: list[ContentBlock]            # 一条消息包含多个内容块
```

**一条 assistant 消息可以同时包含文字 + 多个工具调用**，一条 user 消息可以同时包含文字 + 多个工具结果。

#### 序列化方法

| 方法 | 方向 | 用途 |
|------|------|------|
| `to_api_param()` | 内部 → API | 发送给 LLM API 之前序列化 |
| `serialize_content_block()` | 内部 → API | 单个 block 的序列化 |
| `assistant_message_from_api()` | API → 内部 | 从 Anthropic SDK 响应反序列化 |
| `from_user_text()` | 字符串 → 内部 | 快速创建用户文本消息 |
| `model_validate()` | JSON dict → 内部 | 从会话快照恢复（Pydantic） |

### 3.3 `stream_events.py` — 4 种流事件（50 行）

这 4 种事件是 **engine 包唯一对外输出的「产品」**——上层只需要消费这些事件。

```
Agent 循环内部                          上层（handle_line → 前端）
─────────────                          ──────────────────────

LLM 返回一个 token ──→ yield AssistantTextDelta ──→ 前端逐字显示
LLM 回合结束     ──→ yield AssistantTurnComplete ──→ 前端写入 transcript
工具即将执行     ──→ yield ToolExecutionStarted  ──→ 前端显示 spinner
工具执行完毕     ──→ yield ToolExecutionCompleted ──→ 前端显示结果
```

**设计特点**：
- 全部是 `@dataclass(frozen=True)` — 不可变值对象，创建后不能修改
- `StreamEvent` 是联合类型 — 上层用 `isinstance` 分支处理
- 只有 `TurnComplete` 携带 `UsageSnapshot` — 因为只有回合结束时才知道 Token 用量

### 3.4 `cost_tracker.py` — Token 用量累加器（25 行）

```python
class CostTracker:
    _usage: UsageSnapshot    # 累计值

    def add(self, usage: UsageSnapshot):
        self._usage = UsageSnapshot(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
        )

    @property
    def total(self) -> UsageSnapshot:
        return self._usage
```

**被 `QueryEngine` 持有**，在每次 `run_query` yield 出带 usage 的事件时累加。

**注意**：它只做累加，不做费用计算（不知道价格）。`/status` 命令通过 `engine.total_usage` 读取并显示给用户。

### 3.5 `query_engine.py` — 对话管理器（149 行）

**QueryEngine 是 engine 包对外的「门面」**，上层（`handle_line`）只和它交互。

#### 管理的三项状态

```python
self._messages: list[ConversationMessage] = []   # 对话历史（核心）
self._cost_tracker = CostTracker()                # Token 累计用量
self._system_prompt: str                           # 当前 System Prompt（可热更新）
```

#### 对外的两个核心方法

```python
submit_message(prompt)       # 用户新输入 → 追加消息 → 启动 Agent 循环
continue_pending()           # /continue → 不追加消息 → 从中断处继续循环
```

**两者的唯一区别**：`submit_message` 会先 `messages.append(user_msg)`，`continue_pending` 不会。

#### 热更新方法

```python
set_system_prompt(prompt)    # handle_line 每次调用前更新（记忆检索依赖用户输入）
set_model(model)             # /model 命令切换模型
set_max_turns(max_turns)     # sync_app_state 每次刷新
set_permission_checker(...)  # /permissions 切换权限模式
```

#### 关键设计：共享可变 `_messages` 列表

```python
async def submit_message(self, prompt):
    self._messages.append(user_msg)          # 追加到列表
    async for event, usage in run_query(context, self._messages):  # ← 传引用
        ...
```

`run_query` 接收的是 `self._messages` 的**引用**，循环中直接 `messages.append(assistant_msg)` 和 `messages.append(tool_results)`。所以循环结束后 `self._messages` 自动包含了完整的对话历史，不需要返回值。

### 3.6 `query.py` — Agent 循环（244 行）

**这是整个项目最核心的文件。** 包含 3 个部分：

#### 部分 1：`QueryContext` — 一次循环所需的所有依赖（第 43-58 行）

```python
@dataclass
class QueryContext:
    api_client           # LLM API 调用
    tool_registry        # 工具查找和执行
    permission_checker   # 权限判断
    cwd                  # 工作目录
    model                # 模型名
    system_prompt        # System Prompt
    max_tokens           # 最大输出 Token
    permission_prompt    # 权限确认回调（→ backend_host._ask_permission）
    ask_user_prompt      # 用户提问回调（→ backend_host._ask_question）
    max_turns            # 最大循环轮数
    hook_executor        # 钩子执行器
    tool_metadata        # 额外元数据（MCP manager 等）
```

**为什么要打包成 dataclass？** 因为 `_execute_tool_call` 需要访问大部分字段，逐个传参太冗长。

#### 部分 2：`run_query()` — Agent 循环本体（第 61-153 行）

```
for turn in range(max_turns):
    ┌─ A. auto_compact_if_needed()     Token 太多？压缩！
    │
    ├─ B. api_client.stream_message()  调用 LLM（流式）
    │      yield TextDelta × N         逐 token 输出
    │      yield TurnComplete          回合结束
    │
    ├─ C. tool_uses 为空？ → return    没有工具调用 → 循环结束
    │
    └─ D. 执行工具
           单个：顺序执行，立即 yield 事件
           多个：asyncio.gather 并发，之后 yield 事件
           messages.append(tool_results)
           → 回到 A
```

#### 部分 3：`_execute_tool_call()` — 单个工具的执行流水线（第 156-243 行）

```
输入: (tool_name, tool_use_id, tool_input)
  │
  ├─ 关卡 1: PreToolUse Hook        → 可阻止执行
  ├─ 关卡 2: registry.get(name)     → 工具不存在则报错
  ├─ 关卡 3: input_model.validate() → Pydantic 参数校验
  ├─ 关卡 4: permission_checker     → 权限检查（可能触发弹窗）
  ├─ 关卡 5: tool.execute()         → 实际执行
  └─ 关卡 6: PostToolUse Hook       → 执行后通知
  │
输出: ToolResultBlock(content=..., is_error=...)
```

**每个关卡失败都返回 `ToolResultBlock(is_error=True)`**，不抛异常。这样 LLM 能看到错误信息并调整策略（比如换个工具或换个参数）。

---

## 四、engine 包的对外接口

### 对上层（handle_line / backend_host）暴露的接口

| 接口 | 类型 | 谁调用 |
|------|------|--------|
| `engine.submit_message(prompt)` | AsyncIterator[StreamEvent] | handle_line（普通消息） |
| `engine.continue_pending()` | AsyncIterator[StreamEvent] | handle_line（/continue） |
| `engine.messages` | list[ConversationMessage] | save_session_snapshot |
| `engine.total_usage` | UsageSnapshot | save_session_snapshot |
| `engine.set_system_prompt()` | void | handle_line（每次调用前） |
| `engine.set_max_turns()` | void | sync_app_state |
| `engine.set_model()` | void | /model 命令 |
| `engine.clear()` | void | /clear 命令 |
| `engine.load_messages()` | void | 恢复会话 |

### 对下层（API / 工具 / 权限）依赖的接口

| 依赖 | 接口 | 文件 |
|------|------|------|
| LLM API | `api_client.stream_message(request)` | `api/client.py` |
| 工具系统 | `tool_registry.get(name).execute(input)` | `tools/base.py` |
| 权限系统 | `permission_checker.evaluate(...)` | `permissions/checker.py` |
| 钩子系统 | `hook_executor.execute(event, data)` | `hooks/executor.py` |
| 对话压缩 | `auto_compact_if_needed(messages)` | `services/compact/` |

### 反向回调（engine 内部调用到上层）

| 回调 | 注入方 | 触发时机 |
|------|--------|---------|
| `permission_prompt(tool, reason) → bool` | backend_host._ask_permission | 写操作权限确认 |
| `ask_user_prompt(question) → str` | backend_host._ask_question | 工具需要用户输入 |

---

## 五、消息历史的生命周期

跟踪 `_messages` 列表在一次完整交互中的变化：

```python
# 初始状态
_messages = []

# 用户输入 "Fix the bug"
# → submit_message("Fix the bug")
_messages = [
    user("Fix the bug"),                                          # submit_message append
]

# 第 1 轮 LLM 回复（直接调工具）
_messages = [
    user("Fix the bug"),
    assistant(tool_use: read_file("main.py")),                    # run_query append
]

# 第 1 轮工具结果
_messages = [
    user("Fix the bug"),
    assistant(tool_use: read_file("main.py")),
    user(tool_result: "def main():..."),                          # run_query append
]

# 第 2 轮 LLM 回复（文字 + 工具）
_messages = [
    user("Fix the bug"),
    assistant(tool_use: read_file("main.py")),
    user(tool_result: "def main():..."),
    assistant("Found the bug!" + tool_use: file_edit(...)),       # run_query append
]

# 第 2 轮工具结果
_messages = [
    ...,
    user(tool_result: "File edited successfully"),                # run_query append
]

# 第 3 轮 LLM 回复（纯文字，无工具调用 → 循环结束）
_messages = [
    ...,
    assistant("Done! The bug was a null pointer on line 42."),    # run_query append
]
# → run_query return（tool_uses 为空）
# → submit_message 结束
# → handle_line 调用 save_session_snapshot 保存到磁盘
```

---

## 六、engine 包的设计模式总结

| 模式 | 体现 | 好处 |
|------|------|------|
| **值对象** | `@dataclass(frozen=True)` 的 StreamEvent 和 ContentBlock | 不可变，线程安全，可自由传递 |
| **AsyncIterator 流式** | `run_query` / `submit_message` 用 yield | 每个 token 立即到达 UI，零缓冲 |
| **策略模式** | `permission_prompt` / `ask_user_prompt` 回调注入 | 同一引擎服务交互和非交互两种模式 |
| **共享可变状态** | `_messages` 列表传引用给 `run_query` | 循环直接 append，外部自动看到更新 |
| **门面模式** | QueryEngine 封装 run_query + CostTracker | 上层只需两个方法，不感知内部复杂性 |
| **容错不抛异常** | `_execute_tool_call` 每个关卡返回 error ToolResult | LLM 能看到错误并调整，不会中断循环 |
| **延迟导入** | `__init__.py` 的 `__getattr__` | 不需要引擎的命令不加载引擎代码 |

---

## 七、关联阅读

| 方向 | 文档/文件 | 说明 |
|------|-----------|------|
| ↑ 上层调用 | [06-runtime-and-agent-loop.md](06-runtime-and-agent-loop.md) | handle_line 如何调用 engine |
| ↑ 上层调用 | [05-frontend-backend-protocol.md](05-frontend-backend-protocol.md) | 事件如何传到前端 |
| ↓ 消息压缩 | [08-engine-compact-and-cost.md](08-engine-compact-and-cost.md) | compact 系统详解 |
| ↓ 工具系统 | `tools/base.py` → `tools/file_read_tool.py` | 工具怎么实现 |
| ↓ API 客户端 | `api/client.py` / `api/openai_client.py` | LLM 调用怎么实现 |
| ↓ 权限系统 | `permissions/checker.py` | 权限检查的完整逻辑 |
