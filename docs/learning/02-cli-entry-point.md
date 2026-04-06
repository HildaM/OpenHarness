# `main()` 入口函数深度剖析

> 本文档聚焦 `src/openharness/cli.py` 第 416-681 行的 `main()` 函数，
> 逐行拆解参数解析、分支路由、异步启动的完整逻辑。

---

## 一、`main()` 在整个启动链中的位置

```
用户执行 oh --model gpt-4o -p "Hello"
    ↓
pyproject.toml    →  oh = "openharness.cli:app"
    ↓
Typer 框架        →  解析 CLI 参数，匹配到 @app.callback
    ↓
★ main()          →  本文档分析的核心
    ↓
ui/app.py         →  run_repl() 或 run_print_mode()
    ↓
ui/runtime.py     →  build_runtime() 装配所有子系统
    ↓
engine/query.py   →  Agent 循环
```

---

## 二、函数签名分析

### 装饰器

```python
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, ...):
```

`@app.callback` 是 Typer 的特殊装饰器：
- 普通子命令用 `@app.command()`
- `@app.callback()` 定义的函数在**任何子命令之前**执行
- `invoke_without_command=True` 意味着**即使没有子命令也会执行**（如直接运行 `oh`）

### 子命令守卫（第 579 行）

```python
if ctx.invoked_subcommand is not None:
    return
```

这是第一道关卡。当用户执行 `oh mcp list` 时：
- Typer 先执行 `main()`（因为 `@app.callback`）
- `ctx.invoked_subcommand` 值为 `"mcp"`
- `main()` 立刻 `return`，把控制权交给 `mcp_list()` 子命令

**只有直接运行 `oh` / `oh -p ...` / `oh -c` 等不带子命令时**，才会继续执行后续逻辑。

---

## 三、CLI 参数全解

`main()` 接收 20 个参数，分为 6 组。Typer 会自动从命令行解析它们。

### 第 1 组：Session（会话管理）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `continue_session` | `-c` / `--continue` | `bool` | `False` | 继续当前目录最近的会话 |
| `resume` | `-r` / `--resume` | `str \| None` | `None` | 按 ID 恢复会话，或打开选择器 |
| `name` | `-n` / `--name` | `str \| None` | `None` | 为会话设置显示名称 |

**设计细节**：
- `--continue` 直接加载 `latest.json`，零交互
- `--resume`（不带值）弹出会话选择器，用户选择后加载
- `--resume abc123`（带值）精确匹配 session ID

### 第 2 组：Model & Effort（模型配置）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `model` | `-m` / `--model` | `str \| None` | `None` | 模型别名或完整 ID |
| `effort` | `--effort` | `str \| None` | `None` | 推理深度：low/medium/high/max |
| `verbose` | `--verbose` | `bool` | `False` | 覆盖配置文件的 verbose 设置 |
| `max_turns` | `--max-turns` | `int \| None` | `None` | 单次提问的最大工具调用轮数 |

**设计细节**：
- `model` 为 `None` 时使用配置文件默认值 `claude-sonnet-4-20250514`
- `max_turns` 默认 200，在 `-p` 模式下常设为较小值避免死循环

### 第 3 组：Output（输出控制）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `print_mode` | `-p` / `--print` | `str \| None` | `None` | 非交互模式，值为 prompt 文本 |
| `output_format` | `--output-format` | `str \| None` | `None` | 输出格式：text/json/stream-json |

**`-p` 的参数设计**：Typer 将 `-p` 的值同时作为"是否启用"的开关和 prompt 内容。
- `oh -p "Hello"` → `print_mode = "Hello"`
- `oh`（无 -p） → `print_mode = None` → 走交互模式

### 第 4 组：Permissions（权限控制）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `permission_mode` | `--permission-mode` | `str \| None` | `None` | default/plan/full_auto |
| `dangerously_skip_permissions` | `--dangerously-skip-permissions` | `bool` | `False` | 跳过所有权限检查 |
| `allowed_tools` | `--allowed-tools` | `list[str] \| None` | `None` | 工具白名单 |
| `disallowed_tools` | `--disallowed-tools` | `list[str] \| None` | `None` | 工具黑名单 |

