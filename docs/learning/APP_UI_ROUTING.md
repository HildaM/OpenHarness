# `ui/app.py` — UI 路由层深度剖析

> **前置阅读**：[STARTUP_FLOW.md](STARTUP_FLOW.md)（第三层）、[MAIN_ENTRY_DEEP_DIVE.md](MAIN_ENTRY_DEEP_DIVE.md)（路径 B/C）
>
> **源文件**：`src/openharness/ui/app.py`（159 行）

---

## 一、这个文件的角色

`app.py` 是 CLI 层（`cli.py`）和运行时层（`runtime.py`）之间的 **路由桥梁**。它只做一件事：**根据运行模式，选择正确的启动方式**。

```
cli.py main()
    ↓
★ ui/app.py          ← 你正在这里
    ├─ run_repl()          → 交互模式
    └─ run_print_mode()    → 非交互模式
         ↓
ui/runtime.py        ← 下一站：装配子系统 + 处理消息
```

整个文件只导出 **2 个函数**，结构极其清晰：

| 函数 | 行数 | 触发方式 | 职责 |
|------|------|----------|------|
| `run_repl()` | 15-55 | `oh` / `oh -c` / `oh -r` | 交互模式路由 |
| `run_print_mode()` | 58-159 | `oh -p "prompt"` | 非交互模式完整生命周期 |

---

## 二、文件头部导入分析

```python
from openharness.api.client import SupportsStreamingMessages   # API 客户端 Protocol
from openharness.engine.stream_events import StreamEvent        # 流事件类型
from openharness.ui.backend_host import run_backend_host        # 后端主机（交互模式用）
from openharness.ui.react_launcher import launch_react_tui      # 前端启动器（交互模式用）
from openharness.ui.runtime import (
    build_runtime,     # 装配所有子系统 → RuntimeBundle
    close_runtime,     # 关闭资源（MCP 连接等）
    handle_line,       # 处理一行用户输入（命令 or Agent 循环）
    start_runtime,     # 触发 SESSION_START 钩子
)
```

**值得注意的**：
- `run_repl()` 依赖 `backend_host` 和 `react_launcher`（交互模式两条分支）
- `run_print_mode()` 依赖 `runtime` 模块的四个函数（完整生命周期）
- `SupportsStreamingMessages` 是一个 Protocol 类型，用于参数标注，不是具体实现

---

## 三、`run_repl()` 逐行解读

### 函数签名

```python
async def run_repl(
    *,                                              # 强制关键字参数
    prompt: str | None = None,                      # 初始 prompt（通常为 None）
    cwd: str | None = None,                         # 工作目录
    model: str | None = None,                       # 模型名
    max_turns: int | None = None,                   # 最大回合数
    base_url: str | None = None,                    # API base URL
    system_prompt: str | None = None,               # 自定义 System Prompt
    api_key: str | None = None,                     # API Key
    api_format: str | None = None,                  # anthropic / openai / copilot
    api_client: SupportsStreamingMessages | None = None,  # 外部注入客户端（测试用）
    backend_only: bool = False,                     # 是否仅启动后端
    restore_messages: list[dict] | None = None,     # 恢复的历史消息
) -> None:
```

**设计细节**：
- `*` 强制所有参数必须用关键字传递，避免位置参数的顺序错误
- `api_client` 参数允许外部注入 mock 客户端，这是为**单元测试**设计的接口
- 所有参数默认值都是 `None`/`False`，实际值在 `build_runtime()` 中从配置文件解析

### 函数体（核心路由）

```python
async def run_repl(...) -> None:
    """Run the default OpenHarness interactive application (React TUI)."""

    # ── 分支 1：仅启动后端（被 React 前端 spawn 时走这条路）──
    if backend_only:
        await run_backend_host(
            cwd=cwd, model=model, max_turns=max_turns,
            base_url=base_url, system_prompt=system_prompt,
            api_key=api_key, api_format=api_format,
            api_client=api_client, restore_messages=restore_messages,
        )
        return

    # ── 分支 2：启动 React TUI 前端（用户直接运行 oh 走这条路）──
    exit_code = await launch_react_tui(
        prompt=prompt, cwd=cwd, model=model, max_turns=max_turns,
        base_url=base_url, system_prompt=system_prompt,
        api_key=api_key, api_format=api_format,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)
```

