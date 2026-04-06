# 第五~七层：核心运行时 — 从装配到 Agent 循环

> **这是整个项目最重要的学习文档**。前面四层都是"外壳"，本文涵盖的三个文件才是 Agent 真正干活的地方。
>
> **涉及文件**：
> - `ui/runtime.py`（432 行）— 装配 + 消息路由
> - `engine/query_engine.py`（149 行）— 对话管理
> - `engine/query.py`（244 行）— Agent 循环本体
>
> **前置阅读**：[01-startup-overview.md](01-startup-overview.md) 第五~七层概览

---

## 一、三个文件的分工

```
用户输入 "Fix the bug"
    ↓
ui/runtime.py          handle_line()     ← 消息路由：命令还是 Agent？
    ↓
engine/query_engine.py  submit_message()  ← 对话管理：历史 + 成本追踪
    ↓
engine/query.py         run_query()       ← Agent 循环：LLM → 工具 → LLM → ...
```

类比**餐厅**：
- `runtime.py` = **领班**（接待客人、分配座位、结账）
- `query_engine.py` = **经理**（记录每桌点了什么、花了多少钱）
- `query.py` = **厨师**（真正做菜的人）

---

## 二、`runtime.py` — 领班

### 2.1 `RuntimeBundle`：整台机器的零件清单

`build_runtime()` 返回一个 `RuntimeBundle`，它是整个会话的**所有组件的容器**：

```python
@dataclass
class RuntimeBundle:
    api_client: SupportsStreamingMessages   # LLM API 客户端
    cwd: str                                 # 工作目录
    mcp_manager: McpClientManager            # MCP 服务器连接管理
    tool_registry: ToolRegistry              # 42+ 工具注册表
    app_state: AppStateStore                 # UI 状态（模型名、权限模式等）
    hook_executor: HookExecutor              # 生命周期钩子执行器
    engine: QueryEngine                      # 对话引擎（核心）
    commands: object                         # 54 个斜杠命令注册表
    external_api_client: bool                # 是否外部注入客户端（测试用）
    session_id: str                          # 会话 ID
```

**组件关系图**：

```
RuntimeBundle ────────────────────────────────────────────
│                                                         │
│  api_client ──────────────┐                             │
│  tool_registry ───────────┤                             │
│  hook_executor ───────────┤   这 5 个组件在创建时        │
│  permission_checker ──────┼─→ 被注入到 QueryEngine 中    │
│  permission_prompt ───────┘                             │
│                                                         │
│  mcp_manager ─── 提供 MCP 工具 ──→ tool_registry        │
│  app_state ──── 提供 UI 状态 ──→ 前端                    │
│  commands ───── 处理斜杠命令（独立于 engine）             │
│                                                         │
──────────────────────────────────────────────────────────
```

### 2.2 `build_runtime()` 的 11 步装配顺序

为什么是这个顺序？因为后面的组件**依赖**前面的组件：

```
步骤  创建的组件              依赖
───   ──────                ────
 1    settings              无（从文件 + 环境变量 + CLI 参数合并）
 2    plugins               settings
 3    api_client             settings（api_format 决定用哪种客户端）
 4    mcp_manager            settings + plugins（合并 MCP 配置）
 5    tool_registry          mcp_manager（MCP 工具需要适配注册）
 6    provider               settings（自动检测 Provider 信息）
 7    app_state              settings + provider + mcp_manager
 8    hook_executor          settings + plugins + api_client
 9    engine                 api_client + tool_registry + hook_executor + permission_checker
10    restore_messages       engine（可选：恢复会话历史）
11    RuntimeBundle          所有组件打包
```

**第 3 步是最关键的分支点**——根据 `api_format` 创建不同的客户端：

```python
if api_client:                          # 测试注入
    resolved_api_client = api_client
elif settings.api_format == "copilot":  # GitHub Copilot
    resolved_api_client = CopilotClient(model=...)
elif settings.api_format == "openai":   # OpenAI 兼容（DashScope, DeepSeek, ...）
    resolved_api_client = OpenAICompatibleClient(api_key=..., base_url=...)
else:                                   # 默认 Anthropic
    resolved_api_client = AnthropicApiClient(api_key=..., base_url=...)
```

三种客户端都实现 `SupportsStreamingMessages` Protocol，后续代码**完全不关心**具体是哪种。

**第 5 步的工具注册**（`tools/__init__.py:46-94`）：

