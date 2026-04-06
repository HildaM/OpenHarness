# 为什么整个项目都在用 `yield`？

> 这篇文档解释项目中 `yield` / `AsyncIterator` 的使用动机，
> 以及如果不用 `yield` 会怎样。

---

## 一、先看一个对比

假设 LLM 要回复 "Hello world"（10 个字符，约 3 个 token）。

### 不用 yield（一次性返回）

```python
async def run_query(context, messages) -> str:
    response = await api_client.call(messages)    # 等 3 秒...
    return response.text                           # 3 秒后一次性返回 "Hello world"
```

**用户体验**：盯着空白屏幕 3 秒 → 突然出现完整文本。

### 用 yield（流式返回）

```python
async def run_query(context, messages):
    async for event in api_client.stream_message(messages):
        yield event                                # 每收到一个 token 就立刻传出去
```

**用户体验**：50ms 后出现 "Hello"，80ms 后出现 " world"——逐字显示，像打字一样。

**这就是用 yield 的根本原因：让用户看到实时进展，而不是等完才显示。**

---

## 二、如果不用 yield，代码会变成什么样？

### 方案 A：一次性返回（最简单，体验最差）

```python
# query.py — 不用 yield
async def run_query(context, messages) -> list[StreamEvent]:
    all_events = []
    for turn in range(max_turns):
        response = await api_client.call(messages)         # 等待完整响应
        all_events.append(TurnComplete(response))
        if not response.tool_uses:
            break
        for tc in response.tool_uses:
            result = await execute_tool(tc)
            all_events.append(ToolCompleted(result))
    return all_events                                       # 全部完成后一次性返回

# handle_line — 等全部做完才渲染
events = await run_query(context, messages)
for event in events:
    await render_event(event)                               # 等了 30 秒才开始渲染
```

**问题**：Agent 可能执行 8 轮、读 5 个文件、改 2 个文件，总共花 30 秒。用户在这 30 秒里什么都看不到。

### 方案 B：回调函数（可行，但嵌套地狱）

```python
# query.py — 用回调
async def run_query(context, messages, on_event: Callable):
    for turn in range(max_turns):
        async for chunk in api_client.stream(messages):
            await on_event(TextDelta(chunk.text))          # 通过回调传出去
        await on_event(TurnComplete(response))
        for tc in response.tool_uses:
            await on_event(ToolStarted(tc))
            result = await execute_tool(tc)
            await on_event(ToolCompleted(result))

# handle_line — 传回调进去
await run_query(context, messages, on_event=render_event)
```

**问题**：可以工作，但 `run_query` → `submit_message` → `handle_line` 每层都要传回调，
而且无法在外层用 `async for` 优雅地消费事件，失去了 Python 异步生成器的优势。

### 方案 C：yield（当前项目的做法）

```python
# query.py — 用 yield
async def run_query(context, messages):
    for turn in range(max_turns):
        async for chunk in api_client.stream(messages):
            yield TextDelta(chunk.text)                    # 立即传出
        yield TurnComplete(response)
        for tc in response.tool_uses:
            yield ToolStarted(tc)
            result = await execute_tool(tc)
            yield ToolCompleted(result)

# handle_line — 用 async for 消费
async for event in engine.submit_message(line):
    await render_event(event)                              # 每个事件立即渲染
```

**优势**：
- 生产者（run_query）和消费者（handle_line）**解耦**——各自只关心自己的逻辑
- 多层可以**透明穿透**——yield 一路向上传递，中间层不需要知道事件内容
- 消费者可以用 `async for` 这种 Python 原生语法

---

## 三、yield 在项目中的 4 层穿透

```python
# 第 4 层：query.py — 生产事件
async def run_query(context, messages):
    async for api_event in api_client.stream_message(...):
        yield AssistantTextDelta(text=api_event.text), None   # 产出

# 第 3 层：query_engine.py — 透传 + 记账
async def submit_message(self, prompt):
    async for event, usage in run_query(context, self._messages):
        if usage: self._cost_tracker.add(usage)               # 做一点自己的事
        yield event                                            # 继续向上传

# 第 2 层：runtime.py handle_line() — 消费 + 委托渲染
async for event in bundle.engine.submit_message(line):
    await render_event(event)                                  # 交给回调

# 第 1 层：backend_host.py _render_event() — 最终消费者
async def _render_event(event):
    await self._emit(BackendEvent(type="assistant_delta", message=event.text))
    # → stdout → 前端 → 用户屏幕
```