**只有两条路径，非此即彼**：

```
run_repl()
    │
    ├─ backend_only=True   ──→  run_backend_host()
    │    谁调用？React 前端通过 --backend-only 参数 spawn 的 Python 后端
    │    做什么？启动 JSON Lines 协议的后端主机，通过 stdin/stdout 与前端通信
    │    参数差异：接收 api_client 和 restore_messages
    │
    └─ backend_only=False  ──→  launch_react_tui()
         谁调用？用户直接执行 oh
         做什么？启动 Node.js 前端进程，前端再 spawn 后端
         参数差异：不接收 api_client 和 restore_messages
         返回值：前端进程的退出码
```

### 两条分支的参数差异

| 参数 | `run_backend_host()` | `launch_react_tui()` |
|------|---------------------|---------------------|
| `api_client` | ✅ 接收 | ❌ 不接收 |
| `restore_messages` | ✅ 接收 | ❌ 不接收 |
| `prompt` | ❌ 不接收 | ✅ 接收 |

**为什么？**
- `api_client` 和 `restore_messages` 是内存中的 Python 对象，无法序列化传递给 Node.js 进程，只能在后端直接使用
- `prompt` 是字符串，可以通过环境变量传递给前端（`initial_prompt` 字段）

### `launch_react_tui()` 内部做了什么？

```
launch_react_tui()
    ↓
1. 定位前端目录: frontend/terminal/
2. 检查 node_modules/ → 不存在则 npm install
3. 构建后端启动命令: ["python", "-m", "openharness", "--backend-only", ...]
4. 将命令序列化为 JSON 环境变量: OPENHARNESS_FRONTEND_CONFIG
5. 启动 Node.js 子进程: npm exec -- tsx src/index.tsx
6. 等待子进程退出 → 返回退出码
```

### `run_backend_host()` 内部做了什么？

```
run_backend_host()
    ↓
1. ReactBackendHost(config) → 创建后端主机实例
2. host.run()
   ├─ build_runtime()       → 装配所有子系统
   ├─ start_runtime()       → 触发 SESSION_START 钩子
   ├─ emit(ready)           → 向前端发送就绪事件
   └─ 事件循环：
       ├─ 从 stdin 读取前端请求
       ├─ submit_line → handle_line() → Agent 循环
       ├─ permission_response → 解除权限确认等待
       └─ shutdown → 退出
```

---

## 四、`run_print_mode()` 逐行解读

这是非交互模式的**完整实现**，包含完整的运行时生命周期。

### 第一阶段：准备（第 58-99 行）

```python
async def run_print_mode(
    *, prompt: str, output_format: str = "text",        # prompt 是必填的
    cwd, model, base_url, system_prompt,
    append_system_prompt, api_key, api_format,
    api_client, permission_mode, max_turns,
) -> None:
```

与 `run_repl()` 的区别：
- `prompt` 是 **必填**的（`str` 而非 `str | None`）
- 多了 `output_format` 和 `permission_mode` 参数
- 多了 `append_system_prompt` 参数

```python
    # 延迟导入 4 种流事件类型（只在这个函数里需要）
    from openharness.engine.stream_events import (
        AssistantTextDelta,       # 模型产出的增量文本
        AssistantTurnComplete,    # 模型回合结束
        ToolExecutionCompleted,   # 工具执行完成
        ToolExecutionStarted,     # 工具开始执行
    )
```

```python
    # 权限回调：非交互模式无法弹窗，所以自动放行
    async def _noop_permission(tool_name: str, reason: str) -> bool:
        return True     # 永远返回 True → 放行所有工具

    # 问答回调：非交互模式无法提问，返回空字符串
    async def _noop_ask(question: str) -> str:
        return ""       # 永远返回空 → 跳过所有提问
```

**这两个 noop 回调是理解非交互模式安全模型的关键**：
- Agent 引擎在工具执行前会调用 `permission_prompt` 确认权限
- 在交互模式下，这个回调会弹出 y/n 对话框
- 在非交互模式下，用 `_noop_permission` 替代，**自动放行一切**
- 这就是为什么命令行提供了 `--permission-mode` 来让用户控制安全级别