```python
def create_default_tool_registry(mcp_manager=None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        BashTool(),            # Shell 命令
        FileReadTool(),        # 读文件
        FileWriteTool(),       # 写文件
        FileEditTool(),        # 编辑文件
        GlobTool(),            # 搜索文件名
        GrepTool(),            # 搜索内容
        WebFetchTool(),        # 抓网页
        AgentTool(),           # 子 Agent
        ...                    # 共 36 个内置工具
    ):
        registry.register(tool)

    # 动态注册 MCP 工具
    if mcp_manager is not None:
        for tool_info in mcp_manager.list_tools():
            registry.register(McpToolAdapter(mcp_manager, tool_info))

    return registry
```

**第 9 步的 System Prompt 组装**（`prompts/context.py:34-101`）：

```python
system_prompt = build_runtime_system_prompt(settings, cwd=cwd, latest_user_prompt=prompt)
```

组装了 8 个片段（按顺序拼接）：

```
① 基础角色定义          "You are OpenHarness, an open-source AI coding assistant CLI..."
② 环境信息              "OS: Darwin, Git: yes (branch: main), Python: 3.12..."
③ Fast Mode 提示       （如果启用）
④ Effort/Passes 设置    "Effort: medium, Passes: 1"
⑤ 可用技能列表          "- commit: Create clean git commits\n- review: ..."
⑥ CLAUDE.md 项目指令    （项目根目录的 CLAUDE.md 内容）
⑦ Issue/PR 上下文       （.openharness/issue.md）
⑧ 持久化记忆            MEMORY.md + 基于用户输入检索的相关记忆
```

### 2.3 `handle_line()` — 每次用户输入的入口

```python
async def handle_line(bundle, line, *, print_system, render_event, clear_output) -> bool:
```

这个函数**每次用户提交一行输入**都会被调用（无论交互模式还是非交互模式）。

#### 分支 1：斜杠命令（第 331-374 行）

```python
    parsed = bundle.commands.lookup(line)    # 如 "/help" → (help_command, "")
    if parsed is not None:
        command, args = parsed
        result = await command.handler(args, CommandContext(...))
        # 渲染命令结果
        # 如果命令触发了 continue_pending（如 /continue），继续 Agent 循环
        sync_app_state(bundle)
        return not result.should_exit        # /exit 返回 False → 结束会话
```

`CommandResult` 有几个重要字段：
- `message` — 显示给用户的文本
- `should_exit` — 是否结束会话（`/exit` 用）
- `clear_screen` — 是否清屏（`/clear` 用）
- `continue_pending` — 是否继续中断的 Agent 循环（`/continue` 用）

#### 分支 2：普通消息 → Agent 循环（第 376-407 行）

```python
    # 每次用户输入都重新构建 System Prompt
    settings = bundle.current_settings()                    # 重新读配置（支持热更新）
    bundle.engine.set_max_turns(settings.max_turns)
    system_prompt = build_runtime_system_prompt(             # 重新组装 Prompt
        settings, cwd=bundle.cwd, latest_user_prompt=line    # ← 传入用户输入用于记忆检索
    )
    bundle.engine.set_system_prompt(system_prompt)

    # 提交给引擎
    try:
        async for event in bundle.engine.submit_message(line):
            await render_event(event)                        # 流式渲染到 UI
    except MaxTurnsExceeded as exc:
        await print_system(f"Stopped after {exc.max_turns} turns.")

    # 保存会话快照
    save_session_snapshot(cwd=bundle.cwd, messages=bundle.engine.messages, ...)
    sync_app_state(bundle)                                   # 刷新 UI 状态
    return True                                              # 继续接受输入
```

**关键设计：System Prompt 在每次输入时重新构建**

为什么不一次性构建好？因为：
1. `latest_user_prompt` 参数会影响**记忆检索**——不同的用户输入会匹配到不同的记忆文件
2. `settings` 可能被斜杠命令修改过（如 `/model gpt-4o`）
3. 插件和 Hook 可能被动态加载/卸载

### 2.4 生命周期函数

```python
async def start_runtime(bundle):
    await bundle.hook_executor.execute(HookEvent.SESSION_START, {...})

async def close_runtime(bundle):
    await bundle.mcp_manager.close()                    # 关闭 MCP 连接
    await bundle.hook_executor.execute(HookEvent.SESSION_END, {...})

def sync_app_state(bundle):
    settings = bundle.current_settings()                # 重新读配置
    bundle.engine.set_max_turns(settings.max_turns)     # 同步到引擎
    bundle.app_state.set(model=settings.model, ...)     # 同步到 UI
```

---

## 三、`query_engine.py` — 经理

### 3.1 职责