**设计细节**（第 584 行）：
```python
if dangerously_skip_permissions:
    permission_mode = "full_auto"
```
`--dangerously-skip-permissions` 本质上就是 `--permission-mode full_auto` 的语法糖，命名刻意包含 "dangerously" 提醒用户风险。

### 第 5 组：System & Context（上下文配置）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `system_prompt` | `-s` / `--system-prompt` | `str \| None` | `None` | 完全替换默认 System Prompt |
| `append_system_prompt` | `--append-system-prompt` | `str \| None` | `None` | 在默认 Prompt 末尾追加 |
| `settings_file` | `--settings` | `str \| None` | `None` | 自定义配置文件路径 |
| `base_url` | `--base-url` | `str \| None` | `None` | API 基础 URL |
| `api_key` | `-k` / `--api-key` | `str \| None` | `None` | API Key（最高优先级） |
| `bare` | `--bare` | `bool` | `False` | 精简模式：跳过钩子、插件、MCP |
| `api_format` | `--api-format` | `str \| None` | `None` | anthropic/openai/copilot |

### 第 6 组：Advanced（高级选项）

| 参数 | CLI 形式 | 类型 | 默认值 | 作用 |
|------|----------|------|--------|------|
| `debug` | `-d` / `--debug` | `bool` | `False` | 开启 debug 日志 |
| `mcp_config` | `--mcp-config` | `list[str] \| None` | `None` | 临时加载 MCP 服务器配置 |
| `cwd` | `--cwd` | `str` | `Path.cwd()` | 工作目录（隐藏参数） |
| `backend_only` | `--backend-only` | `bool` | `False` | 仅启动后端（隐藏参数） |

**隐藏参数**（`hidden=True`）：
- `cwd` 和 `backend_only` 不会出现在 `oh --help` 中
- `backend_only` 是 React 前端内部使用的，用户不应直接调用
- `cwd` 由 React 前端传递，确保后端进程的工作目录正确

---

## 四、三条执行路径

`main()` 在参数解析后，根据条件走 **三条互斥路径**：

```
main()
  │
  ├─ ctx.invoked_subcommand?  ──→  return（交给子命令处理）
  │
  ├─ --continue / --resume?   ──→  路径 A：恢复会话
  │
  ├─ --print?                 ──→  路径 B：非交互模式
  │
  └─ （默认）                  ──→  路径 C：交互模式
```

---

### 路径 A：恢复会话（第 590-644 行）

**触发条件**：`oh -c` 或 `oh -r` 或 `oh --resume <id>`

```
用户执行 oh -c
    ↓
load_session_snapshot(cwd)
    ↓  读取 ~/.openharness/data/sessions/<project>-<hash>/latest.json
    ↓
session_data = {
    "session_id": "a1b2c3d4e5f6",
    "model": "claude-sonnet-4-20250514",
    "system_prompt": "You are OpenHarness...",
    "messages": [ ... 历史对话消息 ... ],
    "usage": {"input_tokens": 5000, "output_tokens": 2000},
    "summary": "Fix the bug in main.py"
}
    ↓
asyncio.run(run_repl(
    restore_messages=session_data["messages"],
    model=session_data["model"],
    ...
))
```

#### 恢复会话的三种方式

**方式 1：`oh -c`（继续最近会话）**

```python
if continue_session:
    session_data = load_session_snapshot(cwd)  # 读 latest.json
```

直接读取 `latest.json`，无需交互。

**方式 2：`oh -r`（会话选择器）**

```python
elif resume == "" or resume is None:
    sessions = list_session_snapshots(cwd, limit=10)  # 列出最近 10 个
    # 打印列表让用户选择
    for i, s in enumerate(sessions, 1):
        print(f"  {i}. [{s['session_id']}] {s.get('summary', '?')[:50]}")
    choice = typer.prompt("Enter session number or ID")
    # 支持按序号或 ID 选择
```

**方式 3：`oh -r abc123`（精确恢复）**

```python
else:
    session_data = load_session_by_id(cwd, resume)  # 按 ID 查找
```

