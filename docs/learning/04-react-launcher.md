# `ui/react_launcher.py` — 前后端双进程架构深度剖析

> **前置阅读**：[03-app-ui-routing.md](03-app-ui-routing.md) 中的 `run_repl()` 两条分支
>
> **源文件**：`src/openharness/ui/react_launcher.py`（116 行）
>
> **关联前端**：`frontend/terminal/`（React/Ink TUI）

---

## 一、这个文件的角色

`react_launcher.py` 负责 **启动 Node.js 前端进程**，是交互模式的入口。它本身不包含任何 Agent 逻辑，但它建立了整个双进程通信架构的基础。

```
cli.py → app.py → ★ react_launcher.py → Node.js 前端 → spawn Python 后端
                       你正在这里              ↕ stdin/stdout
                                           backend_host.py
```

**只有 3 个函数 + 2 个辅助函数**，结构极简：

| 函数 | 行数 | 职责 |
|------|------|------|
| `_resolve_npm()` | 13-15 | 找到系统中的 npm 可执行文件 |
| `_repo_root()` | 18-19 | 定位项目根目录 |
| `get_frontend_dir()` | 22-24 | 返回前端目录路径 |
| `build_backend_command()` | 27-53 | 构建后端启动命令 |
| `launch_react_tui()` | 56-113 | **核心：启动前端进程** |

---

## 二、辅助函数

### `_resolve_npm()`（第 13-15 行）

```python
def _resolve_npm() -> str:
    return shutil.which("npm") or "npm"
```

`shutil.which("npm")` 在 `$PATH` 中搜索 `npm` 可执行文件，返回绝对路径如 `/usr/local/bin/npm`。如果找不到就退回 `"npm"`（让操作系统自己尝试解析）。

**为什么不直接用 `"npm"`？** 因为在某些环境下（如 nvm、虚拟环境），`npm` 可能不在默认 PATH 中，`shutil.which` 能更可靠地找到它。

### `_repo_root()`（第 18-19 行）

```python
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
```

从当前文件路径向上回溯 3 级找到项目根目录：

```
__file__     = src/openharness/ui/react_launcher.py
parents[0]   = src/openharness/ui/
parents[1]   = src/openharness/
parents[2]   = src/
parents[3]   = OpenHarness/          ← 项目根目录
```

### `get_frontend_dir()`（第 22-24 行）

```python
def get_frontend_dir() -> Path:
    return _repo_root() / "frontend" / "terminal"
```

拼接出 `OpenHarness/frontend/terminal/`——React 前端的根目录。

---

## 三、`build_backend_command()` 逐行解读

这个函数负责**序列化 Python 运行参数为命令行字符串**，供前端进程 spawn 后端时使用。

```python
def build_backend_command(
    *, cwd, model, max_turns, base_url, system_prompt, api_key, api_format,
) -> list[str]:
    # 基础命令：用当前 Python 解释器运行 openharness 模块，加 --backend-only 标志
    command = [sys.executable, "-m", "openharness", "--backend-only"]
    #          ↑                ↑                   ↑
    #    如 /path/.venv/bin/python                 关键标志：告诉 main() 走后端路径
    #                    运行 openharness 包

    # 只追加非 None 的参数
    if cwd:            command.extend(["--cwd", cwd])
    if model:          command.extend(["--model", model])
    if max_turns is not None:  command.extend(["--max-turns", str(max_turns)])
    if base_url:       command.extend(["--base-url", base_url])
    if system_prompt:  command.extend(["--system-prompt", system_prompt])
    if api_key:        command.extend(["--api-key", api_key])
    if api_format:     command.extend(["--api-format", api_format])
    return command
```

**生成的命令示例**：

```python
# 用户执行: oh --model gpt-4o --api-format openai
build_backend_command(cwd="/home/user/project", model="gpt-4o", api_format="openai")
# 返回:
["/path/.venv/bin/python", "-m", "openharness", "--backend-only",
 "--cwd", "/home/user/project",
 "--model", "gpt-4o",
 "--api-format", "openai"]
```