`QueryEngine` **不执行 Agent 循环**，它是一个**薄封装层**，管理：
- 对话历史（`_messages` 列表）
- Token 成本累积（`_cost_tracker`）
- 可变参数热更新（`set_system_prompt`、`set_model`、`set_max_turns`）

### 3.2 核心方法

```python
class QueryEngine:
    def __init__(self, *, api_client, tool_registry, permission_checker, ...):
        self._messages: list[ConversationMessage] = []   # 对话历史
        self._cost_tracker = CostTracker()                # Token 成本

    async def submit_message(self, prompt: str) -> AsyncIterator[StreamEvent]:
        """用户提交新消息。"""
        # 1. 追加用户消息到历史
        self._messages.append(ConversationMessage.from_user_text(prompt))

        # 2. 打包所有参数为 QueryContext
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            max_turns=self._max_turns,
            permission_prompt=self._permission_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
        )

        # 3. 委托给 run_query() 执行 Agent 循环
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)    # 累加 Token 成本
            yield event                           # 透传事件给上层

    async def continue_pending(self, *, max_turns=None) -> AsyncIterator[StreamEvent]:
        """继续中断的 Agent 循环（不追加新用户消息）。"""
        # 与 submit_message 几乎相同，但不 append 新消息
        context = QueryContext(...)
        async for event, usage in run_query(context, self._messages):
            ...
```

**`submit_message` vs `continue_pending`**：
- `submit_message` — 用户输入了新消息 → 追加到历史 → 执行循环
- `continue_pending` — `/continue` 命令 → 不追加消息 → 从上次中断处继续循环

**`_messages` 是共享的可变引用**：`run_query()` 直接修改 `self._messages` 列表（append 模型回复和工具结果），所以循环结束后历史自动更新。

---

## 四、`query.py` — 厨师（Agent 循环核心）

**这是整个项目最核心的 ~90 行代码。**

### 4.1 `run_query()` 完整逻辑

```python
async def run_query(context: QueryContext, messages: list[ConversationMessage]):
    compact_state = AutoCompactState()

    for turn in range(context.max_turns):    # 最多 200 轮

        # ═══════ 阶段 A：自动压缩 ═══════
        messages, was_compacted = await auto_compact_if_needed(messages, ...)
        # Token 超阈值 → 先清旧工具结果（cheap），不够再 LLM 摘要（expensive）

        # ═══════ 阶段 B：调用 LLM API ═══════
        async for event in context.api_client.stream_message(
            ApiMessageRequest(
                model=context.model,
                messages=messages,
                system_prompt=context.system_prompt,
                max_tokens=context.max_tokens,
                tools=context.tool_registry.to_api_schema(),  # ← 传递所有工具定义
            )
        ):
            if isinstance(event, ApiTextDeltaEvent):
                yield AssistantTextDelta(text=event.text), None     # 流式文本
            if isinstance(event, ApiMessageCompleteEvent):
                final_message = event.message                       # 完整回复

        # 将模型回复加入历史
        messages.append(final_message)
        yield AssistantTurnComplete(message=final_message, usage=usage), usage

        # ═══════ 阶段 C：检查是否结束 ═══════
        if not final_message.tool_uses:
            return    # 没有工具调用 → 循环结束 ✓

        # ═══════ 阶段 D：执行工具 ═══════
        tool_calls = final_message.tool_uses

        if len(tool_calls) == 1:
            # 单工具：顺序执行（立即 yield 事件）
            yield ToolExecutionStarted(...)
            result = await _execute_tool_call(context, ...)
            yield ToolExecutionCompleted(...)
        else:
            # 多工具：并发执行（先 yield 所有 Started，再并发执行，再 yield 所有 Completed）
            for tc in tool_calls:
                yield ToolExecutionStarted(...)
            results = await asyncio.gather(*[_execute_tool_call(...) for tc in tool_calls])
            for tc, result in zip(tool_calls, results):
                yield ToolExecutionCompleted(...)

        # 工具结果追加到历史（作为 user 消息）
        messages.append(ConversationMessage(role="user", content=tool_results))

        # → 回到阶段 A，开始下一轮

    raise MaxTurnsExceeded(context.max_turns)    # 超过最大轮数
```

### 4.2 `_execute_tool_call()` — 工具执行的 6 道关卡