```python
    # 装配运行时（与交互模式用同一个 build_runtime）
    bundle = await build_runtime(
        prompt=prompt,
        model=model, max_turns=max_turns, base_url=base_url,
        system_prompt=system_prompt, api_key=api_key,
        api_format=api_format, api_client=api_client,
        permission_prompt=_noop_permission,    # ← 注入 noop 权限回调
        ask_user_prompt=_noop_ask,             # ← 注入 noop 问答回调
    )
    await start_runtime(bundle)                # 触发 SESSION_START 钩子
```

### 第二阶段：执行 + 渲染（第 101-157 行）

```python
    collected_text = ""        # 累积所有模型输出文本
    events_list: list[dict] = []   # 累积所有 stream-json 事件
```

这两个变量通过 `nonlocal` 在嵌套函数中共享——这是一个 **闭包** 模式。

#### 三个渲染回调

`handle_line()` 需要三个回调函数来渲染输出。非交互模式将它们实现为直接写到 stdout/stderr：

**回调 1：`_print_system`** — 渲染系统消息

```python
    async def _print_system(message: str) -> None:
        nonlocal collected_text
        if output_format == "text":
            print(message, file=sys.stderr)      # 系统消息写到 stderr
        elif output_format == "stream-json":
            obj = {"type": "system", "message": message}
            print(json.dumps(obj), flush=True)    # JSON 写到 stdout
```

**为什么系统消息写到 stderr？** 因为 `text` 模式下，stdout 只输出 LLM 的回答。系统消息（如 "Stopped after N turns"）是元信息，不应混入 stdout，这样用户可以：
```bash
oh -p "Hello" > answer.txt      # stdout 到文件（纯 LLM 回答）
                                  # stderr 到终端（系统消息）
```

**回调 2：`_render_event`** — 渲染 Agent 引擎产出的流事件

```python
    async def _render_event(event: StreamEvent) -> None:
        nonlocal collected_text

        if isinstance(event, AssistantTextDelta):
            # 模型正在生成文本（一个 token 一个 token 地来）
            collected_text += event.text
            if output_format == "text":
                sys.stdout.write(event.text)     # 逐字写到 stdout（流式输出）
                sys.stdout.flush()               # 立即刷新（不等缓冲区满）
            elif output_format == "stream-json":
                print(json.dumps({"type": "assistant_delta", "text": event.text}))

        elif isinstance(event, AssistantTurnComplete):
            # 模型一个回合结束
            if output_format == "text":
                sys.stdout.write("\n")           # 只加一个换行
            elif output_format == "stream-json":
                print(json.dumps({"type": "assistant_complete", "text": event.message.text.strip()}))

        elif isinstance(event, ToolExecutionStarted):
            # text 格式下工具执行是静默的！只在 stream-json 中输出
            if output_format == "stream-json":
                print(json.dumps({"type": "tool_started", "tool_name": event.tool_name, ...}))

        elif isinstance(event, ToolExecutionCompleted):
            if output_format == "stream-json":
                print(json.dumps({"type": "tool_completed", "tool_name": event.tool_name, ...}))
```

**四种事件的渲染行为对比**：

| 事件 | text 模式 | json 模式 | stream-json 模式 |
|------|-----------|-----------|------------------|
| `AssistantTextDelta` | 逐字写到 stdout | 累积到 `collected_text` | 每次输出一行 JSON |
| `AssistantTurnComplete` | 写换行符 | 不输出 | 输出完整文本 JSON |
| `ToolExecutionStarted` | **静默** | 不输出 | 输出工具名 + 输入 |
| `ToolExecutionCompleted` | **静默** | 不输出 | 输出工具名 + 结果 |

**关键洞察**：`text` 格式下工具执行完全不可见，用户只看到最终文本。这是刻意设计——面向人类阅读时，工具调用是实现细节；面向程序处理时（`stream-json`），工具调用是重要的调试信息。

**回调 3：`_clear_output`** — 清屏

```python
    async def _clear_output() -> None:
        pass    # 非交互模式下清屏无意义，直接 pass
```

