# OpenHarness 后端启动流程学习指南

> 适合想要深入理解项目代码的开发者，按照代码实际执行顺序逐层剖析。

---

## 一、全局启动流程总览

当你执行 `oh` 或 `uv run oh` 时，代码会经过以下 **7 层调用栈**，最终进入 Agent 循环：

```
① pyproject.toml          oh = "openharness.cli:app"
       ↓
② cli.py                  Typer 解析 CLI 参数
       ↓
③ ui/app.py               路由到 run_repl() 或 run_print_mode()
       ↓
④ ui/react_launcher.py    启动 React TUI 前端（交互模式）
   ui/backend_host.py      或启动 Backend Host（JSON 协议后端）
       ↓
⑤ ui/runtime.py           build_runtime() — 装配所有子系统
       ↓
⑥ engine/query_engine.py  QueryEngine — 管理对话历史
       ↓
⑦ engine/query.py         run_query() — Agent 循环核心
```

下面逐层详细解读。

---

## 二、第一层：入口点（Entry Point）

### 文件：`pyproject.toml`

```toml
[project.scripts]
openharness = "openharness.cli:app"
oh = "openharness.cli:app"
```

`oh` 和 `openharness` 两个命令都指向同一个入口：`openharness.cli` 模块中的 `app` 对象。

### 文件：`src/openharness/__main__.py`（7 行）

```python
"""Entry point for `python -m openharness`."""
from openharness.cli import app

if __name__ == "__main__":
    app()
```

支持 `python -m openharness` 方式运行。

**学习要点**：
- `app` 是一个 `typer.Typer()` 实例
- Python 包通过 `[project.scripts]` 注册可执行命令

---

## 三、第二层：CLI 参数解析

### 文件：`src/openharness/cli.py`（667 行）

