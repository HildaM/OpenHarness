# Agent 引擎深入：消息模型、对话压缩与成本追踪

> **前置阅读**：[06-runtime-and-agent-loop.md](06-runtime-and-agent-loop.md) 第四节 `run_query()`
>
> **本文涉及文件**：
> - `engine/messages.py`（109 行）— 消息和内容块模型
> - `engine/cost_tracker.py`（25 行）— Token 成本累积
> - `engine/stream_events.py`（50 行）— 4 种流事件
> - `services/compact/__init__.py`（493 行）— 对话压缩系统
> - `services/token_estimation.py`（16 行）— Token 估算
> - `api/usage.py`（18 行）— Token 用量快照

---

## 零、引擎的上下层交互边界（先看这节！）

在深入细节之前，先搞清楚 **engine 上下游的接口边界**——谁调用它、它调用谁、数据怎么流动。

### 分层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│  backend_host.py  _process_line()                               │
│    定义 3 个回调（_render_event / _print_system / _clear_output）│ ← 第 1 层
│    调用 handle_line(bundle, line, callbacks)                     │
└──────────────────────────┬──────────────────────────────────────┘
                           │  正向调用
┌──────────────────────────▼──────────────────────────────────────┐
│  runtime.py  handle_line()                                      │
│    重建 System Prompt                                           │ ← 第 2 层
│    调用 engine.submit_message(line)                              │
│    async for event in ...:  await render_event(event)           │
└──────────────────────────┬──────────────────────────────────────┘
                           │  正向调用              ↑ yield event
┌──────────────────────────▼──────────────────────────────────────┐
│  query_engine.py  submit_message()                              │
│    messages.append(user_msg)                                    │ ← 第 3 层
│    async for event, usage in run_query():                        │
│        cost_tracker.add(usage)                                  │
│        yield event                                               │  ← 向上透传
└──────────────────────────┬──────────────────────────────────────┘
                           │  正向调用              ↑ yield event
