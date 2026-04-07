# 12 — API 客户端：LLM 调用的底层引擎

> 涉及源文件：`api/client.py` (186行) · `api/openai_client.py` (343行) · `api/copilot_client.py` (131行) · `api/copilot_auth.py` (245行) · `api/provider.py` (97行) · `api/errors.py` (20行) · `api/usage.py` (18行) · `api/__init__.py` (20行)
>
> 预计阅读时间：40 分钟
>
> 前置知识：已理解 Agent 循环（06）、engine 包全景（07）、工具系统（10-11）

---

## 本章核心问题

在 `run_query()` 的阶段 B 中，一行代码就能调用 LLM：

```python
async for event in context.api_client.stream_message(request):
```

但这行代码背后，`api_client` 到底是什么？它怎么支持 Anthropic、OpenAI、Copilot 三种完全不同的 API？流式数据怎么从 LLM 服务器到达你的终端？出错了怎么重试？

---

## 一、全景架构图

```
                           SupportsStreamingMessages (Protocol)
                                      │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
           AnthropicApiClient  OpenAICompatibleClient  CopilotClient
           (client.py:98)      (openai_client.py:170)  (copilot_client.py:48)
                    │                 │                 │
                    │                 │                 └─── 内部持有 ──→ OpenAICompatibleClient
                    │                 │                        (委托模式)
                    ▼                 ▼
           anthropic SDK       openai SDK
           AsyncAnthropic      AsyncOpenAI
                    │                 │
                    └────────┬────────┘
                             ▼
                     HTTP/SSE → LLM 服务器
```

**关键洞察**：三个客户端不共享基类，而是通过 **Python Protocol**（结构化子类型）实现统一接口。只要有 `stream_message` 方法，就能被引擎使用。

---

## 二、协议层：一个接口统一三种 API

### 2.1 请求数据类型

```python
# client.py:30-38
@dataclass(frozen=True)
class ApiMessageRequest:
    """Input parameters for a model invocation."""

    model: str                                              # 模型名 如 "claude-sonnet-4-20250514"
    messages: list[ConversationMessage]                     # 对话历史
    system_prompt: str | None = None                        # System Prompt
    max_tokens: int = 4096                                  # 单次最大输出
    tools: list[dict[str, Any]] = field(default_factory=list)  # 工具定义（JSON Schema）
```

**设计选择**：用 `@dataclass(frozen=True)` 而非 Pydantic BaseModel。为什么？

因为这是**内部传输对象**，不需要：
- 校验（数据来自可信的内部代码，不像工具参数来自 LLM）
- JSON Schema 导出（不需要给 LLM 看）
- 序列化/反序列化（在进程内传递，不跨网络）

`frozen=True` 意味着**不可变**——创建后不能修改任何字段，这是防御性编程。

### 2.2 流事件类型

```python
# client.py:41-57
@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """Incremental text produced by the model."""
    text: str

@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """Terminal event containing the full assistant message."""
    message: ConversationMessage    # 完整的 assistant 消息
    usage: UsageSnapshot            # Token 用量
    stop_reason: str | None = None  # "end_turn" / "tool_use" / "max_tokens"

ApiStreamEvent = ApiTextDeltaEvent | ApiMessageCompleteEvent
```

这是一个**两事件协议**，极其简洁：

```
流开始
  ├── TextDeltaEvent("Hello")     ← 模型产出第一个 token
  ├── TextDeltaEvent(" World")    ← 模型产出第二个 token
  ├── TextDeltaEvent("!")         ← ...
  └── MessageCompleteEvent(...)   ← 流结束，携带完整消息 + 用量
```

**为什么不是单个事件？** 因为分离的好处是：
- `TextDelta` 可以**立即 yield 给前端**（零缓冲流式传输）
- `MessageComplete` 才触发后续逻辑（保存历史、检查工具调用等）

### 2.3 Protocol 定义

```python
# client.py:60-64
class SupportsStreamingMessages(Protocol):
    """Protocol used by the query engine in tests and production."""

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Yield streamed events for the request."""
```

**这是整个 API 层最重要的 4 行代码。**

Protocol 的含义：**任何类只要有一个签名匹配的 `stream_message` 方法，就自动满足这个协议**——不需要显式继承。

