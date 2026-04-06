# 第四层：前后端通信架构全文件联合分析

> **本文档跨越 7 个文件**，将 Python 后端和 Node.js 前端的代码对照阅读，
> 完整还原双进程通信的每一个细节。
>
> **建议打开方式**：VS Code 左右分屏，左边 Python，右边 TypeScript，对照阅读。

---

## 一、涉及的文件全景

```
Python 后端（左半边）                        Node.js 前端（右半边）
─────────────────────                      ─────────────────────────

ui/react_launcher.py  (116行)               frontend/terminal/src/
  ↓ 启动前端进程                                ├── index.tsx        (9行)
                                               ├── types.ts         (63行)
ui/protocol.py        (198行)                  ├── hooks/
  协议定义（两端共享语义）                        │   └── useBackendSession.ts (172行)
                                               └── components/
ui/backend_host.py    (317行)                      ├── App.tsx      (397行)
  后端主机（事件循环）                              ├── ModalHost.tsx (71行)
                                                   └── PromptInput.tsx (39行)
```

**数据流方向**：

```
用户键盘 → App.tsx → useBackendSession.ts ──stdin──→ backend_host.py → runtime.py → engine
                                          ←stdout──
用户屏幕 ← App.tsx ← useBackendSession.ts           ← backend_host.py ← engine
```

---

## 二、双进程启动：谁启动了谁？

### 第一跳：Python → Node.js

**文件**：`react_launcher.py:101-113`

```python
process = await asyncio.create_subprocess_exec(
    npm, "exec", "--", "tsx", "src/index.tsx",    # 启动 Node.js 前端
    cwd=str(frontend_dir),
    env=env,         # 含 OPENHARNESS_FRONTEND_CONFIG 环境变量
    stdin=None,      # 继承终端 stdin（前端直接读用户键盘）
    stdout=None,     # 继承终端 stdout（前端直接渲染到终端）
    stderr=None,     # 继承终端 stderr
)
```

### 第二跳：Node.js → Python

**文件**：`useBackendSession.ts:40-44`

```typescript
const [command, ...args] = config.backend_command;
// command = "/path/.venv/bin/python"
// args = ["-m", "openharness", "--backend-only", "--cwd", "..."]

const child = spawn(command, args, {
    stdio: ['pipe', 'pipe', 'inherit'],
    //       ↑       ↑        ↑
    //     stdin    stdout   stderr → 终端（debug 日志可见）
});
```

### 最终的进程结构

```
终端
 ├── npm exec tsx index.tsx          ← 前端进程（拥有终端 stdin/stdout）
 │    └── python -m openharness --backend-only  ← 后端进程（管道通信）
 │         ├── stdin  ← 前端写入
 │         ├── stdout → 前端读取
 │         └── stderr → 终端（直接可见）
 │
 └── Python 主进程（await process.wait()，已挂起等待）
```

---

## 三、协议层：两端的"合同"

### Python 端定义：`ui/protocol.py`

```python
class FrontendRequest(BaseModel):                     # 前端 → 后端
    type: Literal["submit_line", "permission_response",
                   "question_response", "list_sessions", "shutdown"]
    line: str | None = None
    request_id: str | None = None
    allowed: bool | None = None
    answer: str | None = None

class BackendEvent(BaseModel):                        # 后端 → 前端
    type: Literal["ready", "state_snapshot", "tasks_snapshot",
                   "transcript_item", "assistant_delta", "assistant_complete",
                   "line_complete", "tool_started", "tool_completed",
                   "clear_transcript", "modal_request", "select_request",
                   "error", "shutdown"]
    message: str | None = None
    item: TranscriptItem | None = None
    state: dict | None = None
    ...
```

### TypeScript 端定义：`frontend/terminal/src/types.ts`

```typescript
export type FrontendConfig = {
    backend_command: string[];           // Python 后端的启动命令
    initial_prompt?: string | null;      // 初始 prompt
};

export type BackendEvent = {
    type: string;                        // 事件类型
    message?: string | null;             // 文本内容
    item?: TranscriptItem | null;        // 对话条目
    state?: Record<string, unknown>;     // 应用状态
    modal?: Record<string, unknown>;     // 弹窗数据
    ...
};
```