┌──────────────────────────▼──────────────────────────────────────┐
│  query.py  run_query()                                          │
│    auto_compact → api_client.stream_message() → 工具执行        │ ← 第 4 层
│    yield TextDelta / TurnComplete / ToolStarted / ToolCompleted  │
│                                                                  │
│    _execute_tool_call():                                         │
│      ├─ tool_registry.get(name) → tool.execute()       正向 ↓  │
│      └─ permission_prompt(name, reason) → 回调 ↑              │
└─────────────────────────────────────────────────────────────────┘
                    ↓ 正向                    ↑ 反向回调
            ┌───────┴────────┐     ┌──────────┴───────────┐
            │  API Client     │     │  backend_host.py     │
            │  (LLM 调用)    │     │  _ask_permission()   │
            │  tools/*.py    │     │  _ask_question()     │
            │  (工具执行)    │     │  (弹窗 → 用户确认)   │
            └────────────────┘     └──────────────────────┘
```

### engine 的 3 个边界接口

**① 上层接口：被 `handle_line()` 调用**

```python
# handle_line() 调用 engine 的只有这两个方法：
engine.submit_message(line)      # 新用户输入 → 启动 Agent 循环
engine.continue_pending()        # /continue 命令 → 继续中断的循环

# 都返回 AsyncIterator[StreamEvent]，上层通过 async for 消费
async for event in engine.submit_message(line):
    await render_event(event)    # 把事件传给回调（→ 前端 or stdout）
```

**② 下层接口：调用 API 和工具**

```python
# query.py 内部调用的外部组件：
api_client.stream_message(request)         # 调 LLM API
tool_registry.get(name).execute(input)     # 执行工具
permission_checker.evaluate(...)            # 检查权限
hook_executor.execute(PRE_TOOL_USE, ...)   # 触发钩子
auto_compact_if_needed(messages, ...)      # 对话压缩
```

**③ 反向回调：engine 内部回调到上层**

```python
# 这两个回调在 build_runtime() 时注入，engine 在需要时调用：
permission_prompt(tool_name, reason) → bool    # 权限确认
ask_user_prompt(question) → str                # 用户提问

# 在交互模式下，这些回调指向 backend_host.py 的 _ask_permission/_ask_question
# 在非交互模式下，这些回调指向 app.py 的 _noop_permission/_noop_ask
```

### 数据流方向总结

```
正向（用户输入 → LLM）:
  backend_host → handle_line → engine.submit_message → run_query → api_client

反向（LLM 回复 → 用户）:
  api_client → yield TextDelta → run_query → yield → submit_message → yield
  → handle_line async for → render_event 回调 → backend_host._emit → 前端

反向回调（引擎需要用户确认）:
  run_query → _execute_tool_call → permission_prompt
  → backend_host._ask_permission → _emit(modal_request) → 前端弹窗
  → 用户按 y → run() 主循环收到 permission_response → Future.set_result
  → _ask_permission 返回 → _execute_tool_call 继续
```

**核心原则**：事件只通过 `yield` 向上流动，用户交互只通过回调向下注入。engine 从不直接知道前端的存在。

---

## 一、为什么需要读这些文件？

在 events.log 实验中你看到了：

```
轮次 1: input_tokens = 6,542
轮次 8: input_tokens = 17,067    ← 仅 8 轮就涨了 2.6 倍
```

如果 Agent 执行更复杂的任务（读几十个文件、修改多处代码），input_tokens 会飙到几万甚至十几万。而模型的上下文窗口是 200K，扣除 System Prompt（~5K）和 output buffer（~20K），只剩 ~175K 给对话历史。

**这组文件解决的核心问题是**：对话历史无限增长怎么办？

```
                       对话越来越长
                            │
         ┌──────────────────┼──────────────────┐
         ↓                  ↓                  ↓
    messages.py        compact/            cost_tracker.py
    消息是什么结构？    怎么压缩？          花了多少 Token？
```

---

## 二、消息模型：`engine/messages.py`

### 三种内容块

LLM 对话中的每条消息不是纯文本，而是由**多个内容块**组成的：

```python
class TextBlock(BaseModel):        # 纯文本
    type: Literal["text"] = "text"
    text: str

class ToolUseBlock(BaseModel):     # 模型请求调用工具
    type: Literal["tool_use"] = "tool_use"
    id: str                         # 唯一标识，如 "toolu_abc123"
    name: str                       # 工具名，如 "read_file"
    input: dict[str, Any]           # 参数，如 {"path": "main.py"}

class ToolResultBlock(BaseModel):  # 工具执行结果
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str                # 对应的 ToolUseBlock.id
    content: str                    # 执行结果文本
    is_error: bool = False          # 是否出错
```

**三者通过 `type` 字段区分**（Pydantic 的 discriminator 模式）：

```python
ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]
```

### ConversationMessage

```python
class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]      # 只有两种角色
    content: list[ContentBlock]              # 一条消息包含多个内容块
```

**关键设计**：一条 assistant 消息可以**同时包含文字和工具调用**：

```python
# 模型回复："找到了 bug，帮你修复" + 调用 file_edit 工具
ConversationMessage(role="assistant", content=[
    TextBlock(text="找到了 bug，帮你修复"),
    ToolUseBlock(id="toolu_abc", name="file_edit", input={"path": "main.py", ...}),
])
```

### 对话历史的真实结构

你在 events.log 中看到的 8 轮对话，对应的 `_messages` 列表是这样的：

```python
messages = [
    # 用户消息
    ConversationMessage(role="user", content=[
        TextBlock(text="Read main.py and summarize it"),
    ]),

    # 第 1 轮 LLM 回复（直接调工具，无文字）
    ConversationMessage(role="assistant", content=[
        ToolUseBlock(id="call_6cc0", name="read_file", input={"path": "main.py"}),
    ]),

    # 第 1 轮工具结果（以 user 角色发送！）
    ConversationMessage(role="user", content=[
        ToolResultBlock(tool_use_id="call_6cc0", content="File not found: ...", is_error=True),
    ]),

    # 第 2 轮 LLM 回复（文字 + 工具调用）
    ConversationMessage(role="assistant", content=[
        TextBlock(text="The file `main.py` doesn't exist at the repository root. Let me search:"),
        ToolUseBlock(id="call_3cf4", name="glob", input={"pattern": "**/main.py"}),
    ]),

    # 第 2 轮工具结果
    ConversationMessage(role="user", content=[
        ToolResultBlock(tool_use_id="call_3cf4", content=".venv/.../main.py\n...", is_error=False),
    ]),

    # ... 第 3~8 轮类似结构 ...
]
```

**为什么工具结果以 `role="user"` 发送？** 这是 Anthropic API 的协议要求——对话必须严格交替 `user → assistant → user → assistant`。工具结果虽然不是用户说的，但在协议层面必须放在 user 消息中。

### 便捷方法

```python
msg.text          # 拼接所有 TextBlock 的文字
msg.tool_uses     # 返回所有 ToolUseBlock 列表
msg.to_api_param() # 序列化为 API 请求格式的 dict