这就是为什么三种客户端可以互相替换：

```python
# runtime.py:199-221 — build_runtime 第 3 步
if api_client:                          # 外部注入（测试用）
    resolved_api_client = api_client
elif settings.api_format == "copilot":  # GitHub Copilot
    resolved_api_client = CopilotClient(model=copilot_model)
elif settings.api_format == "openai":   # OpenAI 兼容
    resolved_api_client = OpenAICompatibleClient(api_key=..., base_url=...)
else:                                   # 默认 Anthropic
    resolved_api_client = AnthropicApiClient(api_key=..., base_url=...)
```

之后引擎代码里只写 `context.api_client.stream_message(request)`，**完全不知道也不关心**底层是哪种 API。

> **设计模式**：这是 **策略模式（Strategy Pattern）** 的 Python 实现。传统面向对象语言（Java/C#）需要定义一个接口 + 显式 implements，Python 用 Protocol 就够了——**鸭子类型的形式化表达**。

---

## 三、Anthropic 客户端——标杆实现

### 3.1 构造：一行创建 SDK 客户端

```python
# client.py:98-105
class AnthropicApiClient:
    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)     # ← Anthropic 官方异步 SDK
```

`AsyncAnthropic` 是 Anthropic 官方 Python SDK 提供的异步客户端，内部使用 `httpx` 作为 HTTP 客户端。

### 3.2 流式调用：两层结构

```
stream_message()          ← 外层：重试循环
    └── _stream_once()    ← 内层：单次 API 调用
```

**内层 `_stream_once()`——单次流式调用**：

```python
# client.py:138-176
async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
    params: dict[str, Any] = {
        "model": request.model,
        "messages": [message.to_api_param() for message in request.messages],
        "max_tokens": request.max_tokens,
    }
    if request.system_prompt:
        params["system"] = request.system_prompt        # Anthropic: system 是独立参数
    if request.tools:
        params["tools"] = request.tools                 # 工具定义列表

    async with self._client.messages.stream(**params) as stream:    # ← SSE 流
        async for event in stream:
            # 只关心 content_block_delta 中的 text_delta 类型
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            if getattr(delta, "type", None) != "text_delta":
                continue
            text = getattr(delta, "text", "")
            if text:
                yield ApiTextDeltaEvent(text=text)          # 每个 token 立即 yield

        final_message = await stream.get_final_message()    # SDK 拼装完整响应

    yield ApiMessageCompleteEvent(                          # 最终事件
        message=assistant_message_from_api(final_message),  # SDK 对象 → 内部模型
        usage=UsageSnapshot(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        ),
        stop_reason=getattr(final_message, "stop_reason", None),
    )
```

**关键细节**：

1. **`messages.stream()`** 是 Anthropic SDK 的流式上下文管理器，内部通过 SSE（Server-Sent Events）接收数据
2. **`async for event in stream`** 逐个接收 SSE 事件（每个 token 一个 event）
3. **`stream.get_final_message()`** SDK 内部已经把所有 delta 拼接好了，直接拿完整消息
4. **`assistant_message_from_api()`** 将 SDK 的对象转成内部的 `ConversationMessage`（见 `messages.py:91`）

**消息格式转换链**：

```
Anthropic SDK Message 对象
    ↓ assistant_message_from_api()    (messages.py:91)
ConversationMessage(role="assistant", content=[TextBlock, ToolUseBlock, ...])
    ↓ to_api_param()                  (messages.py:62) — 下一轮发送时反向转换
{"role": "assistant", "content": [{"type": "text", "text": "..."}, ...]}
```

### 3.3 重试机制：指数退避 + 抖动 + Retry-After

**外层 `stream_message()`**：

```python
# client.py:107-136
async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):      # 最多 4 次（0, 1, 2, 3）
        try:
            async for event in self._stream_once(request):
                yield event
            return                                # 成功 → 结束
        except OpenHarnessApiError:
            raise                                 # 认证错误不重试，直接抛出 ↑
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_RETRIES or not _is_retryable(exc):
                raise _translate_api_error(exc)   # 不可重试 → 翻译错误 → 抛出

            delay = _get_retry_delay(attempt, exc)
            log.warning("API request failed (attempt %d/%d, status=%s), retrying in %.1fs",
                        attempt + 1, MAX_RETRIES + 1, status, delay, exc)
            await asyncio.sleep(delay)            # 等待后重试
```