**两端的 type 枚举语义相同，但没有共享代码**——Python 用 Pydantic `Literal`，TypeScript 用 `type`。这是跨语言通信的常见模式：协议通过文档（或隐式约定）保持一致。

---

## 四、数据发送与接收的对照

### 后端发送（Python 侧）

**文件**：`backend_host.py:280-283`

```python
async def _emit(self, event: BackendEvent) -> None:
    async with self._write_lock:                              # 加锁防止并发写入混乱
        sys.stdout.write("OHJSON:" + event.model_dump_json() + "\n")
        sys.stdout.flush()                                    # 立即刷新
```

**关键**：`_write_lock` 是一个 `asyncio.Lock`。因为 Agent 引擎可能并发执行多个工具，多个协程可能同时调用 `_emit()`。如果不加锁，两条 JSON 可能交织在一行内，导致前端解析失败。

### 前端接收（TypeScript 侧）

**文件**：`useBackendSession.ts:47-55`

```typescript
const reader = readline.createInterface({ input: child.stdout });

reader.on('line', (line) => {
    if (!line.startsWith('OHJSON:')) {
        // 非协议消息 → 作为日志显示（如 Python 的 print 输出）
        setTranscript((items) => [...items, { role: 'log', text: line }]);
        return;
    }
    // 协议消息 → 去掉前缀，解析 JSON
    const event = JSON.parse(line.slice('OHJSON:'.length)) as BackendEvent;
    handleEvent(event);
});
```

**`OHJSON:` 前缀的作用**：区分协议消息和普通 print 输出。后端代码中如果有 `print("debug info")`，前端不会把它当成协议消息解析崩溃，而是显示为日志。

### 前端发送（TypeScript 侧）

**文件**：`useBackendSession.ts:31-37`

```typescript
const sendRequest = (payload: Record<string, unknown>): void => {
    const child = childRef.current;
    if (!child || child.stdin.destroyed) return;
    child.stdin.write(JSON.stringify(payload) + '\n');     // 写到后端 stdin
};
```

### 后端接收（Python 侧）

**文件**：`backend_host.py:122-136`

```python
async def _read_requests(self) -> None:
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline)  # 阻塞读在线程中
        if not raw:                                                 # EOF = 前端退出
            await self._request_queue.put(FrontendRequest(type="shutdown"))
            return
        payload = raw.decode("utf-8").strip()
        request = FrontendRequest.model_validate_json(payload)      # Pydantic 校验
        await self._request_queue.put(request)                      # 放入异步队列
```

**为什么用 `asyncio.to_thread`？**
- `sys.stdin.readline()` 是**同步阻塞**调用
- 如果在 asyncio 事件循环中直接调用，会冻结整个后端
- `asyncio.to_thread` 把阻塞操作放到线程池，事件循环继续处理其他任务（如发送事件）

---

## 四点五、`backend_host.py` 与 Engine 层的关系

一个常见困惑：`backend_host.py` 怎么和 `QueryEngine` 交互？答案是**从不直接交互**，它们之间隔了 `runtime.py` 的 `handle_line()`：

```
backend_host._process_line("Fix bug")
    │  定义回调，调用 handle_line
    ▼
runtime.py handle_line()
    │  重建 System Prompt，调用 engine
    ▼
engine.submit_message("Fix bug")          ← backend_host 不直接碰这层
    │  管理历史，调用 run_query
    ▼
run_query()  →  LLM API  →  工具执行      ← 更不直接碰这层
    │
    │  yield 事件一路穿透回来：
    │  run_query → submit_message → handle_line async for → render_event 回调
    ▼
backend_host._render_event()  →  _emit()  →  stdout  →  前端
```

**两种交互方式**：
- **正向**：`_process_line()` → `handle_line()` → `engine.submit_message()` — 逐层往下调用
- **反向回调**：`engine` 内部 → `permission_prompt()` → `backend_host._ask_permission()` — 引擎通过注入的回调反向联系 backend_host