#### 执行 + 最终输出

```python
    # 核心：提交用户输入给 Agent 引擎
    await handle_line(
        bundle,
        prompt,                          # 用户的 prompt
        print_system=_print_system,      # 系统消息渲染器
        render_event=_render_event,      # 流事件渲染器
        clear_output=_clear_output,      # 清屏处理器
    )

    # json 格式的特殊处理：等一切结束后，一次性输出完整结果
    if output_format == "json":
        result = {"type": "result", "text": collected_text.strip()}
        print(json.dumps(result))        # 输出到 stdout
```

**三种输出格式的时序差异**：

```
text 模式：
  [实时] H-e-l-l-o- -w-o-r-l-d       ← 逐字流式
  [完成] \n

json 模式：
  [静默] ...引擎在后台运行...           ← 无任何输出
  [完成] {"type":"result","text":"Hello world"}  ← 一次性输出

stream-json 模式：
  [实时] {"type":"assistant_delta","text":"Hello"}
  [实时] {"type":"tool_started","tool_name":"read_file",...}
  [实时] {"type":"tool_completed","tool_name":"read_file",...}
  [实时] {"type":"assistant_delta","text":" world"}
  [完成] {"type":"assistant_complete","text":"Hello world"}
```

### 第三阶段：清理（第 158-159 行）

```python
    finally:
        await close_runtime(bundle)
```

`finally` 确保无论是否异常，都会执行清理：
- 关闭所有 MCP 服务器连接
- 触发 `SESSION_END` 钩子

**为什么 `run_repl()` 没有 `finally` 清理？**
- 交互模式下，清理由 `ReactBackendHost.run()` 的 `finally` 块负责（在 `backend_host.py:114-119`）
- `launch_react_tui()` 只是启动了一个子进程并等待退出，Python 主进程没有持有任何需要清理的资源

---

## 五、`handle_line()` — 两个函数共用的核心

`run_print_mode()` 和 `ReactBackendHost._process_line()` 都最终调用 `handle_line()`：

```python
# runtime.py 中的函数签名
async def handle_line(
    bundle: RuntimeBundle,         # 运行时上下文
    line: str,                     # 用户输入的一行文本
    *,
    print_system: SystemPrinter,   # 系统消息渲染回调
    render_event: StreamRenderer,  # 流事件渲染回调
    clear_output: ClearHandler,    # 清屏回调
) -> bool:                         # 返回 True 继续，False 退出
```

**关键设计**：`handle_line()` 不关心输出到哪里。它通过 **3 个回调函数** 将输出委托给调用方：
- 非交互模式 → 回调写到 stdout/stderr
- 交互模式 → 回调通过 JSON Lines 发送给前端

这就是 **策略模式（Strategy Pattern）** 的应用——同一份核心逻辑，不同的渲染策略。

```
                    handle_line(bundle, line, callbacks)
                           │
                ┌──────────┼──────────┐
                │          │          │
         print_system  render_event  clear_output
                │          │          │
    ┌───────────┤    ┌─────┤    ┌─────┤
    │           │    │     │    │     │
非交互模式:   stderr  stdout  pass
交互模式:     JSON↓   JSON↓  JSON↓ (发给前端)
```

---

## 六、流事件（StreamEvent）类型系统

`_render_event` 回调处理 4 种事件，它们定义在 `engine/stream_events.py`：

```python
@dataclass(frozen=True)
class AssistantTextDelta:
    """模型产出了一小段文本（一个或几个 token）"""
    text: str

@dataclass(frozen=True)
class AssistantTurnComplete:
    """模型的一个完整回合结束（可能还会继续，如果有工具调用）"""
    message: ConversationMessage    # 完整的回合消息
    usage: UsageSnapshot            # 本回合的 token 用量

@dataclass(frozen=True)
class ToolExecutionStarted:
    """引擎即将执行一个工具"""
    tool_name: str                  # 如 "read_file"
    tool_input: dict[str, Any]      # 如 {"file_path": "main.py"}

@dataclass(frozen=True)
class ToolExecutionCompleted:
    """一个工具执行完毕"""
    tool_name: str
    output: str                     # 工具输出内容
    is_error: bool = False          # 是否出错

# 联合类型：渲染器只需处理这 4 种
StreamEvent = AssistantTextDelta | AssistantTurnComplete | ToolExecutionStarted | ToolExecutionCompleted
```