**可重试的状态码**：

```python
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
```

| 状态码 | 含义 | 为什么重试 |
|--------|------|----------|
| 429 | Rate Limited | 等一会就能继续 |
| 500 | Internal Server Error | 可能是暂时的 |
| 502/503 | Bad Gateway / Service Unavailable | 服务暂时不可用 |
| 529 | Anthropic 过载 | Anthropic 特有，等一会再试 |

**退避策略**：

```python
# client.py:78-95
def _get_retry_delay(attempt: int, exc: Exception | None = None) -> float:
    # 优先级 1：服务器告诉你等多久
    if isinstance(exc, APIStatusError):
        retry_after = getattr(exc, "headers", {}).get("retry-after")
        if retry_after:
            return min(float(retry_after), MAX_DELAY)

    # 优先级 2：指数退避 + 随机抖动
    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)   # 1s → 2s → 4s → 8s（上限 30s）
    jitter = random.uniform(0, delay * 0.25)              # 0~25% 随机偏移
    return delay + jitter
```

**为什么需要 jitter（抖动）？** 想象 100 个客户端同时被 429，如果都在 2 秒后重试，服务器又会被打爆。jitter 让它们在 2.0~2.5 秒之间**随机分散**，避免「雷群效应（thundering herd）」。

---

## 四、OpenAI 兼容客户端——格式翻译器

### 4.1 为什么需要它？

Anthropic 和 OpenAI 的 API 格式有**系统性差异**：

| 方面 | Anthropic 格式 | OpenAI 格式 |
|------|---------------|-------------|
| System Prompt | 独立参数 `system=` | role="system" 的消息 |
| 工具定义 | `{"name", "description", "input_schema"}` | `{"type":"function", "function":{"name","description","parameters"}}` |
| 工具结果 | user 消息中的 `tool_result` 内容块 | 独立的 role="tool" 消息 |
| 工具调用 | assistant 消息中的 `tool_use` 内容块 | assistant 消息的 `tool_calls` 字段 |
| 流式格式 | `content_block_delta` 事件 | `chat.completion.chunk` |

OpenHarness 内部统一使用 Anthropic 格式，`OpenAICompatibleClient` 的核心职责就是**双向翻译**。

### 4.2 工具定义翻译

```python
# openai_client.py:40-58
def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Anthropic:  {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI:     {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result
```

本质上就是改了 key 名：`input_schema` → `parameters`，外面包了一层 `function`。

### 4.3 消息格式翻译

这是最复杂的翻译逻辑：

```python
# openai_client.py:61-103 — Anthropic 消息 → OpenAI 消息
def _convert_messages_to_openai(messages, system_prompt):
    openai_messages = []

    # 差异 1：system prompt 变成 system 角色消息
    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        if msg.role == "assistant":
            openai_msg = _convert_assistant_message(msg)    # 差异 2：tool_use → tool_calls
            openai_messages.append(openai_msg)
        elif msg.role == "user":
            tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
            text_blocks = [b for b in msg.content if isinstance(b, TextBlock)]

            if tool_results:
                # 差异 3：tool_result 从 user 消息中拆出来，变成独立的 role="tool" 消息
                for tr in tool_results:
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    })
            if text_blocks:
                text = "".join(b.text for b in text_blocks)
                if text.strip():
                    openai_messages.append({"role": "user", "content": text})

    return openai_messages
```

**翻译示意图**：

```
Anthropic 格式：                         OpenAI 格式：
┌─────────────────────────────┐         ┌─────────────────────────────┐
│ system_prompt = "You are..." │    →    │ {role: "system",            │
│ (独立参数)                    │         │  content: "You are..."}     │
└─────────────────────────────┘         └─────────────────────────────┘

┌─────────────────────────────┐         ┌─────────────────────────────┐
│ role: "assistant"            │         │ role: "assistant"            │
│ content: [                   │    →    │ content: "I'll read that."  │
│   TextBlock("I'll read..."), │         │ tool_calls: [{              │
│   ToolUseBlock(read_file)    │         │   id: "toolu_xxx",          │
│ ]                            │         │   type: "function",         │
└─────────────────────────────┘         │   function: {name, args}    │
                                        │ }]                          │
                                        └─────────────────────────────┘

┌─────────────────────────────┐         ┌─────────────────────────────┐
│ role: "user"                 │         │ {role: "tool",              │
│ content: [                   │    →    │  tool_call_id: "toolu_xxx", │
│   ToolResultBlock(           │         │  content: "file contents"}  │
│     "file contents")         │         └─────────────────────────────┘
│ ]                            │
└─────────────────────────────┘
```