这是整个项目的 **命令行入口**，用 [Typer](https://typer.tiangolo.com/) 框架构建。

### 结构分解

```python
# 1. 创建主应用
app = typer.Typer(name="openharness", invoke_without_command=True)

# 2. 注册子命令组
mcp_app = typer.Typer(name="mcp")       # oh mcp list/add/remove
plugin_app = typer.Typer(name="plugin")  # oh plugin list/install/uninstall
auth_app = typer.Typer(name="auth")      # oh auth status/login/copilot-login
cron_app = typer.Typer(name="cron")      # oh cron start/stop/status

app.add_typer(mcp_app)
app.add_typer(plugin_app)
app.add_typer(auth_app)
app.add_typer(cron_app)

# 3. 主命令回调（核心）
@app.callback(invoke_without_command=True)
def main(ctx, continue_session, resume, model, print_mode, ...):
    ...
```

### `main()` 函数的三条路径

```python
def main(ctx, ...):
    # 如果是子命令（oh mcp / oh plugin / oh auth / oh cron），直接返回
    if ctx.invoked_subcommand is not None:
        return

    import asyncio

    # 路径 A：恢复会话（--continue / --resume）
    if continue_session or resume is not None:
        session_data = load_session_snapshot(cwd)  # 从磁盘加载
        asyncio.run(run_repl(..., restore_messages=session_data["messages"]))
        return

    # 路径 B：非交互模式（-p "prompt"）
    if print_mode is not None:
        asyncio.run(run_print_mode(prompt=print_mode, ...))
        return

    # 路径 C：交互模式（默认）
    asyncio.run(run_repl(prompt=None, cwd=cwd, model=model, ...))
```

**学习要点**：
- `invoke_without_command=True` 让 Typer 在没有子命令时也执行 `main()`
- 所有异步操作都通过 `asyncio.run()` 启动
- CLI 参数通过 `typer.Option()` 声明，自动生成帮助文档
- 三条路径最终都进入 `ui/app.py`

**阅读建议**：先忽略子命令（`mcp_app`、`plugin_app` 等），聚焦 `main()` 函数的三条路径。

---

## 四、第三层：UI 路由

### 文件：`src/openharness/ui/app.py`（160 行）

这个文件定义了两个核心 async 函数，是 CLI 层和运行时层之间的桥梁。

### `run_repl()` — 交互模式入口

```python
async def run_repl(*, prompt, cwd, model, backend_only, ...):
    # 分支 1：仅启动后端（被 React 前端调用时）
    if backend_only:
        await run_backend_host(cwd=cwd, model=model, ...)
        return

    # 分支 2：启动 React TUI 前端（默认）
    exit_code = await launch_react_tui(prompt=prompt, cwd=cwd, model=model, ...)
    if exit_code != 0:
        raise SystemExit(exit_code)
```

### `run_print_mode()` — 非交互模式入口

```python
async def run_print_mode(*, prompt, output_format="text", ...):
    # 1. 构建运行时
    bundle = await build_runtime(prompt=prompt, model=model, ...)
    await start_runtime(bundle)

    # 2. 处理一行输入
    await handle_line(bundle, prompt,
        print_system=_print_system,      # 系统消息 → stderr
        render_event=_render_event,      # 流事件 → stdout
        clear_output=_clear_output,      # 清屏（noop）
    )

    # 3. 清理资源
    await close_runtime(bundle)
```

**学习要点**：
- 交互模式走 `launch_react_tui()` → 启动 Node.js 前端进程
- 非交互模式直接走 `build_runtime()` → `handle_line()` → `close_runtime()`
- `handle_line()` 是所有用户输入的统一处理入口

---

## 五、第四层：前后端通信架构

OpenHarness 的交互模式采用 **双进程架构**：Python 后端 + Node.js 前端，通过 **stdin/stdout JSON 协议** 通信。

### 启动顺序

```
用户执行 oh
  ↓
Python 主进程 → launch_react_tui()
  ↓
启动 Node.js 子进程 (npm exec -- tsx src/index.tsx)
  ↓ 环境变量传递 OPENHARNESS_FRONTEND_CONFIG
Node.js 前端读取配置中的 backend_command
  ↓
Node.js 启动 Python 后端子进程 (python -m openharness --backend-only)
  ↓
Python 后端进入 ReactBackendHost.run() 事件循环
  ↓
双向通信：stdin(前端→后端) / stdout(后端→前端)
```

### 文件：`src/openharness/ui/react_launcher.py`（117 行）

```python
async def launch_react_tui(...):
    frontend_dir = get_frontend_dir()  # frontend/terminal/

    # 自动安装前端依赖
    if not (frontend_dir / "node_modules").exists():
        await asyncio.create_subprocess_exec("npm", "install", ...)

    # 通过环境变量传递配置给前端
    env["OPENHARNESS_FRONTEND_CONFIG"] = json.dumps({
        "backend_command": build_backend_command(...),  # Python 后端启动命令
        "initial_prompt": prompt,
    })

    # 启动 Node.js 前端
    process = await asyncio.create_subprocess_exec(
        "npm", "exec", "--", "tsx", "src/index.tsx",
        cwd=str(frontend_dir), env=env,
    )
    return await process.wait()
```

`build_backend_command()` 生成的命令类似：
```
python -m openharness --backend-only --cwd /path/to/project --model claude-sonnet-4
```

### 文件：`src/openharness/ui/backend_host.py`（317 行）

`ReactBackendHost` 是后端主循环，使用 JSON Lines 协议与前端通信：

```python
class ReactBackendHost:
    async def run(self):
        # 1. 构建运行时（同 print_mode）
        self._bundle = await build_runtime(...)
        await start_runtime(self._bundle)

        # 2. 发送 ready 事件（含命令列表、状态快照）
        await self._emit(BackendEvent.ready(...))

        # 3. 主事件循环
        while self._running:
            request = await self._request_queue.get()  # 从 stdin 读取

            if request.type == "shutdown":     break
            if request.type == "submit_line":  await self._process_line(request.line)
            if request.type == "permission_response":  # 权限确认回复
            if request.type == "question_response":    # 问题回答回复
            ...

        # 4. 清理
        await close_runtime(self._bundle)
```

**通信协议**：
```
前端 → 后端（stdin）:  {"type": "submit_line", "line": "Fix the bug"}
后端 → 前端（stdout）: OHJSON:{"type": "assistant_delta", "message": "I'll help..."}
后端 → 前端（stdout）: OHJSON:{"type": "tool_started", "tool_name": "read_file", ...}
后端 → 前端（stdout）: OHJSON:{"type": "line_complete"}
```

**学习要点**：
- 前端和后端是两个独立进程
- 后端通过 `OHJSON:` 前缀的 JSON Lines 向前端发送事件
- 权限确认是异步的：后端发送 `modal_request` → 前端弹窗 → 用户点击 → 前端发送 `permission_response`
- `_process_line()` 内部调用 `handle_line()`，与非交互模式共用同一代码路径

---

## 六、第五层：运行时装配（核心重点）

### 文件：`src/openharness/ui/runtime.py`（433 行）

这是整个项目最重要的文件之一。`build_runtime()` 是系统的 **装配中心**，负责创建和连接所有子系统。

### `build_runtime()` 装配过程（逐行解读）

```python
async def build_runtime(*, prompt, model, max_turns, base_url, 
                        system_prompt, api_key, api_format,
                        api_client, permission_prompt, ask_user_prompt,
                        restore_messages) -> RuntimeBundle:

    # ──── 第 1 步：加载配置 ────
    settings = load_settings().merge_cli_overrides(
        model=model, max_turns=max_turns, base_url=base_url,
        system_prompt=system_prompt, api_key=api_key, api_format=api_format,
    )
    # load_settings() 读取 ~/.openharness/settings.json
    # merge_cli_overrides() 用 CLI 参数覆盖配置文件值
    # 最终 settings 包含完整的运行时配置
```

```python
    # ──── 第 2 步：加载插件 ────
    cwd = str(Path.cwd())
    plugins = load_plugins(settings, cwd)
    # 扫描 ~/.openharness/plugins/ 和 .openharness/plugins/ 两个目录
    # 加载符合 claude-code 插件格式的所有插件
```

```python
    # ──── 第 3 步：创建 API 客户端 ────
    if api_client:                          # 外部注入（测试用）
        resolved_api_client = api_client
    elif settings.api_format == "copilot":  # GitHub Copilot
        resolved_api_client = CopilotClient(model=copilot_model)
    elif settings.api_format == "openai":   # OpenAI 兼容
        resolved_api_client = OpenAICompatibleClient(
            api_key=settings.resolve_api_key(),
            base_url=settings.base_url,
        )
    else:                                   # 默认 Anthropic
        resolved_api_client = AnthropicApiClient(
            api_key=settings.resolve_api_key(),
            base_url=settings.base_url,
        )
    # 三种客户端都实现 SupportsStreamingMessages Protocol
    # 引擎层不关心具体是哪种客户端
```

```python
    # ──── 第 4 步：连接 MCP 服务器 ────
    mcp_manager = McpClientManager(load_mcp_server_configs(settings, plugins))
    await mcp_manager.connect_all()
    # 从 settings.mcp_servers + 插件配置中合并 MCP 服务器列表
    # 异步连接所有配置的 MCP 服务器（Stdio / HTTP / WebSocket）
```

```python
    # ──── 第 5 步：创建工具注册表 ────
    tool_registry = create_default_tool_registry(mcp_manager)
    # 注册 36 个内置工具（Bash, FileRead, FileWrite, Grep, ...）
    # 如果有 MCP 服务器，为每个 MCP 工具创建 McpToolAdapter 并注册
    # 最终 tool_registry 包含所有可用工具
```

```python
    # ──── 第 6 步：检测 Provider 信息 ────
    provider = detect_provider(settings)
    # 根据 base_url 和 model 名自动识别当前 Provider
    # 返回 ProviderInfo(name, auth_kind, voice_supported, ...)
    # 仅用于 UI 显示，不影响客户端行为
```

```python
    # ──── 第 7 步：初始化应用状态 ────
    app_state = AppStateStore(AppState(
        model=settings.model,
        permission_mode=settings.permission.mode.value,
        cwd=cwd,
        provider=provider.name,
        ...
    ))
    # AppStateStore 持有所有 UI 需要展示的状态信息
    # 前端通过 status_snapshot 事件获取这些信息
```

```python
    # ──── 第 8 步：创建 Hook 执行器 ────
    hook_reloader = HookReloader(get_config_file_path())
    hook_executor = HookExecutor(
        hook_reloader.current_registry(),
        HookExecutionContext(cwd=Path(cwd), api_client=resolved_api_client, ...),
    )
    # HookReloader 监听配置文件变更，支持热重载
    # HookExecutor 在工具执行前后触发 PreToolUse / PostToolUse 钩子
```

```python
    # ──── 第 9 步：创建 QueryEngine（核心引擎） ────
    engine = QueryEngine(
        api_client=resolved_api_client,
        tool_registry=tool_registry,
        permission_checker=PermissionChecker(settings.permission),
        cwd=cwd,
        model=settings.model,
        system_prompt=build_runtime_system_prompt(settings, cwd=cwd, latest_user_prompt=prompt),
        max_tokens=settings.max_tokens,
        max_turns=settings.max_turns,
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        hook_executor=hook_executor,
        tool_metadata={"mcp_manager": mcp_manager, "bridge_manager": bridge_manager},
    )
    # 这是整个系统的「大脑」，管理对话历史和 Agent 循环
```

```python
    # ──── 第 10 步：恢复会话（可选） ────
    if restore_messages:
        restored = [ConversationMessage.model_validate(m) for m in restore_messages]
        engine.load_messages(restored)
```

```python
    # ──── 第 11 步：组装 RuntimeBundle ────
    return RuntimeBundle(
        api_client=resolved_api_client,
        cwd=cwd,
        mcp_manager=mcp_manager,
        tool_registry=tool_registry,
        app_state=app_state,
        hook_executor=hook_executor,
        engine=engine,
        commands=create_default_command_registry(),  # 54 个斜杠命令
        session_id=uuid4().hex[:12],
    )
```

### RuntimeBundle 对象关系图

```
RuntimeBundle
├── api_client           ──→ AnthropicApiClient / OpenAICompatibleClient / CopilotClient
├── mcp_manager          ──→ McpClientManager（管理 MCP 连接）
├── tool_registry        ──→ ToolRegistry（42+ 工具注册表）
├── app_state            ──→ AppStateStore（UI 状态）
├── hook_executor        ──→ HookExecutor（Pre/PostToolUse 钩子）
├── engine               ──→ QueryEngine
│   ├── _api_client      ──→ 同上 api_client
│   ├── _tool_registry   ──→ 同上 tool_registry
│   ├── _permission_checker → PermissionChecker（权限检查）
│   ├── _system_prompt   ──→ 组装后的完整 System Prompt
│   ├── _messages        ──→ 对话历史 []
│   └── _cost_tracker    ──→ CostTracker（Token 计费）
├── commands             ──→ CommandRegistry（54 个斜杠命令）
└── session_id           ──→ "a1b2c3d4e5f6"
```

### System Prompt 组装过程

`build_runtime_system_prompt()` 在 `prompts/context.py` 中，组装顺序：

```
1. 基础 System Prompt    ← prompts/system_prompt.py（角色定义 + 行为准则）
   + 环境信息            ← prompts/environment.py（OS, Git, Python, 日期）
2. Fast Mode 提示        ← 如果 settings.fast_mode=True
3. Effort/Passes 设置    ← settings.effort + settings.passes
4. 可用技能列表          ← skills/loader.py（扫描 7 个内置 + 用户技能）
5. CLAUDE.md 内容        ← 项目根目录的 CLAUDE.md
6. Issue/PR 上下文       ← .openharness/issue.md + pr_comments.md
7. MEMORY.md 内容        ← .openharness/memory/MEMORY.md
8. 相关记忆              ← 基于用户输入检索的相关记忆文件
```

**学习要点**：
- `build_runtime()` 是理解整个项目的**核心枢纽**
- 所有子系统在这里创建并连接，形成 `RuntimeBundle`
- `RuntimeBundle` 贯穿整个会话生命周期
- System Prompt 是**动态组装**的，每次用户输入都可能更新

---

## 七、第六层：消息处理

### 文件：`src/openharness/ui/runtime.py` 中的 `handle_line()`

这是**所有用户输入的统一处理入口**，交互模式和非交互模式共用。

```python
async def handle_line(bundle, line, *, print_system, render_event, clear_output):
    # 每次处理前热重载 Hook 注册表
    bundle.hook_executor.update_registry(
        load_hook_registry(bundle.current_settings(), bundle.current_plugins())
    )

    # ──── 分支 1：斜杠命令 ────
    parsed = bundle.commands.lookup(line)  # 如 "/help", "/model gpt-4o"
    if parsed is not None:
        command, args = parsed
        result = await command.handler(args, CommandContext(engine=bundle.engine, ...))
        # 渲染命令结果（可能触发 continue_pending）
        sync_app_state(bundle)
        return not result.should_exit

    # ──── 分支 2：普通消息 → Agent 循环 ────
    settings = bundle.current_settings()
    bundle.engine.set_max_turns(settings.max_turns)

    # 每次用户输入都重新构建 System Prompt（可能更新记忆检索）
    system_prompt = build_runtime_system_prompt(settings, cwd=bundle.cwd, latest_user_prompt=line)
    bundle.engine.set_system_prompt(system_prompt)

    # 提交给引擎，流式处理事件
    try:
        async for event in bundle.engine.submit_message(line):
            await render_event(event)
    except MaxTurnsExceeded as exc:
        await print_system(f"Stopped after {exc.max_turns} turns.")

    # 自动保存会话快照
    save_session_snapshot(cwd=bundle.cwd, messages=bundle.engine.messages, ...)
    sync_app_state(bundle)
    return True
```

**学习要点**：
- `/` 开头的输入走命令系统，其他输入走 Agent 循环
- System Prompt **每次用户输入都会重新构建**
- 会话快照在每次交互后自动保存

---

## 八、第七层：Agent 循环（核心）

### 文件：`src/openharness/engine/query_engine.py`（149 行）

`QueryEngine` 是对话管理器，负责：
- 维护对话历史 `_messages`
- 累计 Token 用量 `_cost_tracker`
- 调用底层 `run_query()` 执行 Agent 循环

```python
class QueryEngine:
    async def submit_message(self, prompt: str) -> AsyncIterator[StreamEvent]:
        # 1. 追加用户消息到历史
        self._messages.append(ConversationMessage.from_user_text(prompt))

        # 2. 创建查询上下文
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            ...
        )

        # 3. 执行 Agent 循环，流式产出事件
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)
            yield event
```

### 文件：`src/openharness/engine/query.py`（244 行）

`run_query()` 是 Agent 循环的**最底层实现**，整个项目最核心的 ~90 行代码：

```python
async def run_query(context, messages):
    compact_state = AutoCompactState()

    for turn in range(context.max_turns):     # 最多 200 轮

        # ──── 阶段 1：自动压缩 ────
        messages, was_compacted = await auto_compact_if_needed(
            messages, api_client=context.api_client, model=context.model, ...
        )

        # ──── 阶段 2：调用 LLM API ────
        async for event in context.api_client.stream_message(
            ApiMessageRequest(
                model=context.model,
                messages=messages,
                system_prompt=context.system_prompt,
                max_tokens=context.max_tokens,
                tools=context.tool_registry.to_api_schema(),  # 传递工具定义
            )
        ):
            if isinstance(event, ApiTextDeltaEvent):
                yield AssistantTextDelta(text=event.text), None    # 流式文本
            if isinstance(event, ApiMessageCompleteEvent):
                final_message = event.message                      # 完整响应

        # 将模型响应加入历史
        messages.append(final_message)
        yield AssistantTurnComplete(message=final_message, usage=usage), usage

        # ──── 阶段 3：检查是否需要工具调用 ────
        if not final_message.tool_uses:
            return  # 模型没有请求工具 → 结束

        # ──── 阶段 4：执行工具 ────
        tool_calls = final_message.tool_uses

        if len(tool_calls) == 1:
            # 单工具：顺序执行
            result = await _execute_tool_call(context, tc.name, tc.id, tc.input)
        else:
            # 多工具：并发执行
            results = await asyncio.gather(*[_execute_tool_call(...) for tc in tool_calls])

        # 将工具结果加入历史
        messages.append(ConversationMessage(role="user", content=tool_results))

        # → 回到阶段 1，开始下一轮

    raise MaxTurnsExceeded(context.max_turns)
```

### 工具执行详情 `_execute_tool_call()`

```python
async def _execute_tool_call(context, tool_name, tool_use_id, tool_input):
    # 1. PreToolUse Hook（可阻止执行）
    if context.hook_executor:
        pre_hooks = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE, ...)
        if pre_hooks.blocked:
            return ToolResultBlock(content="blocked by hook", is_error=True)

    # 2. 查找工具
    tool = context.tool_registry.get(tool_name)
    if tool is None:
        return ToolResultBlock(content=f"Unknown tool: {tool_name}", is_error=True)

    # 3. Pydantic 输入验证
    parsed_input = tool.input_model.model_validate(tool_input)

    # 4. 权限检查
    decision = context.permission_checker.evaluate(
        tool_name,
        is_read_only=tool.is_read_only(parsed_input),
        file_path=..., command=...,
    )
    if not decision.allowed:
        if decision.requires_confirmation:
            confirmed = await context.permission_prompt(tool_name, decision.reason)
            if not confirmed:
                return ToolResultBlock(content="Permission denied", is_error=True)
        else:
            return ToolResultBlock(content=decision.reason, is_error=True)

    # 5. 执行工具
    result = await tool.execute(parsed_input, ToolExecutionContext(cwd=context.cwd, ...))

    # 6. PostToolUse Hook
    if context.hook_executor:
        await context.hook_executor.execute(HookEvent.POST_TOOL_USE, ...)

    return ToolResultBlock(content=result.output, is_error=result.is_error)
```

**学习要点**：
- Agent 循环就是 **调用 LLM → 执行工具 → 把结果给 LLM → 再调用 LLM** 的过程
- 每个工具执行都有 **6 道关卡**：Hook → 查找 → 验证 → 权限 → 执行 → Hook
- 多工具并发用 `asyncio.gather`，单工具顺序执行
- 自动压缩在**每轮 LLM 调用前**触发，保证不超出上下文窗口

---

## 九、完整生命周期时序图

以用户输入 `"Fix the bug in main.py"` 为例：

```
用户输入 "Fix the bug in main.py"
│
├─[handle_line]──────────────────────────────────────────────────
│  ├─ 不是斜杠命令 → 走 Agent 路径
│  ├─ build_runtime_system_prompt() → 重新组装 System Prompt
│  └─ engine.submit_message("Fix the bug in main.py")
│     │
│     ├─[QueryEngine.submit_message]─────────────────────────────
│     │  ├─ messages.append(user_message)
│     │  └─ run_query(context, messages)
│     │     │
│     │     ├─[run_query 第 1 轮]────────────────────────────────
│     │     │  ├─ auto_compact_if_needed()     → Token 检查
│     │     │  ├─ api_client.stream_message()  → 调用 LLM
│     │     │  │  ├─ yield TextDelta("I'll read main.py first")
│     │     │  │  └─ yield Complete(tool_uses=[read_file(path="main.py")])
│     │     │  │
│     │     │  ├─ messages.append(assistant_message)
│     │     │  ├─ yield AssistantTurnComplete
│     │     │  │
│     │     │  ├─ _execute_tool_call("read_file", ...)
│     │     │  │  ├─ PreToolUse Hook     → pass
│     │     │  │  ├─ PermissionChecker   → allowed (read_only)
│     │     │  │  ├─ FileReadTool.execute() → 读取文件内容
│     │     │  │  └─ PostToolUse Hook    → pass
│     │     │  │
│     │     │  ├─ yield ToolExecutionCompleted
│     │     │  └─ messages.append(tool_results)
│     │     │
│     │     ├─[run_query 第 2 轮]────────────────────────────────
│     │     │  ├─ api_client.stream_message()  → LLM 看到文件内容
│     │     │  │  └─ yield Complete(tool_uses=[file_edit(path="main.py", ...)])
│     │     │  │
│     │     │  ├─ _execute_tool_call("file_edit", ...)
│     │     │  │  ├─ PermissionChecker → requires_confirmation
│     │     │  │  ├─ permission_prompt() → 前端弹窗 → 用户按 y
│     │     │  │  └─ FileEditTool.execute() → 修改文件
│     │     │  │
│     │     │  └─ messages.append(tool_results)
│     │     │
│     │     └─[run_query 第 3 轮]────────────────────────────────
│     │        ├─ api_client.stream_message()  → LLM 看到修改结果
│     │        │  └─ yield Complete(text="I've fixed the bug...", tool_uses=[])
│     │        │
│     │        ├─ yield AssistantTurnComplete
│     │        └─ return（无工具调用 → 结束循环）
│     │
│     └─ cost_tracker.add(total_usage)
│
├─ save_session_snapshot()    → 保存到 .openharness/data/sessions/
├─ sync_app_state()           → 更新 UI 状态
└─ return True                → 继续接受输入
```

---

## 十、建议的代码阅读顺序

按照以下顺序阅读源码，由浅入深：

### 第一遍：理解骨架（约 30 分钟）

| 顺序 | 文件 | 行数 | 重点 |
|------|------|------|------|
| 1 | `__main__.py` | 7 | 入口 |
| 2 | `cli.py` 第 397-667 行 | 270 | `main()` 三条路径 |
| 3 | `ui/app.py` | 160 | `run_repl()` 和 `run_print_mode()` |
| 4 | `ui/runtime.py` 第 40-206 行 | 166 | `RuntimeBundle` + `build_runtime()` |
| 5 | `ui/runtime.py` 第 317-407 行 | 90 | `handle_line()` |

### 第二遍：理解引擎（约 20 分钟）

| 顺序 | 文件 | 行数 | 重点 |
|------|------|------|------|
| 6 | `engine/query_engine.py` | 149 | `submit_message()` |
| 7 | `engine/query.py` | 244 | `run_query()` + `_execute_tool_call()` |
| 8 | `engine/messages.py` | 109 | 消息模型 |
| 9 | `engine/stream_events.py` | 50 | 4 种流事件 |

### 第三遍：理解子系统（按需）

| 顺序 | 文件 | 重点 |
|------|------|------|
| 10 | `api/client.py` | Anthropic 客户端 + `SupportsStreamingMessages` Protocol |
| 11 | `tools/base.py` | `BaseTool` 抽象类 + `ToolRegistry` |
| 12 | `permissions/checker.py` | 三级权限模式 |
| 13 | `prompts/context.py` | System Prompt 组装 |
| 14 | `config/settings.py` | 配置加载与合并 |
| 15 | `ui/backend_host.py` | 前后端通信协议 |

### 第四遍：理解扩展系统（按需）

| 文件 | 重点 |
|------|------|
| `skills/loader.py` | 技能发现与加载 |
| `plugins/loader.py` | 插件发现与加载 |
| `hooks/executor.py` | 钩子执行机制 |
| `mcp/client.py` | MCP 连接管理 |
| `services/compact/__init__.py` | 对话压缩策略 |

---

## 十一、调试技巧

### 添加断点观察启动流程

在 `ui/runtime.py` 的 `build_runtime()` 中添加打印：

```python
async def build_runtime(...):
    settings = load_settings().merge_cli_overrides(...)
    print(f"[DEBUG] model={settings.model}, api_format={settings.api_format}")
    print(f"[DEBUG] base_url={settings.base_url}")
    ...
```

### 使用 `--debug` 开启日志

```bash
uv run oh --debug -p "Hello"
```

### 只运行后端（跳过前端）

```bash
# 非交互模式，不需要 Node.js
uv run oh -p "Hello" --output-format stream-json
```

可以看到每个流事件的 JSON 输出，便于理解数据流。

### 使用 pytest 运行引擎测试

```bash
uv run pytest tests/test_engine/test_query_engine.py -v
```

这些测试使用 mock API 客户端，不需要真实 API Key，非常适合理解引擎行为。

---

## 十二、关键设计总结

| 设计决策 | 实现方式 | 文件 |
|----------|----------|------|
| API 客户端可替换 | `SupportsStreamingMessages` Protocol（鸭子类型） | `api/client.py:60` |
| 延迟导入加速启动 | `__getattr__` 在 `__init__.py` 中 | 各模块 `__init__.py` |
| 消息格式统一 | 内部使用 Anthropic 格式，客户端层负责转换 | `engine/messages.py` |
| 权限三级模式 | `PermissionChecker.evaluate()` 统一入口 | `permissions/checker.py` |
| 每次输入重建 Prompt | `handle_line()` 中每次调用 `build_runtime_system_prompt()` | `ui/runtime.py:378` |
| 前后端分离 | JSON Lines over stdin/stdout | `ui/backend_host.py` |
| 工具执行 6 道关卡 | Hook → 查找 → 验证 → 权限 → 执行 → Hook | `engine/query.py:156` |

---

*阅读完本文档后，你应该能够：*
1. *从 `oh` 命令追踪到 Agent 循环的每一步*
2. *理解 `RuntimeBundle` 包含的所有组件及其关系*
3. *知道用户输入的两条处理路径（斜杠命令 vs Agent 循环）*
4. *理解前后端的双进程通信架构*
5. *能够在任意层添加断点进行调试*