**关键设计**：

1. **`sys.executable`** — 使用 **当前 Python 解释器** 的完整路径，确保后端进程使用同一个虚拟环境。如果写死 `"python"` 可能找到系统 Python 而非 `.venv` 里的。

2. **`--backend-only`** — 这个标志让 `cli.py` 的 `main()` 走 `run_repl(backend_only=True)` 路径，最终进入 `ReactBackendHost`。

3. **只传可序列化的参数** — `api_client`（Python 对象）和 `restore_messages`（内存数据）无法通过命令行传递，所以 `build_backend_command` 不包含它们。

---

## 四、`launch_react_tui()` 逐行解读

这是整个文件的核心——启动 React 前端进程。

### 阶段 1：前置检查（第 68-73 行）

```python
async def launch_react_tui(...) -> int:
    frontend_dir = get_frontend_dir()                    # frontend/terminal/
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"React terminal frontend is missing: {package_json}")

    npm = _resolve_npm()                                 # /usr/local/bin/npm
```

如果 `package.json` 不存在，说明前端代码缺失（可能是不完整的 clone），直接抛异常。

### 阶段 2：自动安装依赖（第 75-84 行）

```python
    if not (frontend_dir / "node_modules").exists():
        install = await asyncio.create_subprocess_exec(
            npm, "install",
            "--no-fund",    # 不显示 npm 赞助信息
            "--no-audit",   # 不检查安全漏洞（加速安装）
            cwd=str(frontend_dir),
        )
        if await install.wait() != 0:
            raise RuntimeError("Failed to install React terminal frontend dependencies")
```

**只在首次运行时触发**——检查 `node_modules/` 是否存在。不存在就执行 `npm install`。

**为什么用 `asyncio.create_subprocess_exec` 而非 `subprocess.run`？**
- `launch_react_tui` 本身是 `async` 函数，运行在 asyncio 事件循环中
- `subprocess.run` 会**阻塞整个事件循环**
- `asyncio.create_subprocess_exec` + `await install.wait()` 是异步等待，不阻塞

### 阶段 3：构建前端配置（第 86-100 行）

```python
    env = os.environ.copy()                               # 复制当前环境变量
    env["OPENHARNESS_FRONTEND_CONFIG"] = json.dumps({
        "backend_command": build_backend_command(          # 后端启动命令
            cwd=cwd or str(Path.cwd()),
            model=model,
            max_turns=max_turns,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
        ),
        "initial_prompt": prompt,                         # 初始 prompt（通常为 None）
    })
```

通过**环境变量**将配置传递给 Node.js 进程。JSON 结构：

```json
{
  "backend_command": ["/path/.venv/bin/python", "-m", "openharness", "--backend-only", "--cwd", "/path"],
  "initial_prompt": null
}
```

**为什么用环境变量而非命令行参数？**
- 命令行参数需要转义处理（引号、空格）
- 环境变量可以传递任意 JSON，不受 shell 解析影响
- Node.js 通过 `process.env.OPENHARNESS_FRONTEND_CONFIG` 直接读取

### 阶段 4：启动前端进程（第 101-113 行）

```python
    process = await asyncio.create_subprocess_exec(
        npm, "exec", "--", "tsx", "src/index.tsx",        # 实际执行的命令
        cwd=str(frontend_dir),                            # 工作目录
        env=env,                                          # 含 OPENHARNESS_FRONTEND_CONFIG
        stdin=None,                                       # 继承父进程的 stdin
        stdout=None,                                      # 继承父进程的 stdout
        stderr=None,                                      # 继承父进程的 stderr
    )
    return await process.wait()                           # 等待前端进程退出
```

**命令解析**：`npm exec -- tsx src/index.tsx`
- `npm exec` — 运行 node_modules/.bin/ 中的命令
- `--` — 分隔 npm 参数和被执行命令的参数
- `tsx` — TypeScript 执行器（类似 ts-node，但更快）
- `src/index.tsx` — React 前端入口文件