### 4.4 流式处理：手动累积 vs SDK 自动拼装

Anthropic SDK 提供 `stream.get_final_message()` 自动拼装完整消息，但 OpenAI SDK 没有。所以 `OpenAICompatibleClient` 必须**手动累积**：

```python
# openai_client.py:209-323 — 手动累积流式数据
async def _stream_once(self, request):
    # 初始化累积变量
    collected_content = ""                          # 累积文本
    collected_reasoning = ""                        # 累积思维链（thinking models）
    collected_tool_calls: dict[int, dict] = {}      # 按 index 累积工具调用
    finish_reason: str | None = None
    usage_data: dict[str, int] = {}

    stream = await self._client.chat.completions.create(
        model=request.model,
        messages=openai_messages,
        max_tokens=request.max_tokens,
        stream=True,
        stream_options={"include_usage": True},     # 请求返回用量信息
    )

    async for chunk in stream:
        if not chunk.choices:
            # 用量独占 chunk（某些 provider 在最后发送）
            if chunk.usage:
                usage_data = {
                    "input_tokens": chunk.usage.prompt_tokens or 0,
                    "output_tokens": chunk.usage.completion_tokens or 0,
                }
            continue

        delta = chunk.choices[0].delta

        # ① 累积 reasoning_content（思考模型如 Kimi k2.5 专有）
        reasoning_piece = getattr(delta, "reasoning_content", None) or ""
        if reasoning_piece:
            collected_reasoning += reasoning_piece

        # ② 流式文本 → 立即 yield（零缓冲！）
        if delta.content:
            collected_content += delta.content
            yield ApiTextDeltaEvent(text=delta.content)

        # ③ 累积工具调用（OpenAI 按 index 分块发送）
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index                        # 第 N 个工具调用
                if idx not in collected_tool_calls:
                    collected_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                entry = collected_tool_calls[idx]
                if tc_delta.id:
                    entry["id"] = tc_delta.id               # 可能跨多个 chunk 才完整
                if tc_delta.function:
                    if tc_delta.function.name:
                        entry["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        entry["arguments"] += tc_delta.function.arguments  # 参数 JSON 分段到达
```

**工具调用为什么要按 index 累积？**

OpenAI 的流式 API 把一个工具调用拆成多个 chunk 发送：

```
chunk 1: tool_calls[0].id = "call_abc"
chunk 2: tool_calls[0].function.name = "read_file"
chunk 3: tool_calls[0].function.arguments = '{"path":'
chunk 4: tool_calls[0].function.arguments = ' "main.py"}'
```

所以必须用 `index` 作为 key，逐步拼接 `id`、`name`、`arguments`。

**流结束后，组装最终消息**：

```python
    # 组装 ContentBlock 列表
    content: list[ContentBlock] = []
    if collected_content:
        content.append(TextBlock(text=collected_content))

    for _idx in sorted(collected_tool_calls.keys()):
        tc = collected_tool_calls[_idx]
        if not tc["name"]:
            continue                                # 跳过幻影工具调用（某些 provider 的 bug）
        args = json.loads(tc["arguments"])
        content.append(ToolUseBlock(id=tc["id"], name=tc["name"], input=args))

    final_message = ConversationMessage(role="assistant", content=content)

    # 暂存 reasoning（下次发回 API 时需要回放）
    if collected_reasoning:
        final_message._reasoning = collected_reasoning      # monkey-patch 属性

    yield ApiMessageCompleteEvent(message=final_message, usage=..., stop_reason=finish_reason)
```

### 4.5 Thinking Models 支持

某些模型（如 Kimi k2.5）会在回复前输出「思维过程」，通过 `reasoning_content` 字段返回：