```python
async def _execute_tool_call(context, tool_name, tool_use_id, tool_input):

    # 关卡 1：PreToolUse Hook（可阻止执行）
    if context.hook_executor is not None:
        pre_hooks = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE, ...)
        if pre_hooks.blocked:
            return ToolResultBlock(content="blocked by hook", is_error=True)

    # 关卡 2：工具查找
    tool = context.tool_registry.get(tool_name)
    if tool is None:
        return ToolResultBlock(content=f"Unknown tool: {tool_name}", is_error=True)

    # 关卡 3：Pydantic 输入验证
    try:
        parsed_input = tool.input_model.model_validate(tool_input)
    except Exception as exc:
        return ToolResultBlock(content=f"Invalid input: {exc}", is_error=True)

    # 关卡 4：权限检查
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

    # 关卡 5：实际执行
    result = await tool.execute(parsed_input, ToolExecutionContext(cwd=context.cwd, ...))

    # 关卡 6：PostToolUse Hook
    if context.hook_executor is not None:
        await context.hook_executor.execute(HookEvent.POST_TOOL_USE, ...)

    return ToolResultBlock(content=result.output, is_error=result.is_error)
```

**权限检查的优先级**（`permissions/checker.py:50-106`）：

```
工具黑名单? → 拒绝
    ↓ 不在黑名单
工具白名单? → 放行
    ↓ 不在白名单
路径匹配拒绝规则? → 拒绝（如 /etc/* 被拒绝）
    ↓ 无匹配
命令匹配拒绝模式? → 拒绝（如 rm -rf / 被拒绝）
    ↓ 无匹配
FULL_AUTO 模式? → 放行
    ↓ 不是
只读操作? → 放行（read_file、grep 等）
    ↓ 不是只读
PLAN 模式? → 拒绝
    ↓ 不是
DEFAULT 模式 → 需要用户确认（弹窗 y/n）
```

### 4.3 工具抽象：`BaseTool`

每个工具都继承 `BaseTool`（`tools/base.py`）：

```python
class BaseTool(ABC):
    name: str                       # 如 "read_file"
    description: str                # 如 "Read the contents of a file"
    input_model: type[BaseModel]    # Pydantic 模型，定义参数结构

    async def execute(self, arguments, context) -> ToolResult:
        """执行工具。"""
        ...

    def is_read_only(self, arguments) -> bool:
        """是否只读（权限系统用）。默认 False。"""
        return False

    def to_api_schema(self) -> dict:
        """生成 JSON Schema（发给 LLM，让模型知道怎么调用）。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
```

`to_api_schema()` 生成的 JSON 会在每轮 LLM 请求中发送给模型（`context.tool_registry.to_api_schema()`），让模型知道有哪些工具可用以及参数格式。

---

## 五、数据流全景图

以 `"Fix the bug in main.py"` 为例，追踪数据从用户输入到 LLM 再到工具执行的完整流转：

```
用户输入 "Fix the bug in main.py"
    │
    ▼
handle_line(bundle, "Fix the bug in main.py")
    │
    ├─ 不是斜杠命令
    ├─ build_runtime_system_prompt()     →  重组 System Prompt（含记忆检索）
    ├─ engine.set_system_prompt(...)
    │
    ▼
engine.submit_message("Fix the bug in main.py")
    │
    ├─ messages.append({role:"user", content:[{type:"text", text:"Fix the bug..."}]})
    │
    ▼
run_query(context, messages)
    │
    ╔═══════════ 第 1 轮 ═══════════╗
    ║                                ║
    ║  auto_compact_if_needed()      ║  Token 检查 → 不需要压缩
    ║          ↓                     ║
    ║  api_client.stream_message({   ║  HTTP POST 到 LLM API
    ║    model: "claude-sonnet-4",   ║
    ║    messages: [用户消息],        ║
    ║    system_prompt: "You are..", ║
    ║    tools: [42个工具的JSON Schema] ║
    ║  })                            ║
    ║          ↓                     ║
    ║  LLM 返回:                     ║
    ║    text: ""                    ║  ← 不说话，直接调工具
    ║    tool_use: read_file(main.py)║
    ║          ↓                     ║
    ║  messages.append(assistant_msg)║
    ║  yield TurnComplete            ║
    ║          ↓                     ║
    ║  tool_uses 非空 → 执行工具     ║
    ║          ↓                     ║
    ║  _execute_tool_call:           ║
    ║    1. PreHook → pass           ║
    ║    2. registry.get("read_file")║ → FileReadTool 实例
    ║    3. validate({path:"main.py"})║
    ║    4. permission: is_read_only ║ → True → 放行
    ║    5. tool.execute()           ║ → 读取文件内容
    ║    6. PostHook → pass          ║
    ║          ↓                     ║
    ║  messages.append({             ║
    ║    role:"user",                ║  ← 注意：工具结果以 user 角色追加
    ║    content:[tool_result]       ║
    ║  })                            ║
    ║                                ║
    ╚════════════════════════════════╝
    │
    ╔═══════════ 第 2 轮 ═══════════╗
    ║                                ║
    ║  api_client.stream_message({   ║  第 2 次 HTTP 请求
    ║    messages: [                  ║  包含完整历史：
    ║      user("Fix the bug..."),   ║    用户消息
    ║      assistant(tool_use),      ║    模型的工具调用
    ║      user(tool_result),        ║    工具执行结果
    ║    ],                          ║
    ║    ...                         ║
    ║  })                            ║
    ║          ↓                     ║
    ║  LLM 返回:                     ║
    ║    text: "找到了 bug，修复中"   ║
    ║    tool_use: file_edit(...)    ║
    ║          ↓                     ║
    ║  _execute_tool_call:           ║
    ║    4. permission: 写操作       ║ → requires_confirmation
    ║    → permission_prompt()       ║ → 弹窗确认 → 用户按 y
    ║    5. tool.execute()           ║ → 修改文件
    ║                                ║
    ╚════════════════════════════════╝
    │
    ╔═══════════ 第 3 轮 ═══════════╗
    ║                                ║
    ║  LLM 返回:                     ║
    ║    text: "已修复！bug 是..."    ║
    ║    tool_uses: []               ║  ← 无工具调用
    ║          ↓                     ║
    ║  return                        ║  ← 循环结束
    ║                                ║
    ╚════════════════════════════════╝
    │
    ▼
回到 handle_line()
    ├─ save_session_snapshot()       →  保存到 ~/.openharness/data/sessions/
    ├─ sync_app_state()              →  刷新 UI 状态
    └─ return True                   →  继续接受下一次输入
```