详细的边界分析见 [08-engine-compact-and-cost.md](08-engine-compact-and-cost.md) 第零节。

---

## 五、后端事件循环逐行解读

**文件**：`backend_host.py:56-120` — `ReactBackendHost.run()`

这是后端的核心循环，分 3 个阶段：

### 阶段 1：初始化（第 57-77 行）

```python
async def run(self) -> int:
    # 装配所有子系统（与 print_mode 共用同一个 build_runtime）
    self._bundle = await build_runtime(
        ...,
        permission_prompt=self._ask_permission,   # ← 注入异步权限回调
        ask_user_prompt=self._ask_question,        # ← 注入异步问答回调
    )
    await start_runtime(self._bundle)

    # 通知前端："我准备好了"
    await self._emit(BackendEvent.ready(
        self._bundle.app_state.get(),              # 应用状态
        get_task_manager().list_tasks(),            # 后台任务列表
        [f"/{cmd.name}" for cmd in self._bundle.commands.list_commands()],  # 54 个斜杠命令
    ))
    await self._emit(self._status_snapshot())      # MCP 状态等
```

对应前端处理 `ready` 事件：

```typescript
// useBackendSession.ts:72-83
if (event.type === 'ready') {
    setStatus(event.state ?? {});
    setTasks(event.tasks ?? []);
    setCommands(event.commands ?? []);           // 用于命令选择器
    // 如果有 initial_prompt，自动提交
    if (config.initial_prompt && !sentInitialPrompt.current) {
        sentInitialPrompt.current = true;
        sendRequest({ type: 'submit_line', line: config.initial_prompt });
        setBusy(true);
    }
}
```

### 阶段 2：事件循环（第 79-113 行）

```python
    reader = asyncio.create_task(self._read_requests())  # 后台读 stdin
    try:
        while self._running:
            request = await self._request_queue.get()     # 等待前端请求
```

**两个并发任务**：
- `_read_requests()` — 后台任务：不断读 stdin，放入队列
- 主循环 — 从队列取请求，逐个处理

```python
            # 路由不同请求类型
            if request.type == "shutdown":
                await self._emit(BackendEvent(type="shutdown"))
                break

            if request.type == "permission_response":
                # 找到等待中的 Future，解除挂起
                self._permission_requests[request.request_id].set_result(bool(request.allowed))
                continue

            if request.type == "question_response":
                self._question_requests[request.request_id].set_result(request.answer or "")
                continue

            if request.type == "submit_line":
                if self._busy:
                    await self._emit(BackendEvent(type="error", message="Session is busy"))
                    continue
                self._busy = True
                try:
                    should_continue = await self._process_line(line)  # ← 进入 Agent 循环
                finally:
                    self._busy = False
```

### 阶段 3：清理（第 114-120 行）

```python
    finally:
        reader.cancel()                                    # 取消 stdin 读取任务
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        if self._bundle is not None:
            await close_runtime(self._bundle)              # 关闭 MCP、触发钩子
    return 0
```

---

## 六、`_process_line()` — 事件转换的桥梁

**文件**：`backend_host.py:138-209`

这个函数的作用是**把 Agent 引擎的 `StreamEvent` 转换为前后端协议的 `BackendEvent`**。

```
engine 产出:  AssistantTextDelta("Hello")
     ↓ _render_event()
后端发送:     OHJSON:{"type":"assistant_delta","message":"Hello"}
     ↓ readline
前端处理:     setAssistantBuffer(prev => prev + "Hello")
     ↓ React 渲染
用户看到:     屏幕上出现 "Hello"
```

**四种 StreamEvent → BackendEvent 的转换对照**：