```
chunk: delta.reasoning_content = "让我分析一下用户的需求..."    ← 思考过程（不展示给用户）
chunk: delta.reasoning_content = "需要读取 main.py 文件..."
chunk: delta.content = "I'll read the file for you."            ← 正式回复（展示给用户）
```

**问题**：下一轮对话发回 API 时，这些思考模型要求 assistant 消息**必须带回 `reasoning_content` 字段**，否则会报错。

**解决方案**：

1. **收集时**：`collected_reasoning` 累积所有思考片段
2. **存储时**：`final_message._reasoning = collected_reasoning`（monkey-patch 到消息对象上）
3. **发回时**：`_convert_assistant_message()` 检查 `msg._reasoning` 并写入 `openai_msg["reasoning_content"]`

```python
# openai_client.py:106-143
def _convert_assistant_message(msg):
    openai_msg = {"role": "assistant"}
    openai_msg["content"] = content if content else None

    # 回放 reasoning_content
    reasoning = getattr(msg, "_reasoning", None)
    if reasoning:
        openai_msg["reasoning_content"] = reasoning
    elif tool_uses:
        # 思考模型要求此字段存在，即使为空
        openai_msg["reasoning_content"] = ""

    return openai_msg
```

---

## 五、Copilot 客户端——委托模式的典范

### 5.1 架构：不重写，只包装

`CopilotClient` 自己不实现流式逻辑，而是**持有一个 `OpenAICompatibleClient`** 并委托给它：

```python
# copilot_client.py:48-131
class CopilotClient:
    def __init__(self, github_token=None, *, enterprise_url=None, model=None):
        # 加载 OAuth token
        auth_info = load_copilot_auth()
        token = github_token or auth_info.github_token

        # 确定 API base URL
        base_url = copilot_api_base(enterprise_url)

        # 自定义 Headers（Copilot 特有）
        default_headers = {
            "User-Agent": "openharness/0.1.0",
            "Openai-Intent": "conversation-edits",
        }

        # 关键技巧：先创建带自定义 headers 的 AsyncOpenAI
        raw_openai = AsyncOpenAI(
            api_key=token,
            base_url=base_url,
            default_headers=default_headers,
        )

        # 再创建 OpenAICompatibleClient
        self._inner = OpenAICompatibleClient(api_key=token, base_url=base_url)

        # 偷梁换柱：替换内部的 SDK 客户端，让它带上 Copilot 的 headers
        self._inner._client = raw_openai          # ← 核心手法
```

**为什么不直接继承 `OpenAICompatibleClient`？**

因为继承会耦合 `__init__` 的签名和行为。Copilot 的初始化逻辑（OAuth token 加载、自定义 headers）跟 OpenAI 客户端完全不同，委托模式更灵活：

- 改 headers？只改 CopilotClient 的 `__init__`
- 改流式逻辑？改 OpenAICompatibleClient 就行，Copilot 自动跟随
- 测试？可以 mock `_inner` 而不需要 mock 整个继承链

### 5.2 stream_message：模型覆盖 + 透明委托

```python
# copilot_client.py:112-131
async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
    # 如果构造时指定了 model，覆盖请求中的 model
    effective_model = self._model or request.model
    patched = ApiMessageRequest(
        model=effective_model,
        messages=request.messages,
        system_prompt=request.system_prompt,
        max_tokens=request.max_tokens,
        tools=request.tools,
    )
    # 完全委托给内部的 OpenAI 客户端
    async for event in self._inner.stream_message(patched):
        yield event
```

因为 `ApiMessageRequest` 是 `frozen=True`（不可变），不能直接改 `model` 字段，所以创建一个新的 `patched` 请求——这就是不可变数据结构的标准用法。

---

## 六、Copilot OAuth 认证：设备流

### 6.1 为什么用「设备流」？

传统 OAuth 需要浏览器跳转 + 回调 URL，但 OpenHarness 是终端应用，没有浏览器。**设备流（Device Flow）** 专为这种场景设计：