**事件流的典型序列**：

```
用户输入 "Fix the bug in main.py"

→ AssistantTextDelta("I'll read the file first")     ← 模型思考
→ AssistantTurnComplete(message=..., usage=...)       ← 回合 1 结束
→ ToolExecutionStarted("read_file", {path: "main.py"})  ← 开始读文件
→ ToolExecutionCompleted("read_file", output="...")      ← 读完
→ AssistantTextDelta("I found the bug, fixing...")    ← 模型继续
→ AssistantTurnComplete(message=..., usage=...)       ← 回合 2 结束
→ ToolExecutionStarted("file_edit", {path: "main.py",...})
→ ToolExecutionCompleted("file_edit", output="ok")
→ AssistantTextDelta("Done! I've fixed the bug...")   ← 最终回答
→ AssistantTurnComplete(message=..., usage=...)       ← 回合 3 结束（无工具调用→结束）
```

---

## 七、两个函数的生命周期对比

```
run_repl()（交互模式）                    run_print_mode()（非交互模式）
─────────────────────────                ─────────────────────────
启动前端进程（或后端进程）                  build_runtime()
         │                                      │
    前端负责循环                            start_runtime()
    接受多轮输入                                 │
         │                               handle_line(prompt) ← 只处理一次
    前端进程退出                                 │
         │                               close_runtime()
    返回退出码                                   │
                                         函数返回

生命周期: 长（分钟~小时）                  生命周期: 短（秒~分钟）
输入次数: 多次                             输入次数: 1 次
谁管理循环: ReactBackendHost              谁管理循环: 无循环
资源清理: backend_host.py finally         资源清理: 本函数 finally
```

---

## 八、动手实验

### 实验 1：观察 text 格式的流式输出

```bash
uv run oh -p "Count from 1 to 5 slowly" 2>/dev/null
# 你会看到数字逐个出现（流式），而不是一次性显示
```

### 实验 2：对比三种输出格式

```bash
# 纯文本：只有 LLM 回答
uv run oh -p "What is 2+2?" 2>/dev/null

# JSON：完整结果对象
uv run oh -p "What is 2+2?" --output-format json 2>/dev/null

# Stream JSON：每个事件一行（能看到工具调用）
uv run oh -p "Read the README.md file" --output-format stream-json 2>/dev/null
```

### 实验 3：在 `_render_event` 中加打印观察事件流

在 `app.py` 第 114 行后加：

```python
        async def _render_event(event: StreamEvent) -> None:
            print(f"[EVENT] {type(event).__name__}: {event}", file=sys.stderr)  # 加这行
            nonlocal collected_text
            ...
```

然后运行：
```bash
uv run oh -p "Read main.py and summarize it"
# stderr 会输出每个事件的类型和内容
```

#### 实验 3 的真实运行结果

以下是实际运行 `uv run oh -p "Read main.py and summarize it"` 后的日志（已精简）：

```
[EVENT] AssistantTurnComplete: ...content=[ToolUseBlock(name='read_file', input={'path': 'main.py'})]
        usage=UsageSnapshot(input_tokens=6542, output_tokens=37)

[EVENT] ToolExecutionStarted:  tool_name='read_file', tool_input={'path': 'main.py'}
[EVENT] ToolExecutionCompleted: output='File not found: .../main.py', is_error=True

[EVENT] AssistantTextDelta: text='No'
[EVENT] AssistantTextDelta: text=' `'
[EVENT] AssistantTextDelta: text='main'
[EVENT] AssistantTextDelta: text='.py'
[EVENT] AssistantTextDelta: text='`'
[EVENT] AssistantTextDelta: text=' in'
[EVENT] AssistantTextDelta: text=' the'
[EVENT] AssistantTextDelta: text=' root'
[EVENT] AssistantTextDelta: text=' directory'
[EVENT] AssistantTextDelta: text='.'
[EVENT] AssistantTextDelta: text=' Let'
[EVENT] AssistantTextDelta: text=' me'
[EVENT] AssistantTextDelta: text=' search'
[EVENT] AssistantTextDelta: text=' for'
[EVENT] AssistantTextDelta: text=' it'
[EVENT] AssistantTextDelta: text=':'
[EVENT] AssistantTurnComplete: ...content=[TextBlock(...), ToolUseBlock(name='glob', input={'pattern': '**/main.py'})]
        usage=UsageSnapshot(input_tokens=6603, output_tokens=50)