ConversationMessage.from_user_text("Hello")  # 快速创建用户消息
assistant_message_from_api(raw_response)      # 从 API 响应反序列化
```

---

## 三、Token 估算：`services/token_estimation.py`

```python
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)    # ≈ 每 4 个字符 1 个 token
```

**极其粗糙但有效**的估算——英文平均每个 token 约 4 个字符。不需要精确（精确需要加载 tokenizer 模型，很慢），因为这只用于判断"是否需要压缩"，误差 30% 完全可以接受。

`compact/__init__.py` 中还加了 `4/3` 的安全系数：

```python
TOKEN_ESTIMATION_PADDING = 4 / 3   # 估算值 × 1.33，宁可高估也别低估

def estimate_message_tokens(messages):
    total = 0
    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextBlock):    total += estimate_tokens(block.text)
            elif isinstance(block, ToolResultBlock): total += estimate_tokens(block.content)
            elif isinstance(block, ToolUseBlock):    total += estimate_tokens(block.name + str(block.input))
    return int(total * TOKEN_ESTIMATION_PADDING)
```

---

## 四、对话压缩系统：`services/compact/__init__.py`

这是最重要的文件（493 行），实现了**两级压缩策略**。

### 什么时候触发压缩？

在 `run_query()` 的**每轮 Agent 循环开始前**调用：

```python
# engine/query.py:80-88
for turn in range(context.max_turns):
    messages, was_compacted = await auto_compact_if_needed(
        messages, api_client=..., model=..., system_prompt=..., state=compact_state,
    )
    # ... 然后才调用 LLM API ...
```

触发条件（`should_autocompact()`）：

```python
def get_autocompact_threshold(model):
    context_window = 200_000                   # 模型上下文窗口
    reserved = 20_000                          # 留给 output 的空间
    effective = context_window - reserved       # = 180,000
    buffer = 13_000                            # 安全缓冲
    return effective - buffer                   # = 167,000

def should_autocompact(messages, model, state):
    if state.consecutive_failures >= 3:        # 连续失败 3 次就放弃
        return False
    token_count = estimate_message_tokens(messages)
    return token_count >= 167_000              # 超过阈值就触发
```

**数字解读**：200K 上下文窗口中，扣除 20K output + 13K 缓冲，当对话历史估算超过 **167K token** 时触发压缩。

### 第一级：Microcompact（免费，不调 LLM）

**原理**：把旧的工具执行结果**替换为占位符**，因为结果内容通常很大但 LLM 已经"看过了"。

```python
COMPACTABLE_TOOLS = frozenset({
    "read_file", "bash", "grep", "glob",
    "web_search", "web_fetch", "edit_file", "write_file",
})

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"
```

**执行逻辑**：

```
历史中有 20 个工具结果

保留最近 5 个（keep_recent=5）的原始内容
清除前 15 个的 content → 替换为 "[Old tool result content cleared]"

效果：
  原来: ToolResultBlock(content="<1000 行文件内容>")    ← 消耗大量 Token
  清除后: ToolResultBlock(content="[Old tool result content cleared]")  ← 几乎不消耗