```
终端                                GitHub
  │                                    │
  ├── POST /login/device/code ────────→│   步骤 1：获取设备码
  │←── device_code + user_code ────────┤
  │                                    │
  │   显示: "请访问 https://github.com/login/device"
  │   显示: "输入代码: ABCD-1234"
  │                                    │
  ├── POST /login/oauth/access_token ──→│   步骤 2：轮询（每 5 秒）
  │←── "authorization_pending" ────────┤   用户还没输入
  │                                    │
  ├── POST /login/oauth/access_token ──→│   步骤 2：继续轮询
  │←── access_token=gho_xxx ───────────┤   用户已授权！
  │                                    │
  └── 保存到 ~/.openharness/copilot_auth.json
```

### 6.2 代码实现

```python
# copilot_auth.py:157-181 — 步骤 1：请求设备码
def request_device_code(*, client_id=COPILOT_CLIENT_ID, github_domain="github.com"):
    resp = httpx.post(
        f"https://{github_domain}/login/device/code",
        json={"client_id": client_id, "scope": "read:user"},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    data = resp.json()
    return DeviceCodeResponse(
        device_code=data["device_code"],     # 内部 ID（不给用户看）
        user_code=data["user_code"],         # 用户要输入的短码
        verification_uri=data["verification_uri"],
        interval=data.get("interval", 5),    # 推荐轮询间隔
        expires_in=data.get("expires_in", 900),
    )
```

```python
# copilot_auth.py:184-244 — 步骤 2：轮询等待用户授权
def poll_for_access_token(device_code, interval, ...):
    poll_interval = float(interval)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        time.sleep(poll_interval + _POLL_SAFETY_MARGIN)    # 3 秒安全边距
        resp = httpx.post(
            f"https://{github_domain}/login/oauth/access_token",
            json={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()

        if "access_token" in data:
            return data["access_token"]        # ← 成功！

        error = data.get("error", "")
        if error == "authorization_pending":
            continue                            # 用户还没输入，继续等
        if error == "slow_down":
            poll_interval += 5.0                # 服务器要求降速
            continue
        raise RuntimeError(f"OAuth failed: {data.get('error_description')}")

    raise RuntimeError("OAuth timed out")
```

注意：这里用的是 **`httpx`（同步模式）** 而非 `asyncio`，因为设备流认证是从 CLI 命令 `oh auth copilot-login` 调用的，CLI 不需要异步。

### 6.3 Token 持久化

```python
# copilot_auth.py:96-112
def save_copilot_auth(token, *, enterprise_url=None):
    path = get_config_dir() / "copilot_auth.json"    # ~/.openharness/copilot_auth.json
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"github_token": token}
    if enterprise_url:
        payload["enterprise_url"] = enterprise_url
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)                                  # ← 仅文件所有者可读写（安全！）
```

`0o600` = `-rw-------`，防止其他用户读取 OAuth token。

---

## 七、Provider 检测——启发式识别

```python
# provider.py:20-79
def detect_provider(settings: Settings) -> ProviderInfo:
    if settings.api_format == "copilot":
        return ProviderInfo(name="github-copilot", auth_kind="oauth_device", ...)

    base_url = (settings.base_url or "").lower()
    model = settings.model.lower()

    # 关键词启发式匹配
    if "moonshot" in base_url or model.startswith("kimi"):
        return ProviderInfo(name="moonshot-anthropic-compatible", ...)
    if "dashscope" in base_url or model.startswith("qwen"):
        return ProviderInfo(name="dashscope-openai-compatible", ...)
    if "models.inference.ai.azure.com" in base_url or "github" in base_url:
        return ProviderInfo(name="github-models-openai-compatible", ...)
    if "bedrock" in base_url:
        return ProviderInfo(name="bedrock-compatible", ...)
    if "vertex" in base_url or "aiplatform" in base_url:
        return ProviderInfo(name="vertex-compatible", ...)
    if base_url:
        return ProviderInfo(name="anthropic-compatible", ...)

    return ProviderInfo(name="anthropic", ...)       # 默认
```

**注意**：Provider 检测**仅用于 UI 显示**（StatusBar 上显示 provider 名字和认证状态），不影响客户端行为。客户端类型由 `settings.api_format` 决定。

Provider 检测流程图：