**`stdin=None` / `stdout=None` / `stderr=None` 的含义**：
- `None` = **继承父进程的标准流**
- 前端进程直接使用终端的 stdin/stdout/stderr
- 所以用户在终端看到的 UI 就是前端进程渲染的

**`await process.wait()`**：
- Python 主进程在此处**阻塞等待**，直到前端进程退出
- 返回前端的退出码（0=正常，非0=异常）
- 前端退出后，`app.py` 的 `run_repl()` 检查退出码并结束

---

## 五、前端如何 spawn 后端

`react_launcher.py` 启动了前端，但后端是**前端启动的**。来看这个链条：

### 前端入口：`frontend/terminal/src/index.tsx`（9 行）

```tsx
const config = JSON.parse(
    process.env.OPENHARNESS_FRONTEND_CONFIG ?? '{}'
) as FrontendConfig;

render(<App config={config} />);
```

从环境变量读取配置，传给 `<App>` 组件。

### 前端 spawn 后端：`frontend/terminal/src/hooks/useBackendSession.ts`（第 40-44 行）

```typescript
const [command, ...args] = config.backend_command;
// command = "/path/.venv/bin/python"
// args = ["-m", "openharness", "--backend-only", "--cwd", "..."]

const child = spawn(command, args, {
    stdio: ['pipe', 'pipe', 'inherit'],
    //       ↑       ↑        ↑
    //     stdin    stdout   stderr
    //     管道      管道    继承终端
});
```

**`stdio` 配置详解**：

| 流 | 值 | 含义 |
|----|-----|------|
| stdin | `'pipe'` | 创建管道，前端可以**写入**给后端 |
| stdout | `'pipe'` | 创建管道，前端可以**读取**后端输出 |
| stderr | `'inherit'` | 后端 stderr 直接输出到终端（debug 日志等） |

这就建立了**双向通信通道**：

```
前端 (Node.js)                          后端 (Python)
     │                                       │
     │── child.stdin.write(JSON) ──────────→ │  sys.stdin 读取
     │                                       │
     │←── child.stdout (readline) ─────────  │  sys.stdout.write("OHJSON:..." )
     │                                       │
                                    stderr → 终端（直接显示）
```

---

## 六、通信协议详解

### 前端 → 后端（`FrontendRequest`）

前端通过 `child.stdin.write(JSON + "\n")` 发送请求：

```typescript
// useBackendSession.ts:36
child.stdin.write(JSON.stringify(payload) + '\n');
```

请求类型定义在 `ui/protocol.py:15-22`：

| type | 用途 | 额外字段 |
|------|------|---------|
| `submit_line` | 用户提交一行输入 | `line: "Fix the bug"` |
| `permission_response` | 用户回应权限弹窗 | `request_id`, `allowed: true/false` |
| `question_response` | 用户回答 Agent 提问 | `request_id`, `answer: "yes"` |
| `list_sessions` | 请求会话列表（/resume） | 无 |
| `shutdown` | 关闭后端 | 无 |

### 后端 → 前端（`BackendEvent`）

后端通过 `sys.stdout.write("OHJSON:" + JSON + "\n")` 发送事件。`OHJSON:` 前缀用于区分协议消息和普通输出。

```python
# backend_host.py:282
sys.stdout.write(_PROTOCOL_PREFIX + event.model_dump_json() + "\n")
```

前端读取并解析：

```typescript
// useBackendSession.ts:48-54
reader.on('line', (line) => {
    if (!line.startsWith(PROTOCOL_PREFIX)) {
        // 非协议消息 → 当作日志显示
        setTranscript((items) => [...items, {role: 'log', text: line}]);
        return;
    }
    // 协议消息 → 解析 JSON 并处理
    const event = JSON.parse(line.slice(PROTOCOL_PREFIX.length));
    handleEvent(event);
});
```