[EVENT] ToolExecutionStarted:  tool_name='glob', tool_input={'pattern': '**/main.py'}
[EVENT] ToolExecutionCompleted: output='.venv/lib/.../main.py\n...' (13 个结果)

[EVENT] AssistantTextDelta: text='All'
[EVENT] AssistantTextDelta: text=' `'
[EVENT] AssistantTextDelta: text='main'
...
```

##### 从日志中读出的 5 个关键事实

**① 总共发了 3 次 LLM 请求（3 轮 Agent 循环）**

| 轮次 | LLM 决策 | 工具调用 | Token 消耗 |
|------|----------|---------|-----------|
| 第 1 轮 | 直接调用工具（**无文本输出**） | `read_file("main.py")` → 文件不存在 | 6542 in / 37 out |
| 第 2 轮 | 先输出文字再调工具 | `glob("**/main.py")` → 找到 .venv 里的文件 | 6603 in / 50 out |
| 第 3 轮 | 输出最终回答 | （无工具调用 → 循环结束） | ... |

**② 每个 TextDelta ≈ 1 个 token**

```
TextDelta('No')      ← token 1
TextDelta(' `')      ← token 2（注意前面的空格也是 token 的一部分）
TextDelta('main')    ← token 3
TextDelta('.py')     ← token 4
```

这是 LLM 的自回归生成特性：模型逐 token 生成，API 服务端通过 SSE 逐 token 推送，客户端逐 token 渲染。**整个链路只有 1 次 HTTP 请求**（per 轮），服务端在同一个连接上持续推送。

**③ LLM 可以不说话直接行动**

第 1 轮没有任何 `TextDelta`——LLM 判断不需要跟用户解释，直接调用 `read_file`。`TurnComplete` 的 content 里只有 `ToolUseBlock`，没有 `TextBlock`。

**④ 工具执行在两轮 LLM 请求之间**

```
LLM 请求 1 → 返回 ToolUseBlock
                ↓
          ToolStarted → ToolCompleted（本地执行，不调 API）
                ↓
LLM 请求 2 → 看到工具结果，继续推理
```

工具执行是**纯本地操作**（读文件、搜索等），不消耗 LLM token。

**⑤ input_tokens 在增长**

第 1 轮 `input_tokens=6542`，第 2 轮 `input_tokens=6603`——因为第 2 轮的输入包含了第 1 轮的对话历史 + 工具结果。这就是为什么项目需要 auto-compact 机制来控制上下文窗口。

#### 完整 events.log 统计分析

将 stderr 重定向到文件可以获得干净的完整日志：

```bash
uv run oh -p "Read main.py and summarize it" 2>events.log
```

以下是对 552 行完整日志的统计：

| 指标 | 数值 |
|------|------|
| Agent 循环轮数 | **8 轮**（8 次 `TurnComplete`） |
| LLM 请求次数 | **8 次**（每轮 1 次 HTTP 请求） |
| 工具调用次数 | **9 次**（第 3 轮并发调了 2 个 glob） |
| TextDelta 事件 | **~435 个**（≈ 435 个 output token） |
| input_tokens 增长 | 6542 → 6598 → 6868 → 8616 → 8763 → 11103 → 13826 → 15874 → **17067** |
| 总 output_tokens | 32 + 55 + 108 + 90 + 64 + 44 + 34 + 36 + 456 = **~919** |

##### Agent 完整决策过程

LLM 像一个真人开发者一样，逐步探索项目结构：

```
轮次  LLM 决策                              工具调用                        结果
────  ──────                              ────────                      ────
 1    直接调工具（无文字输出）               read_file("main.py")           ❌ 文件不存在
 2    "文件不存在，搜一下"                   glob("**/main.py")            找到 13 个（全在 .venv 里）
 3    "都是第三方包，看看项目结构"           glob("*.py") + glob("src/**/*.py")  并发执行 2 个工具！
 4    "找到 __main__.py，读一下"            read_file("__main__.py")       读到 6 行入口代码
 5    "只是个 shim，读 cli.py"              read_file("cli.py")            读到前 200 行
 6    （静默续读，不输出文字）                read_file("cli.py", offset=200)   读到 200-400 行
 7    （静默续读）                           read_file("cli.py", offset=400)   读到 400-600 行
 8    （静默续读）                           read_file("cli.py", offset=600)   读到 600-681 行
 最终  输出完整的 Markdown 总结（456 token）  无工具调用 → Agent 循环结束