| StreamEvent | BackendEvent type | 前端处理 |
|-------------|-------------------|---------|
| `AssistantTextDelta` | `assistant_delta` | 追加到 `assistantBuffer`（实时显示） |
| `AssistantTurnComplete` | `assistant_complete` + `tasks_snapshot` | 写入 `transcript`，清空 buffer，`setBusy(false)` |
| `ToolExecutionStarted` | `tool_started` | 追加 `{role:"tool"}` 到 transcript |
| `ToolExecutionCompleted` | `tool_completed` + `tasks_snapshot` + `state_snapshot` | 追加 `{role:"tool_result"}` 到 transcript |

**注意**：工具完成后会额外发送 `tasks_snapshot` 和 `state_snapshot`，因为工具可能改变了后台任务列表或应用状态。

---

## 七、权限弹窗的完整往返

以 Agent 要执行 `file_edit` 为例，追踪消息跨越 **6 个文件**：

```
① engine/query.py:201
   decision.requires_confirmation → 调用 context.permission_prompt()
       ↓ 这个回调指向 backend_host.py 的 _ask_permission

② backend_host.py:241-259
   async def _ask_permission(self, tool_name, reason):
       request_id = uuid4().hex
       future = asyncio.get_running_loop().create_future()   # 创建 Future
       self._permission_requests[request_id] = future
       await self._emit(BackendEvent(                         # 发送给前端
           type="modal_request",
           modal={"kind":"permission", "request_id":"abc123",
                  "tool_name":"file_edit", "reason":"写操作需要确认"}
       ))
       return await future                                    # ← 在这里挂起等待

③ useBackendSession.ts:138-141
   if (event.type === 'modal_request') {
       setModal(event.modal ?? null);                         // 设置 modal 状态
   }

④ App.tsx:345-352 → ModalHost.tsx:16-38
   {session.modal ? <ModalHost modal={session.modal} ... /> : null}

   // ModalHost 渲染权限弹窗:
   ┌ Allow file_edit?
   │ 写操作需要确认
   └ [y] Allow  [n] Deny

⑤ App.tsx:209-228（用户按 y 键）
   if (session.modal?.kind === 'permission') {
       if (chunk.toLowerCase() === 'y') {
           session.sendRequest({
               type: 'permission_response',
               request_id: session.modal.request_id,     // "abc123"
               allowed: true,
           });
           session.setModal(null);                        // 关闭弹窗
       }
   }

⑥ backend_host.py:86-88（回到后端）
   if request.type == "permission_response":
       self._permission_requests["abc123"].set_result(True)
       // Future 被 resolve → _ask_permission 中的 await future 返回 True
       // → engine 继续执行工具
```

**整个往返时间线**：
```
时间   后端                        前端                      用户
0ms    engine 调用 permission_prompt
5ms    _ask_permission: 创建 Future
       发送 modal_request ───────→
10ms                               handleEvent → setModal
15ms                               React 渲染权限弹窗 ──────→ 看到弹窗
                                                              思考...
2000ms                                                        按下 y
2005ms                             sendRequest ──────────→
2010ms  队列收到 permission_response
        Future.set_result(True)
2011ms  _ask_permission 返回 True
        engine 继续执行 file_edit
```

---

## 八、流式文本渲染的对照

用户看到 "Hello world" 逐字出现，涉及 3 个文件的配合：

### 后端：把 StreamEvent 转为协议消息

```python
# backend_host.py:149-152
async def _render_event(event: StreamEvent) -> None:
    if isinstance(event, AssistantTextDelta):
        await self._emit(BackendEvent(type="assistant_delta", message=event.text))
```

每个 token 立即发送，不缓冲。

### 前端：累积 buffer + 最终写入 transcript

```typescript
// useBackendSession.ts:99-112
if (event.type === 'assistant_delta') {
    setAssistantBuffer((value) => value + (event.message ?? ''));
    // ↑ 累积到 buffer，React 实时渲染 buffer 内容
    return;
}
if (event.type === 'assistant_complete') {
    const text = event.message ?? assistantBuffer;
    setTranscript((items) => [...items, { role: 'assistant', text }]);
    // ↑ 完整文本写入 transcript（永久记录）
    setAssistantBuffer('');
    // ↑ 清空 buffer
    setBusy(false);
    // ↑ 解除 busy 状态，输入框恢复可用
}
```