事件类型（14 种）：

| type | 用途 | 关键字段 |
|------|------|---------|
| `ready` | 后端初始化完成 | `state`, `tasks`, `commands` |
| `state_snapshot` | 状态更新 | `state`, `mcp_servers` |
| `tasks_snapshot` | 任务列表更新 | `tasks` |
| `transcript_item` | 对话条目 | `item: {role, text}` |
| `assistant_delta` | 流式文本增量 | `message: "Hello"` |
| `assistant_complete` | 模型回合完成 | `message: "Full text"` |
| `tool_started` | 工具开始执行 | `tool_name`, `tool_input` |
| `tool_completed` | 工具执行完成 | `tool_name`, `output`, `is_error` |
| `line_complete` | 一行输入处理完毕 | 无 |
| `modal_request` | 弹出权限/问答对话框 | `modal: {kind, request_id, ...}` |
| `select_request` | 弹出选择列表 | `select_options` |
| `clear_transcript` | 清空对话记录 | 无 |
| `error` | 错误信息 | `message` |
| `shutdown` | 后端关闭 | 无 |

---

## 七、完整交互时序图

以用户输入 "Hello" 为例的完整时序：

```
用户终端                React 前端 (Node.js)              Python 后端
   │                         │                                │
   │  用户执行 oh             │                                │
   │─────────────────────→  launch_react_tui()                │
   │                         │                                │
   │                    npm exec tsx index.tsx                 │
   │                         │                                │
   │                    读取 OPENHARNESS_FRONTEND_CONFIG       │
   │                         │                                │
   │                    spawn(python -m openharness --backend-only)
   │                         │────────────────────────────────→│
   │                         │                                │
   │                         │                    build_runtime()
   │                         │                    start_runtime()
   │                         │                                │
   │                         │←── OHJSON:{"type":"ready",...} ─│
   │                         │                                │
   │  渲染欢迎界面 ←─────── 显示 WelcomeBanner + StatusBar     │
   │                         │                                │
   │  用户输入 "Hello"        │                                │
   │  按下 Enter             │                                │
   │                         │                                │
   │                  stdin.write({"type":"submit_line","line":"Hello"})
   │                         │────────────────────────────────→│
   │                         │                                │
   │                         │                   handle_line("Hello")
   │                         │                   engine.submit_message()
   │                         │                   api_client.stream_message()
   │                         │                                │
   │                         │←─ OHJSON:{"type":"assistant_delta","message":"Hi"} ─│
   │  看到 "Hi" 逐字出现 ←── setAssistantBuffer("Hi")         │
   │                         │                                │
   │                         │←─ OHJSON:{"type":"assistant_delta","message":"!"} ──│
   │  看到 "!"  ←─────────── setAssistantBuffer("Hi!")         │
   │                         │                                │
   │                         │←─ OHJSON:{"type":"assistant_complete",...} ─────────│
   │                         │                                │
   │  对话记录中出现完整回复 ← setTranscript([..., {role:"assistant", text:"Hi!"}])
   │                         │                                │
   │                         │←─ OHJSON:{"type":"line_complete"} ─────────────────│
   │  输入框恢复可用 ←─────── setBusy(false)                   │
   │                         │                                │
   │  用户按 Ctrl+C          │                                │
   │                  stdin.write({"type":"shutdown"})         │
   │                         │────────────────────────────────→│
   │                         │                                │
   │                         │←─ OHJSON:{"type":"shutdown"} ──│
   │                         │                   close_runtime()
   │                    进程退出                          进程退出
   │←─────────────────── process.wait() 返回 0                │
```

---

## 八、权限确认的异步交互

当 Agent 需要执行写操作时，后端不能直接弹窗——它是一个无头进程。整个确认流程通过协议消息实现：