```
settings.api_format
    │
    ├── "copilot"  → github-copilot
    │
    └── 其他 → 检查 base_url / model 关键词
                │
                ├── moonshot / kimi    → moonshot-anthropic-compatible
                ├── dashscope / qwen   → dashscope-openai-compatible
                ├── github / azure     → github-models-openai-compatible
                ├── bedrock            → bedrock-compatible
                ├── vertex/aiplatform  → vertex-compatible
                ├── 有自定义 base_url  → anthropic-compatible
                └── 默认               → anthropic
```

---

## 八、错误处理体系

### 8.1 错误层次

```python
# errors.py — 整个文件只有 20 行
OpenHarnessApiError(RuntimeError)        # 基类
    ├── AuthenticationFailure            # 401/403 — 认证失败（不重试）
    ├── RateLimitFailure                 # 429 — 速率限制（可重试）
    └── RequestFailure                   # 其他 — 通用错误（可重试/不可重试）
```

### 8.2 错误翻译

每个客户端负责把自己 SDK 的异常翻译成统一的错误类型：

```python
# client.py:179-185 — Anthropic 客户端的翻译器
def _translate_api_error(exc: APIError) -> OpenHarnessApiError:
    name = exc.__class__.__name__
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return AuthenticationFailure(str(exc))
    if name == "RateLimitError":
        return RateLimitFailure(str(exc))
    return RequestFailure(str(exc))

# openai_client.py:334-342 — OpenAI 客户端的翻译器
@staticmethod
def _translate_error(exc: Exception) -> OpenHarnessApiError:
    status = getattr(exc, "status_code", None)
    if status == 401 or status == 403:
        return AuthenticationFailure(str(exc))
    if status == 429:
        return RateLimitFailure(str(exc))
    return RequestFailure(str(exc))
```

**设计原则**：上层代码（`run_query`、`handle_line`）只需要 catch `OpenHarnessApiError`，不需要知道底层是 Anthropic 的 `APIStatusError` 还是 OpenAI 的某个异常——这就是**异常翻译层**的价值。

### 8.3 两个客户端的重试差异对比

| 特性 | AnthropicApiClient | OpenAICompatibleClient |
|------|--------------------|------------------------|
| 最大重试 | 3 次 | 3 次 |
| 可重试码 | {429, 500, 502, 503, 529} | {429, 500, 502, 503} |
| 退避策略 | 指数退避 + 25% 随机 jitter | 纯指数退避（无 jitter） |
| Retry-After | ✅ 支持 | ❌ 不支持 |
| 认证错误 | 立即抛出 | 立即抛出 |
| 网络错误 | ConnectionError/TimeoutError/OSError 可重试 | 同上 |

---

## 九、Token 用量追踪

```python
# usage.py — 整个文件 18 行
class UsageSnapshot(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
```

用量数据的传递链路：

```
LLM API 响应
    ↓ (各客户端的 _stream_once 提取)
ApiMessageCompleteEvent(usage=UsageSnapshot(input=X, output=Y))
    ↓ (run_query yield)
(AssistantTurnComplete, usage)
    ↓ (submit_message 捕获)
self._cost_tracker.add(usage)
    ↓ (/status 命令读取)
engine.total_usage → 显示给用户
```

---

## 十、完整数据流：一次 LLM 调用的旅程

以 Anthropic 客户端为例，从用户输入到看到回复的**完整链路**：