### 前端：渲染到终端

```tsx
// App.tsx:337-341
<ConversationView
    items={session.transcript}          // 历史记录（已完成的回合）
    assistantBuffer={session.assistantBuffer}  // 正在流式输出的文本
    showWelcome={true}
/>
```

**两级缓冲设计的原因**：
- `assistantBuffer` — 实时变化（每个 token 更新一次），用于流式显示
- `transcript` — 稳定数据（回合完成后一次性写入），用于对话历史

如果只用 `transcript`，每个 token 都会触发整个列表重新渲染，性能差。用 buffer 分离可以让历史列表保持稳定，只有 buffer 区域实时刷新。

---

## 九、`busy` 状态管理

`busy` 标志控制"后端正在处理，前端不接受新输入"：

### 前端设置 busy

```typescript
// App.tsx:314 — 用户提交后
session.setBusy(true);

// useBackendSession.ts:107,111 — 收到完成事件后
if (event.type === 'assistant_complete') { setBusy(false); }
if (event.type === 'line_complete')      { setBusy(false); }
```

### 前端响应 busy

```tsx
// PromptInput.tsx:24-30
if (busy) {
    return <Spinner label={toolName ? `Running ${toolName}...` : undefined} />;
    // busy 时输入框变成加载动画
}
return <TextInput ... />;
// 非 busy 时显示输入框
```

### 后端防重入

```python
# backend_host.py:100-110
if self._busy:
    await self._emit(BackendEvent(type="error", message="Session is busy"))
    continue
self._busy = True
try:
    should_continue = await self._process_line(line)
finally:
    self._busy = False    # 无论成功失败都解除
```

**双重保护**：前端用 `busy` 隐藏输入框防止用户输入，后端用 `_busy` 拒绝重复请求——即使前端有 bug 发了重复请求，后端也不会并发处理。

---

## 十、进程退出的三种方式

| 触发 | 前端行为 | 后端行为 |
|------|---------|---------|
| 用户按 Ctrl+C | `sendRequest({type:"shutdown"})` → `exit()` | 收到 shutdown → break → finally 清理 |
| `/exit` 命令 | `sendRequest({type:"submit_line",line:"/exit"})` | `handle_line` 返回 `should_exit=True` → 发送 shutdown → break |
| 前端进程崩溃 | 进程退出，stdin 管道关闭 | `_read_requests` 读到 EOF → 放入 shutdown → break |

第三种是**安全网**——即使前端异常退出，后端也不会变成孤儿进程。

---

## 十一、文件阅读顺序建议

按照数据流方向阅读，从启动到通信到处理：

| 顺序 | 文件 | 行数 | 关注点 |
|------|------|------|--------|
| 1 | `react_launcher.py` | 116 | 如何启动前端 + 构建后端命令 |
| 2 | `frontend/terminal/src/types.ts` | 63 | 所有类型定义（两端共享语义） |
| 3 | `ui/protocol.py` | 198 | Python 端的协议模型 |
| 4 | `useBackendSession.ts` | 172 | 前端如何 spawn 后端 + 处理事件 |
| 5 | `backend_host.py` | 317 | 后端事件循环 + 事件转换 |
| 6 | `ModalHost.tsx` | 71 | 权限弹窗渲染 |
| 7 | `App.tsx:149-228` | ~80 | 键盘输入处理 + 权限回复 |

---

## 十二、关联阅读

| 方向 | 文件 | 说明 |
|------|------|------|
| ↑ 调用方 | `ui/app.py` run_repl() | [03-app-ui-routing.md](03-app-ui-routing.md) |
| ↓ 下游核心 | `ui/runtime.py` build_runtime() | 装配 12 个子系统——核心层的入口 |
| ↓ 下游核心 | `ui/runtime.py` handle_line() | 后端收到 submit_line 后真正的处理逻辑 |
| ↓ Agent 循环 | `engine/query.py` run_query() | 工具执行循环 |