#### 会话存储位置

```
~/.openharness/data/sessions/
└── OpenHarness-a1b2c3d4e5f6/    # 项目名 + 路径哈希前 12 位
    ├── latest.json               # 最近一次会话快照
    ├── session-abc123.json       # 按 session_id 保存的快照
    └── session-def456.json
```

每个快照是完整的 JSON，包含所有对话消息，可以完全恢复上下文。

---

### 路径 B：非交互模式（第 647-667 行）

**触发条件**：`oh -p "your prompt"`

```python
if print_mode is not None:
    prompt = print_mode.strip()
    if not prompt:
        print("Error: -p/--print requires a prompt value", file=sys.stderr)
        raise typer.Exit(1)

    asyncio.run(
        run_print_mode(
            prompt=prompt,
            output_format=output_format or "text",
            cwd=cwd,
            model=model,
            base_url=base_url,
            system_prompt=system_prompt,
            append_system_prompt=append_system_prompt,
            api_key=api_key,
            api_format=api_format,
            permission_mode=permission_mode,
            max_turns=max_turns,
        )
    )
    return
```

#### 执行流程

```
oh -p "Explain this codebase" --output-format json
    ↓
run_print_mode()
    ↓
build_runtime()          → 装配所有子系统（无权限弹窗，自动放行）
    ↓
start_runtime()          → 触发 SESSION_START 钩子
    ↓
handle_line(prompt)      → 提交给 Agent 循环
    ↓                       ↓
    │                 stream events
    │                       ↓
    │              ┌─ text 格式: 直接打印到 stdout
    │              ├─ json 格式: 收集所有文本，最后输出 {"type":"result","text":"..."}
    │              └─ stream-json: 每个事件单独输出一行 JSON
    ↓
close_runtime()          → 关闭 MCP 连接，触发 SESSION_END 钩子
```

#### 三种输出格式对比

**`--output-format text`（默认）**：
```
$ oh -p "Hello"
Hello! How can I help you today?
```

**`--output-format json`**：
```json
$ oh -p "Hello" --output-format json
{"type": "result", "text": "Hello! How can I help you today?"}
```

**`--output-format stream-json`**：
```json
$ oh -p "Hello" --output-format stream-json
{"type": "assistant_delta", "text": "Hello"}
{"type": "assistant_delta", "text": "! How can"}
{"type": "assistant_delta", "text": " I help you today?"}
{"type": "assistant_complete", "text": "Hello! How can I help you today?"}
```

#### 非交互模式的权限处理

```python
# ui/app.py:81-85
async def _noop_permission(tool_name: str, reason: str) -> bool:
    return True  # 自动放行所有工具

async def _noop_ask(question: str) -> str:
    return ""    # 自动跳过所有提问
```

非交互模式下无法弹窗确认，所以**所有权限请求自动放行**。这就是为什么项目提供了 `--permission-mode` 参数——让用户在非交互模式下显式控制安全级别。

---

### 路径 C：交互模式（第 669-681 行）

**触发条件**：直接运行 `oh`（不带 `-p`、`-c`、`-r`）

```python
asyncio.run(
    run_repl(
        prompt=None,           # 无初始 prompt
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        backend_only=backend_only,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
    )
)
```

这是最常用的路径。`run_repl()` 内部根据 `backend_only` 再次分流：

```
run_repl()
    │
    ├─ backend_only=True?   ──→  run_backend_host()
    │                             （被 React 前端 spawn 时走这条路）
    │
    └─ backend_only=False   ──→  launch_react_tui()
                                  （用户直接运行 oh 走这条路）
```

#### 交互模式的双进程启动序列