```

**为什么 LLM 不会受影响？** 因为 LLM 已经在之前的轮次中读过了这些内容，并基于它做出了后续决策。那些决策（assistant 消息）仍然保留在历史中。清除旧结果不会丢失"LLM 学到的知识"，只是丢失了"原始数据"。

### 第二级：Full Compact（调 LLM 做摘要）

如果 microcompact 后仍然超阈值，就调用 LLM 对旧消息做摘要。

**执行流程**：

```
原始消息:  [msg1, msg2, msg3, ..., msg20, msg21, ..., msg26]
                    ↑ 旧消息（需要摘要）        ↑ 最近 6 条（原样保留）

步骤 1: 先 microcompact（清除旧工具结果）
步骤 2: 分割 → older=[msg1~msg20], newer=[msg21~msg26]
步骤 3: 发送 older + 压缩指令给 LLM → 得到结构化摘要
步骤 4: 替换 → [summary_msg, msg21, msg22, ..., msg26]

结果:  从 26 条消息 → 7 条消息（1 条摘要 + 6 条保留）
```

**压缩指令让 LLM 生成结构化摘要**，包含 9 个必填段落：

```
1. Primary Request and Intent    — 用户到底想做什么
2. Key Technical Concepts        — 涉及的技术
3. Files and Code Sections       — 读过/改过哪些文件
4. Errors and Fixes              — 出过什么错
5. Problem Solving               — 解决了什么问题
6. All User Messages             — 用户说的原话（完整保留）
7. Pending Tasks                 — 还没做完的事
8. Current Work                  — 当前正在做什么
9. Optional Next Step            — 下一步该做什么
```

**摘要结果会用特殊格式注入到对话开头**：

```
"This session is being continued from a previous conversation that ran
out of context. The summary below covers the earlier portion..."

[结构化摘要内容]

"Recent messages are preserved verbatim."
"Continue the conversation from where it left off without asking
the user any further questions..."
```

### `auto_compact_if_needed()` — 两级联动

```python
async def auto_compact_if_needed(messages, *, api_client, model, system_prompt, state):
    # 第一关：是否需要压缩？
    if not should_autocompact(messages, model, state):
        return messages, False                  # Token 没超阈值，什么都不做

    # 第二关：先试 microcompact（免费）
    messages, tokens_freed = microcompact_messages(messages)
    if tokens_freed > 0 and not should_autocompact(messages, model, state):
        return messages, True                   # microcompact 就够了

    # 第三关：需要 full compact（调 LLM）
    try:
        result = await compact_conversation(messages, api_client=..., model=..., ...)
        state.consecutive_failures = 0
        return result, True                     # 压缩成功
    except Exception:
        state.consecutive_failures += 1         # 失败计数，连续 3 次就放弃
        return messages, False