---

## 六、关键设计模式总结

| 模式 | 在哪里 | 为什么这样设计 |
|------|--------|--------------|
| **Protocol 鸭子类型** | `SupportsStreamingMessages` | 三种 API 客户端可互换，引擎层零改动 |
| **策略模式** | `handle_line` 的 3 个回调 | 同一份逻辑服务交互 + 非交互两种模式 |
| **AsyncIterator 流式** | `run_query` 用 `yield` | 每个 token 立即传递给上层，不缓冲 |
| **单工具顺序 / 多工具并发** | `run_query` 第 122-149 行 | 单工具可以更快 yield 事件，多工具并发提高效率 |
| **6 道关卡** | `_execute_tool_call` | 每道关卡失败都返回 error ToolResult，不抛异常 |
| **动态 System Prompt** | `handle_line` 每次重建 | 支持热更新配置 + 基于输入的记忆检索 |
| **共享可变列表** | `messages` 传入 `run_query` | 循环直接 append，引擎外部能看到更新后的历史 |

---

## 七、动手实验

### 实验 1：在 `run_query` 入口打印每轮信息

在 `engine/query.py` 第 80 行后加：

```python
    for turn_num in range(context.max_turns):
        print(f"[TURN {turn_num+1}] messages={len(messages)}, "
              f"last_role={messages[-1].role if messages else 'none'}", file=__import__('sys').stderr)
```

### 实验 2：在 `_execute_tool_call` 观察权限检查

在 `engine/query.py` 第 194 行后加：

```python
    decision = context.permission_checker.evaluate(...)
    print(f"[PERM] {tool_name}: allowed={decision.allowed}, "
          f"confirm={decision.requires_confirmation}, "
          f"reason={decision.reason}", file=__import__('sys').stderr)
```

### 实验 3：观察 System Prompt 的动态内容

在 `runtime.py` 第 378 行后加：

```python
    system_prompt = build_runtime_system_prompt(...)
    print(f"[PROMPT] length={len(system_prompt)} chars", file=__import__('sys').stderr)
```

运行 `uv run oh -p "Hello" 2>debug.log`，然后 `cat debug.log` 查看。

---

## 八、关联阅读

| 方向 | 文件 | 说明 |
|------|------|------|
| ↑ 调用方 | `ui/app.py` / `ui/backend_host.py` | 两种模式都调用 `handle_line()` |
| → 工具实现 | `tools/bash_tool.py`, `tools/file_read_tool.py` ... | 42 个具体工具实现 |
| → 权限系统 | `permissions/checker.py` | 权限检查的完整逻辑（107 行） |
| → Prompt 组装 | `prompts/context.py` + `prompts/system_prompt.py` | System Prompt 的 8 个片段 |
| → 对话压缩 | `services/compact/__init__.py` | microcompact + LLM 摘要（493 行） |
| → 消息模型 | `engine/messages.py` | ConversationMessage, TextBlock, ToolUseBlock |