```
用户输入 "Read main.py"
    │
    ▼
handle_line(line="Read main.py")
    │
    ▼
engine.submit_message("Read main.py")
    │  append ConversationMessage.from_user_text("Read main.py")
    ▼
run_query(context, messages)
    │
    ├── 阶段 A: auto_compact_if_needed()
    │
    ├── 阶段 B: context.api_client.stream_message(request)
    │       │
    │       ▼
    │   AnthropicApiClient.stream_message()
    │       │
    │       ├── attempt 0:
    │       │   └── _stream_once(request)
    │       │       │
    │       │       ├── 构建 params: {model, messages, max_tokens, system, tools}
    │       │       │                 (messages 序列化: msg.to_api_param())
    │       │       │
    │       │       ├── self._client.messages.stream(**params)
    │       │       │   ← HTTP POST https://api.anthropic.com/v1/messages
    │       │       │   ← SSE 连接建立
    │       │       │
    │       │       ├── SSE event: content_block_delta{text_delta: "I'll"}
    │       │       │   → yield ApiTextDeltaEvent("I'll")
    │       │       │   → run_query yield (AssistantTextDelta("I'll"), None)
    │       │       │   → submit_message yield event
    │       │       │   → handle_line: await render_event(event)
    │       │       │   → 前端显示 "I'll"                   ← 用户看到第一个 token！
    │       │       │
    │       │       ├── SSE event: content_block_delta{text_delta: " read"}
    │       │       │   → 同上链路 → 前端显示 " read"
    │       │       │
    │       │       ├── SSE event: content_block_delta(tool_use: read_file)
    │       │       │   → (不产出 TextDelta，tool_use 在 final_message 中)
    │       │       │
    │       │       └── stream 结束
    │       │           → stream.get_final_message()
    │       │           → assistant_message_from_api()
    │       │           → yield ApiMessageCompleteEvent(message, usage)
    │       │
    │       └── return (成功，无需重试)
    │
    │   ← run_query 收到 MessageCompleteEvent
    │   ← messages.append(final_message)
    │   ← yield (AssistantTurnComplete, usage)
    │
    ├── 阶段 C: final_message.tool_uses → [ToolUseBlock(read_file)]
    │   → 有工具调用，继续
    │
    ├── 阶段 D: _execute_tool_call(read_file, ...)
    │   → 6 道关卡 → 读取文件 → 返回内容
    │   → messages.append(user: [ToolResultBlock(file_content)])
    │
    └── 回到阶段 A → 下一轮 LLM 调用...
```

---

## 十一、设计模式总结

| 模式 | 在哪里 | 作用 |
|------|--------|------|
| **策略模式 (Strategy)** | `SupportsStreamingMessages` Protocol | 三种客户端可互换，引擎不感知 |
| **委托模式 (Delegate)** | `CopilotClient` → `OpenAICompatibleClient` | 复用流式逻辑，只定制 headers 和认证 |
| **适配器模式 (Adapter)** | `_convert_messages_to_openai()` 等函数 | Anthropic 格式 ↔ OpenAI 格式翻译 |
| **模板方法 (Template Method)** | `stream_message` (重试) → `_stream_once` (单次) | 重试逻辑在外层，业务逻辑在内层 |
| **异常翻译 (Exception Translation)** | `_translate_api_error()` | SDK 特定异常 → 统一错误类型 |
| **不可变数据 (Immutable)** | `@dataclass(frozen=True)` | 请求/事件一旦创建不可修改 |

---

## 十二、与已学知识的关联

| 已学内容 | API 客户端的角色 |
|---------|---------------|
| **06-Agent 循环** | `run_query()` 阶段 B 调用 `api_client.stream_message()` |
| **07-engine 包** | `QueryEngine` 持有 `api_client` 引用，通过 `QueryContext` 传入 `run_query` |
| **08-消息模型** | `ConversationMessage.to_api_param()` 是发送给 API 前的最后一步序列化 |
| **10-工具系统** | `tool_registry.to_api_schema()` 生成工具定义 → 通过 `request.tools` 发给 API |
| **09-为什么用 yield** | `stream_message` 是 `AsyncIterator`，通过 yield 实现零缓冲流式传输 |

---

## 核心收获清单

1. **Protocol > 继承**：`SupportsStreamingMessages` 用 4 行代码统一了三种 API 客户端，无需共享基类
2. **内部格式统一**：所有客户端把各自的 API 格式转换为统一的 `ApiStreamEvent`，上层代码完全不感知 Provider 差异
3. **两层流式架构**：外层 `stream_message` 处理重试，内层 `_stream_once` 处理单次 API 调用
4. **手动累积 vs SDK 自动**：Anthropic SDK 有 `get_final_message()`，OpenAI SDK 需要手动按 index 累积工具调用
5. **Thinking Model 支持**：`_reasoning` monkey-patch 暂存思维链，下一轮回放
6. **设备流 OAuth**：终端应用不能浏览器跳转，用设备码 + 轮询实现认证
7. **异常翻译隔离**：每个客户端负责把自己 SDK 的异常翻译成统一类型，上层只 catch 一种

---

*下一步建议：方向 C「权限系统」—— 只有 107 行，已在 query.py 注释中看到关卡 4 的概要，快速收割。*

---

*最后更新：2026-04-07*