```
后端                                前端                              用户
 │                                   │                                 │
 │  工具需要权限确认                   │                                 │
 │  创建 Future，等待结果              │                                 │
 │                                   │                                 │
 │── OHJSON:{"type":"modal_request",  │                                 │
 │    "modal":{"kind":"permission",   │                                 │
 │     "request_id":"abc123",         │                                 │
 │     "tool_name":"file_edit",       │                                 │
 │     "reason":"写操作需要确认"}}     │                                 │
 │──────────────────────────────────→│                                 │
 │                                   │  弹出权限对话框                   │
 │                                   │──────────────────────────────→  │
 │                                   │                     用户按 y    │
 │                                   │←──────────────────────────────  │
 │                                   │                                 │
 │  {"type":"permission_response",   │                                 │
 │   "request_id":"abc123",          │                                 │
 │   "allowed":true}                 │                                 │
 │←──────────────────────────────────│                                 │
 │                                   │                                 │
 │  Future.set_result(True)           │                                 │
 │  继续执行工具                      │                                 │
```

**后端通过 `asyncio.Future` 实现异步等待**：
```python
# backend_host.py:242-259
future = asyncio.get_running_loop().create_future()
self._permission_requests[request_id] = future
await self._emit(BackendEvent(type="modal_request", ...))
return await future  # ← 在这里挂起，直到前端回复
```

---

## 九、为什么选择 stdio 而非 WebSocket？

你可能会问：为什么不用 WebSocket 或 HTTP 通信？

| 方案 | 优点 | 缺点 |
|------|------|------|
| **stdio（当前方案）** | 零配置、无端口冲突、父子进程生命周期绑定 | 只能单客户端、只能文本 |
| WebSocket | 多客户端、二进制支持 | 需要端口分配、进程生命周期独立 |
| HTTP | 通用、可调试 | 需要端口、连接管理、无法推送 |

stdio 的核心优势是 **父子进程自动绑定**：前端退出 → stdin 关闭 → 后端检测到 EOF → 自动退出。不需要额外的健康检查或超时机制。

---

## 十、动手实验

### 实验 1：查看前端收到的配置

在 `frontend/terminal/src/index.tsx` 第 7 行后加打印：

```tsx
const config = JSON.parse(process.env.OPENHARNESS_FRONTEND_CONFIG ?? '{}');
console.error('[CONFIG]', JSON.stringify(config, null, 2));  // 加这行
```

运行 `uv run oh`，stderr 中会显示完整的配置 JSON。

### 实验 2：手动模拟前后端通信

```bash
# 直接启动后端，手动输入 JSON 请求
echo '{"type":"submit_line","line":"What is 2+2?"}
{"type":"shutdown"}' | uv run oh --backend-only 2>/dev/null

# 你会看到 OHJSON: 前缀的后端事件流
```

### 实验 3：观察双进程

```bash
# 启动 oh 后，在另一个终端查看进程树
uv run oh &
pgrep -a -f "openharness" 
# 你会看到两个进程：
# 1. npm exec -- tsx src/index.tsx  (前端)
# 2. python -m openharness --backend-only  (后端)
```

---

## 十一、关联阅读

| 方向 | 文件 | 说明 |
|------|------|------|
| ↑ 调用方 | `ui/app.py` run_repl() | [03-app-ui-routing.md](03-app-ui-routing.md) |
| → 后端主机 | `ui/backend_host.py` | 接收前端请求、调用 handle_line()（317 行） |
| → 通信协议 | `ui/protocol.py` | FrontendRequest + BackendEvent 定义（198 行） |
| → 前端入口 | `frontend/terminal/src/index.tsx` | 9 行：读配置、渲染 App |
| → 前端通信 | `frontend/terminal/src/hooks/useBackendSession.ts` | spawn 后端 + 处理事件（172 行） |
| → 前端类型 | `frontend/terminal/src/types.ts` | 所有类型定义（63 行） |
| ↓ 下一站 | `ui/runtime.py` build_runtime() | 真正的核心：装配 12 个子系统 |