```

##### 从日志中发现的两个有趣细节

**并发工具执行（第 3 轮，日志第 58-61 行）**：

```
[EVENT] ToolExecutionStarted:  tool_name='glob', tool_input={'pattern': '*.py'}
[EVENT] ToolExecutionStarted:  tool_name='glob', tool_input={'pattern': 'src/**/*.py'}
[EVENT] ToolExecutionCompleted: output='(no matches)'
[EVENT] ToolExecutionCompleted: output='src/__init__.py\nsrc/openharness/...'
```

LLM 在 `TurnComplete` 的 content 中返回了 **2 个 ToolUseBlock**，引擎用 `asyncio.gather` 并发执行。两个 `Started` 紧挨着，两个 `Completed` 也紧挨着——说明是同时发起的，不是顺序执行。

对应 `engine/query.py` 的代码：
```python
if len(tool_calls) == 1:
    result = await _execute_tool_call(...)        # 单工具：顺序
else:
    results = await asyncio.gather(...)           # 多工具：并发  ← 第 3 轮走的这条路
```

**input_tokens 从 6542 涨到 17067（2.6 倍）**：

```
轮次 1: input=6542   ← 初始（system prompt + 用户消息 + 42 个工具定义）
轮次 2: input=6598   ← +56（第 1 轮的工具调用 + 结果）
轮次 3: input=6868   ← +270（第 2 轮的文字 + glob 结果）
轮次 4: input=8616   ← +1748（第 3 轮的文字 + 两个 glob 结果，src/ 下 160 个文件名）
轮次 5: input=8763   ← +147（__main__.py 的 6 行内容）
轮次 6: input=11103  ← +2340（cli.py 前 200 行）
轮次 7: input=13826  ← +2723（cli.py 200-400 行）
轮次 8: input=15874  ← +2048（cli.py 400-600 行）
最终:   input=17067  ← +1193（cli.py 600-681 行）
```

每轮的 input 包含**完整的对话历史**，所以越往后越大。读一个 681 行的文件就把 input 从 6K 推到了 17K。如果是更大的代码库，很快会逼近模型的上下文窗口限制（200K），此时 `auto_compact_if_needed()` 就会触发压缩。

---

## 九、关联阅读

| 方向 | 文件 | 说明 |
|------|------|------|
| ↑ 上游调用方 | `cli.py` main() | [MAIN_ENTRY_DEEP_DIVE.md](MAIN_ENTRY_DEEP_DIVE.md) |
| → 交互模式分支 | `ui/react_launcher.py` | 前端启动器（117 行） |
| → 交互模式分支 | `ui/backend_host.py` | JSON Lines 后端主机（317 行） |
| ↓ 下游核心 | `ui/runtime.py` | `build_runtime()` + `handle_line()` — [STARTUP_FLOW.md](STARTUP_FLOW.md) 第五、六层 |
| ↓ 事件定义 | `engine/stream_events.py` | 4 种 StreamEvent 定义（50 行） |

### 建议的下一步阅读

读完本文档后，推荐按以下顺序继续：

1. **`ui/runtime.py`** 第 94-206 行 — `build_runtime()` 如何装配 12 个子系统
2. **`ui/runtime.py`** 第 317-407 行 — `handle_line()` 如何路由到命令系统或 Agent 循环
3. **`engine/query.py`** — Agent 循环的核心实现（244 行）