**一个 token 从 LLM 服务器到用户屏幕的延迟 < 10ms**，因为每一层都是收到就传，没有缓冲。

如果用「一次性返回」，第 3 层就必须等第 4 层全部完成，第 2 层等第 3 层完成……最终用户等所有层都完成才能看到第一个字。

---

## 四、yield 的另一个好处：中间层可以"加工"

`query_engine.py` 在透传事件的同时，还做了 Token 成本累加：

```python
async for event, usage in run_query(context, self._messages):
    if usage is not None:
        self._cost_tracker.add(usage)    # ← 中间层的"副作用"
    yield event                           # ← 照传不误
```

如果用回调模式，`query_engine` 就不得不包装一个新的回调：

```python
# 回调模式下，中间层被迫包装
async def _wrapped_callback(event, usage):
    if usage: self._cost_tracker.add(usage)
    await original_callback(event)                 # 层层包装，越来越深

await run_query(context, messages, on_event=_wrapped_callback)
```

yield 模式天然支持中间层加工，代码更扁平。

---

## 五、yield 的本质：惰性求值的管道

可以把 yield 想象成**水管**：

```
LLM API                                                      用户屏幕
  │                                                              │
  │ 滴：token "Hello"                                            │
  ▼                                                              │
[api_client]──yield──→[run_query]──yield──→[submit_message]──yield──→[handle_line]──callback──→[前端]
  │                      │                     │                                        │
  │ 滴：token " world"   │ 顺便记账            │                                       │
  ▼                      ▼                     ▼                                        ▼
  立刻流到下一段        立刻流到下一段         立刻流到下一段                            立刻显示
```

**每一段管道都不存水（不缓冲）**，水滴（token）流进就流出。

如果用一次性返回，就变成了：

```
[api_client] → 装满一桶水 → [run_query] → 倒进另一个桶 → [submit_message] → 再倒一次 → ...
```

用户要等所有桶都倒完才能喝到水。

---

## 六、Python 的 `async for` + `yield` 语法快速入门

如果你对 Python 的异步生成器不熟，这是最小的示例：

```python
import asyncio

# 生产者：用 yield 产出数据
async def count_slowly():
    for i in range(5):
        await asyncio.sleep(0.5)    # 模拟等待 LLM 返回
        yield i                      # 每产出一个就暂停，等消费者取走

# 消费者：用 async for 逐个获取
async def main():
    async for number in count_slowly():
        print(f"Got: {number}")      # 每 0.5 秒打印一个，不是等 2.5 秒才全部打印

asyncio.run(main())
# 输出（每行间隔 0.5 秒）：
# Got: 0
# Got: 1
# Got: 2
# Got: 3
# Got: 4
```

**yield 的执行流程**：
1. `count_slowly()` 不会立即执行，返回一个 AsyncIterator 对象
2. `async for` 调用 `__anext__()` → 函数执行到 `yield i` → 暂停，返回 `i`
3. 消费者处理完 → 再次 `__anext__()` → 函数从暂停处继续 → 到下一个 `yield`
4. 函数正常结束 → 抛出 `StopAsyncIteration` → `async for` 循环结束

**关键**：生产者和消费者**交替执行**，不是一方运行完再轮到另一方。

---

## 七、项目中的 yield 汇总

| 位置 | yield 什么 | 为什么 |
|------|-----------|--------|
| `api/client.py` stream_message | `ApiTextDeltaEvent` / `ApiMessageCompleteEvent` | LLM 流式返回，每个 token 立即传出 |
| `engine/query.py` run_query | `(StreamEvent, UsageSnapshot)` | Agent 循环的每一步进展立即传出 |
| `engine/query_engine.py` submit_message | `StreamEvent` | 透传 + Token 记账 |
| `engine/query_engine.py` continue_pending | `StreamEvent` | 同上（/continue 场景） |

**全部服务于同一个目标**：让用户实时看到 Agent 在做什么。

---

## 八、一句话总结

**`yield` 让数据像流水一样逐个穿透 4 层到达用户，而不是等全部完成后倒一桶给用户。** 这是 AI Agent 类应用的标配模式——因为 LLM 生成很慢（几秒到几十秒），用户不能接受等完才显示。