```
用户执行 oh
    ↓
main() → asyncio.run(run_repl(backend_only=False))
    ↓
run_repl() → launch_react_tui()
    ↓
检查 frontend/terminal/node_modules/ 是否存在
    ├─ 不存在 → npm install（自动安装前端依赖）
    └─ 存在   → 跳过
    ↓
构建环境变量 OPENHARNESS_FRONTEND_CONFIG = {
    "backend_command": ["python", "-m", "openharness", "--backend-only", "--cwd", "/path"],
    "initial_prompt": null
}
    ↓
启动 Node.js 子进程: npm exec -- tsx src/index.tsx
    ↓
React 前端启动，读取 OPENHARNESS_FRONTEND_CONFIG
    ↓
前端 spawn Python 后端: python -m openharness --backend-only --cwd /path
    ↓
后端 main() 再次被调用，此时 backend_only=True
    ↓
run_repl(backend_only=True) → run_backend_host()
    ↓
ReactBackendHost.run()
    ↓
build_runtime() → start_runtime() → 事件循环（等待前端消息）
```

**关键点**：`main()` 实际上会被调用**两次**！
1. 第一次：用户执行 `oh` → `backend_only=False` → 启动前端
2. 第二次：前端 spawn 后端 → `backend_only=True` → 启动 Agent 引擎

---

## 五、`asyncio.run()` 的作用

三条路径都通过 `asyncio.run()` 启动异步函数：

```python
asyncio.run(run_repl(...))
asyncio.run(run_print_mode(...))
```

为什么需要 `asyncio`？
- LLM API 调用是网络 I/O → 需要异步
- MCP 服务器连接是网络 I/O → 需要异步
- 多工具并发执行 → 需要 `asyncio.gather`
- 前后端 stdin/stdout 通信 → 需要异步读写

`asyncio.run()` 创建一个新的事件循环，执行传入的协程直到完成，然后关闭循环。整个程序的生命周期就在这一次 `asyncio.run()` 中。

---

## 六、参数传递链路

以 `oh --model gpt-4o --api-format openai -p "Hello"` 为例，追踪 `model` 参数的传递：

```
CLI 命令行
    ↓  Typer 解析
main(model="gpt-4o", api_format="openai", print_mode="Hello")
    ↓
run_print_mode(model="gpt-4o", api_format="openai", prompt="Hello")
    ↓
build_runtime(model="gpt-4o", api_format="openai", prompt="Hello")
    ↓
settings = load_settings()                    # 从 settings.json 读默认值
settings = settings.merge_cli_overrides(      # CLI 参数覆盖
    model="gpt-4o",                           # 覆盖默认 "claude-sonnet-4"
    api_format="openai",                      # 覆盖默认 "anthropic"
)
    ↓
resolved_api_client = OpenAICompatibleClient( # 因为 api_format="openai"
    api_key=settings.resolve_api_key(),
    base_url=settings.base_url,
)
    ↓
engine = QueryEngine(
    model="gpt-4o",                           # 最终传递到引擎
    api_client=resolved_api_client,           # OpenAI 客户端
    ...
)
    ↓
api_client.stream_message(ApiMessageRequest(
    model="gpt-4o",                           # 发送给 API
    ...
))
```

**配置优先级实现**：

```
CLI --model gpt-4o          ← 最高优先级（merge_cli_overrides）
     ↓ 覆盖
ENV OPENHARNESS_MODEL       ← 环境变量（_apply_env_overrides）
     ↓ 覆盖
settings.json "model"       ← 配置文件
     ↓ 覆盖
代码默认值 "claude-sonnet-4" ← 最低优先级（Settings 模型默认值）
```

---

## 七、延迟导入策略

注意 `main()` 中的导入位置：

```python
def main(ctx, ...):
    if ctx.invoked_subcommand is not None:
        return                            # 子命令时直接返回，不导入任何东西

    import asyncio                        # ← 仅在需要时导入

    from openharness.ui.app import run_print_mode, run_repl  # ← 延迟导入

    if continue_session or resume is not None:
        from openharness.services.session_storage import (    # ← 按需导入
            list_session_snapshots,
            load_session_by_id,
            load_session_snapshot,
        )
```

**为什么这样设计？**

1. `oh mcp list` 只需要 MCP 相关模块，不需要加载 UI、引擎、LLM SDK
2. `oh -p "Hello"` 不需要加载会话存储模块
3. `asyncio` 在子命令中不需要

延迟导入将 `oh mcp list` 的启动时间从几百毫秒降到几十毫秒。

---

## 八、错误处理模式