```

**容错机制**：如果 LLM 摘要调用失败（网络错误、API 限流），记录失败次数但不崩溃，继续用原始消息。连续失败 3 次后停止尝试，防止死循环。

---

## 五、成本追踪：`engine/cost_tracker.py` + `api/usage.py`

### UsageSnapshot — 一次 LLM 调用的用量

```python
class UsageSnapshot(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens
```

**来源**：API 客户端在每次 `stream_message()` 结束时返回：

```python
# api/client.py:169-176
yield ApiMessageCompleteEvent(
    message=...,
    usage=UsageSnapshot(
        input_tokens=int(getattr(usage, "input_tokens", 0)),
        output_tokens=int(getattr(usage, "output_tokens", 0)),
    ),
)
```

### CostTracker — 整个会话的累计用量

```python
class CostTracker:
    def __init__(self):
        self._usage = UsageSnapshot()

    def add(self, usage: UsageSnapshot):
        self._usage = UsageSnapshot(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
        )
```

**在 QueryEngine 中使用**：

```python
# query_engine.py:124-126
async for event, usage in run_query(context, self._messages):
    if usage is not None:
        self._cost_tracker.add(usage)    # 每轮累加
    yield event
```

所以 `engine.total_usage` 始终是整个会话的总 Token 消耗。

---

## 六、完整的数据生命周期

从用户输入到压缩，一条消息经历的全过程：

```
用户输入 "Fix the bug"
    ↓
ConversationMessage.from_user_text("Fix the bug")
    ↓ append 到 messages 列表
    ↓
run_query 第 1 轮开始
    ↓
auto_compact_if_needed(messages)
    ↓ 估算 token：6,542 < 167,000 → 不压缩
    ↓
api_client.stream_message(messages + system_prompt + tools)
    ↓ LLM 返回 assistant 消息（含 ToolUseBlock）
    ↓ append 到 messages
    ↓
执行工具 → 返回 ToolResultBlock
    ↓ 包装成 ConversationMessage(role="user") 并 append
    ↓
run_query 第 2 轮开始
    ↓
auto_compact_if_needed(messages)
    ↓ 估算 token：6,598 < 167,000 → 不压缩
    ↓
... 重复 ...
    ↓
run_query 第 N 轮：token 估算超过 167,000！
    ↓
auto_compact_if_needed → 触发！
    ↓
第一级 microcompact：清除旧工具结果
    ├─ 够了 → 继续
    └─ 不够 → 第二级 full compact
              ↓
              调 LLM 生成结构化摘要
              ↓
              messages = [summary_msg, 最近6条消息]
              ↓
              Token 从 170K → ~30K
              ↓
              继续正常 Agent 循环
```

---

## 七、动手实验

### 实验 1：观察 Token 估算

在 Python REPL 中：

```python
from openharness.services.token_estimation import estimate_tokens
from openharness.services.compact import estimate_message_tokens
from openharness.engine.messages import ConversationMessage, TextBlock, ToolResultBlock

# 粗估：4 字符 ≈ 1 token
print(estimate_tokens("Hello world"))  # → 3

# 模拟一条包含大文件内容的工具结果
big_result = "x" * 40000  # 40K 字符 ≈ 10K token
msg = ConversationMessage(role="user", content=[
    ToolResultBlock(tool_use_id="test", content=big_result)
])
print(estimate_message_tokens([msg]))  # → ~13,333 (含 4/3 padding)
```

### 实验 2：观察 microcompact 效果

```python
from openharness.services.compact import microcompact_messages
from openharness.engine.messages import *

# 构造 10 个工具调用 + 结果
messages = []
for i in range(10):
    messages.append(ConversationMessage(role="assistant", content=[
        ToolUseBlock(id=f"call_{i}", name="read_file", input={"path": f"file{i}.py"}),
    ]))
    messages.append(ConversationMessage(role="user", content=[
        ToolResultBlock(tool_use_id=f"call_{i}", content=f"Content of file{i}.py " * 500),
    ]))

print(f"Before: {estimate_message_tokens(messages)} tokens")
messages, saved = microcompact_messages(messages, keep_recent=3)
print(f"After: {estimate_message_tokens(messages)} tokens, saved {saved}")

# 验证：前 7 个结果被清除，后 3 个保留
for msg in messages:
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            print(f"  {block.tool_use_id}: {block.content[:50]}")
```

### 实验 3：查看压缩阈值

```python
from openharness.services.compact import get_autocompact_threshold, get_context_window

for model in ["claude-sonnet-4", "claude-opus-4", "kimi-k2.5"]:
    window = get_context_window(model)
    threshold = get_autocompact_threshold(model)
    print(f"{model}: window={window:,}, compact at {threshold:,} tokens")
```

---

## 八、关联阅读

| 方向 | 文件 | 说明 |
|------|------|------|
| ↑ 调用方 | `engine/query.py` run_query() | 在每轮开始调用 auto_compact_if_needed |
| ↑ 调用方 | `engine/query_engine.py` | 持有 _messages 和 _cost_tracker |
| → API 用量来源 | `api/client.py` / `api/openai_client.py` | 每次 stream_message 返回 UsageSnapshot |
| → 消息序列化 | `engine/messages.py` to_api_param() | 消息如何转成 API 请求格式 |
| ↓ 下一步建议 | `tools/base.py` → `tools/file_read_tool.py` | 工具系统——Agent 的"手" |