`main()` 使用 `typer.Exit(1)` 而非 `sys.exit(1)` 来终止程序：

```python
if session_data is None:
    print("No previous session found.", file=sys.stderr)
    raise typer.Exit(1)
```

**`typer.Exit` vs `sys.exit`**：
- `typer.Exit` 是 Typer 框架感知的异常，会被 Typer 捕获并转换为正确的退出码
- `sys.exit` 会触发 `SystemExit`，可能跳过 Typer 的清理逻辑
- 错误信息输出到 `sys.stderr`，确保与正常输出（`stdout`）分离

---

## 九、完整判断流程图

```
oh [args]
    │
    ▼
┌──────────────────────────────────────┐
│  Typer 解析 CLI 参数                   │
│  填充 main() 的 20 个参数              │
└──────────────┬───────────────────────┘
               │
               ▼
┌─── ctx.invoked_subcommand? ──────────┐
│  是（oh mcp / oh plugin / ...）       │──→  return（交给子命令）
└──────────────┬───────────────────────┘
               │ 否
               ▼
┌─── dangerously_skip_permissions? ────┐
│  是                                   │──→  permission_mode = "full_auto"
└──────────────┬───────────────────────┘
               │
               ▼
     延迟导入 run_repl, run_print_mode
               │
               ▼
┌─── continue_session or resume? ──────┐
│  是                                   │
│  ┌─ -c ?          → load latest.json │
│  ├─ -r (无值)?    → 显示选择器       │
│  └─ -r <id>?     → 按 ID 加载       │
│                                       │
│  asyncio.run(run_repl(               │
│      restore_messages=...,           │
│  ))                                   │──→  return
└──────────────┬───────────────────────┘
               │ 否
               ▼
┌─── print_mode is not None? ──────────┐
│  是 → oh -p "prompt"                  │
│                                       │
│  asyncio.run(run_print_mode(         │
│      prompt=print_mode,              │
│      output_format=...,             │
│  ))                                   │──→  return
└──────────────┬───────────────────────┘
               │ 否
               ▼
┌──────────────────────────────────────┐
│  默认：交互模式                        │
│                                       │
│  asyncio.run(run_repl(               │
│      prompt=None,                    │
│      backend_only=backend_only,      │
│  ))                                   │
└──────────────────────────────────────┘
```

---

## 十、动手实验

### 实验 1：追踪参数传递

在 `main()` 第 582 行后加一行 print：

```python
import asyncio
print(f"[TRACE] model={model}, api_format={api_format}, print_mode={print_mode}")
```

然后运行：
```bash
uv run oh -p "Hello" --model gpt-4o --api-format openai 2>&1 | head -5
```

### 实验 2：观察双进程启动

```bash
# 终端 1：启动 oh，观察进程树
uv run oh &
ps aux | grep openharness
# 你会看到两个 python 进程：一个是父进程（已退出），一个是 --backend-only
```

### 实验 3：手动模拟后端启动

```bash
# 跳过前端，直接启动后端（会等待 stdin 输入）
echo '{"type":"submit_line","line":"Hello"}' | uv run oh --backend-only 2>/dev/null
```

### 实验 4：对比三种输出格式

```bash
uv run oh -p "Say hi" 2>/dev/null
uv run oh -p "Say hi" --output-format json 2>/dev/null
uv run oh -p "Say hi" --output-format stream-json 2>/dev/null
```

---

## 十一、关联阅读

| 想深入了解... | 阅读 |
|--------------|------|
| `run_repl()` / `run_print_mode()` 内部 | `src/openharness/ui/app.py`（160 行） |
| `build_runtime()` 装配过程 | `src/openharness/ui/runtime.py` 第 94-206 行 |
| 前后端通信协议 | `src/openharness/ui/backend_host.py`（317 行） |
| Agent 循环核心 | `src/openharness/engine/query.py`（244 行） |
| 会话存储实现 | `src/openharness/services/session_storage.py`（178 行） |
| 配置加载与合并 | `src/openharness/config/settings.py`（184 行） |
| 完整启动流程总览 | [01-startup-overview.md](01-startup-overview.md) |
