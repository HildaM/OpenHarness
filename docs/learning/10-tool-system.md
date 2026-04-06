# 工具系统深度剖析

> 涉及源文件：`tools/base.py`(76行) · `tools/__init__.py`(104行) · `tools/file_read_tool.py`(63行) · `tools/bash_tool.py`(73行) · `tools/file_edit_tool.py`(55行) · `tools/web_fetch_tool.py`(62行) · `tools/mcp_tool.py`(56行) · `engine/query.py`(361行)
>
> 预计阅读时间：35 分钟
>
> 前置知识：已读完 06（runtime 装配 + Agent 循环）

---

## 一、工具系统在全局中的位置

回顾启动流程的第 5 步和 Agent 循环的阶段 D：

```
build_runtime()
  ├─ 第5步: create_default_tool_registry(mcp_manager)  ← 注册所有工具
  └─ 第9步: QueryEngine(tool_registry=tool_registry)   ← 注入引擎

run_query() 阶段 D
  └─ _execute_tool_call(context, tool_name, tool_use_id, tool_input)
       ├─ 关卡2: context.tool_registry.get(tool_name)  ← 查找工具
       ├─ 关卡3: tool.input_model.model_validate(...)   ← 验证参数
       └─ 关卡5: tool.execute(parsed_input, context)    ← 执行工具
```

**核心问题**：LLM 说"我要调 `read_file`"，系统怎么知道该调谁、怎么调？答案就在工具系统的三层架构中。

---

## 二、三层架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 层：抽象层（base.py — 76 行）                           │
│                                                             │
│   BaseTool          工具的接口契约（抽象类）                   │
│   ToolRegistry      工具名 → 工具实例的映射表                  │
│   ToolResult        工具执行结果的标准格式                     │
│   ToolExecutionContext  工具执行时的共享上下文                  │
└────────────────────────────┬────────────────────────────────┘
                             │ 继承
┌────────────────────────────▼────────────────────────────────┐
│ 第 2 层：实现层（36 个工具文件，每个 30~120 行）                │
│                                                             │
│   FileReadTool, BashTool, FileEditTool, GrepTool, ...       │
│   McpToolAdapter（MCP 协议适配器）                            │
└────────────────────────────┬────────────────────────────────┘
                             │ 注册到
┌────────────────────────────▼────────────────────────────────┐
│ 第 3 层：注册层（__init__.py — 104 行）                       │
│                                                             │
│   create_default_tool_registry()                             │
│     → 实例化 36 个内置工具                                    │
│     → 为每个 MCP 工具创建 McpToolAdapter                      │
│     → 返回完整的 ToolRegistry                                │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、第 1 层：抽象层（base.py）

### 文件：`src/openharness/tools/base.py`（76 行）

这个文件定义了工具系统的全部接口契约，只有 4 个类，极其精简。

### 3.1 ToolExecutionContext — 执行上下文

```python
@dataclass
class ToolExecutionContext:
    """工具执行时的共享上下文"""
    cwd: Path                                    # 当前工作目录
    metadata: dict[str, Any] = field(default_factory=dict)  # 额外元数据
```

**设计要点**：
- `cwd` 是必需的 — 几乎所有文件/命令工具都需要解析相对路径
- `metadata` 是个万能口袋 — 承载 `mcp_manager`、`ask_user_prompt`、`tool_registry` 等
- `metadata` 在 `_execute_tool_call()` 中被填充：

```python
# engine/query.py 关卡5
result = await tool.execute(
    parsed_input,
    ToolExecutionContext(
        cwd=context.cwd,
        metadata={
            "tool_registry": context.tool_registry,    # AgentTool 需要
            "ask_user_prompt": context.ask_user_prompt, # AskUserQuestionTool 需要
            **(context.tool_metadata or {}),            # mcp_manager 等
        },
    ),
)
```

### 3.2 ToolResult — 执行结果

```python
@dataclass(frozen=True)
class ToolResult:
    """标准化的工具执行结果"""
    output: str                                  # 输出文本（LLM 会看到这个）
    is_error: bool = False                       # 是否出错
    metadata: dict[str, Any] = field(default_factory=dict)  # 额外元数据
```

**设计要点**：
- `frozen=True` — 不可变，一旦创建不能修改
- `output` 是**给 LLM 看的文本** — LLM 根据这个内容决定下一步动作
- `is_error=True` 不会中断 Agent 循环 — 错误信息作为工具结果反馈给 LLM，让 LLM 自行调整策略
- `metadata` 目前只有 `BashTool` 用到（存 `returncode`）

### 3.3 BaseTool — 工具基类（核心接口）

```python
class BaseTool(ABC):
    """所有工具的基类"""

    name: str                    # 工具名（LLM 通过这个名字调用工具）
    description: str             # 工具描述（LLM 通过这个理解工具能力）
    input_model: type[BaseModel] # 输入参数的 Pydantic 模型

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        """执行工具 — 唯一必须实现的方法"""

    def is_read_only(self, arguments: BaseModel) -> bool:
        """是否只读 — 影响权限检查（只读工具在 DEFAULT 模式下自动放行）"""
        return False  # 默认非只读

    def to_api_schema(self) -> dict[str, Any]:
        """生成 Anthropic API 需要的 JSON Schema"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
```

**关键设计决策**：

| 设计 | 原因 |
|------|------|
| `name` / `description` / `input_model` 作为类属性 | 编译期确定，不需要实例化参数 |
| `input_model` 用 Pydantic BaseModel | 自动生成 JSON Schema + 自动参数验证 |
| `execute()` 是 `async` | 很多工具涉及 IO（文件读写、HTTP 请求、子进程） |
| `is_read_only()` 接受 `arguments` | 某些工具的只读性取决于参数（如 `bash` 的命令内容） |
| `to_api_schema()` 生成标准格式 | 直接对接 Anthropic Messages API 的 `tools` 参数 |

### 3.4 ToolRegistry — 工具注册表

```python
class ToolRegistry:
    """工具名 → 工具实例的映射表"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}    # 内部用 dict 存储

    def register(self, tool: BaseTool) -> None:  # 注册
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None: # 按名查找
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:       # 列出全部
        return list(self._tools.values())

    def to_api_schema(self) -> list[dict[str, Any]]:  # 所有工具的 JSON Schema
        return [tool.to_api_schema() for tool in self._tools.values()]
```

**使用场景**：

```python
# 注册时（build_runtime 第5步）
registry = ToolRegistry()
registry.register(FileReadTool())

# 查找时（_execute_tool_call 关卡2）
tool = context.tool_registry.get("read_file")

# 生成 API Schema 时（run_query 阶段B）
tools=context.tool_registry.to_api_schema()  # 发给 LLM，告诉它有哪些工具可用
```

---

## 四、第 2 层：工具实现（具体工具分析）

### 4.1 最简工具：FileReadTool（63 行）

这是理解工具实现模式的**最佳起点** — 只做一件事：读文件。

```python
# ──── 第 1 步：定义输入模型 ────
class FileReadToolInput(BaseModel):
    path: str = Field(description="Path of the file to read")
    offset: int = Field(default=0, ge=0, description="Zero-based starting line")
    limit: int = Field(default=200, ge=1, le=2000, description="Number of lines to return")
```

**要点**：
- 继承 `BaseModel` — Pydantic 自动生成 JSON Schema，也自动做参数验证
- `Field(description=...)` — 描述会出现在发给 LLM 的工具定义中，帮助 LLM 理解参数含义
- `Field(default=..., ge=..., le=...)` — 设置默认值和取值范围约束

```python
# ──── 第 2 步：定义工具类 ────
class FileReadTool(BaseTool):
    name = "read_file"                              # LLM 调用时用的名字
    description = "Read a text file from the local repository."
    input_model = FileReadToolInput                 # 关联输入模型

    def is_read_only(self, arguments: FileReadToolInput) -> bool:
        return True                                  # 读文件永远是只读的

    async def execute(self, arguments: FileReadToolInput, context: ToolExecutionContext) -> ToolResult:
        # 1. 路径解析：相对路径 → 基于 cwd 的绝对路径
        path = _resolve_path(context.cwd, arguments.path)

        # 2. 存在性检查
        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(output=f"Cannot read directory: {path}", is_error=True)

        # 3. 二进制检测（包含 \x00 就认为是二进制文件）
        raw = path.read_bytes()
        if b"\x00" in raw:
            return ToolResult(output=f"Binary file cannot be read as text: {path}", is_error=True)

        # 4. 读取并添加行号
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[arguments.offset : arguments.offset + arguments.limit]
        numbered = [
            f"{arguments.offset + index + 1:>6}\t{line}"    # 右对齐6位行号
            for index, line in enumerate(selected)
        ]

        # 5. 返回结果（LLM 会看到带行号的文件内容）
        if not numbered:
            return ToolResult(output=f"(no content in selected range for {path})")
        return ToolResult(output="\n".join(numbered))
```

**实现模式总结**：

```
每个工具 = InputModel（Pydantic） + ToolClass（BaseTool 子类）

InputModel 负责：
  - 定义参数名、类型、默认值、取值范围
  - 生成 JSON Schema（给 LLM 看）
  - 自动验证输入（关卡3 做的事）

ToolClass 负责：
  - 声明 name / description / input_model（3 个类属性）
  - 实现 execute()（核心逻辑）
  - 可选覆盖 is_read_only()（影响权限判断）
```

### 4.2 BashTool — Shell 命令执行（73 行）

比 FileReadTool 复杂一些，涉及子进程管理和超时控制。

```python
class BashToolInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    cwd: str | None = Field(default=None, description="Working directory override")
    timeout_seconds: int = Field(default=120, ge=1, le=600)  # 默认2分钟，最长10分钟

class BashTool(BaseTool):
    name = "bash"
    description = "Run a shell command in the local repository."
    input_model = BashToolInput

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        cwd = Path(arguments.cwd).expanduser() if arguments.cwd else context.cwd

        # 1. 创建子进程（可能受沙箱限制）
        try:
            process = await create_shell_subprocess(
                arguments.command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)

        # 2. 等待执行（带超时）
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()          # 超时则杀死进程
            await process.wait()
            return ToolResult(output=f"Command timed out after {arguments.timeout_seconds} seconds", is_error=True)

        # 3. 合并 stdout + stderr
        parts = []
        if stdout:
            parts.append(stdout.decode("utf-8", errors="replace").rstrip())
        if stderr:
            parts.append(stderr.decode("utf-8", errors="replace").rstrip())
        text = "\n".join(part for part in parts if part).strip() or "(no output)"

        # 4. 截断过长输出（>12000 字符）
        if len(text) > 12000:
            text = f"{text[:12000]}\n...[truncated]..."

        # 5. returncode != 0 → is_error=True
        return ToolResult(
            output=text,
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode},
        )
```

**关键差异对比**：

| 特性 | FileReadTool | BashTool |
|------|-------------|----------|
| `is_read_only()` | 返回 `True` | 默认 `False`（需要权限确认） |
| 超时控制 | 无（同步文件读取） | `asyncio.wait_for` + 超时杀进程 |
| 输出截断 | 无 | 12000 字符上限 |
| metadata | 无 | `{"returncode": ...}` |
| 错误处理 | 文件不存在/是目录/是二进制 | 沙箱不可用/超时/非零退出 |

### 4.3 FileEditTool — 文件编辑（55 行）

```python
class FileEditToolInput(BaseModel):
    path: str = Field(description="Path of the file to edit")
    old_str: str = Field(description="Existing text to replace")
    new_str: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False)

class FileEditTool(BaseTool):
    name = "edit_file"
    description = "Edit an existing file by replacing a string."
    input_model = FileEditToolInput

    async def execute(self, arguments: FileEditToolInput, context: ToolExecutionContext) -> ToolResult:
        path = _resolve_path(context.cwd, arguments.path)
        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)

        original = path.read_text(encoding="utf-8")
        if arguments.old_str not in original:
            return ToolResult(output="old_str was not found in the file", is_error=True)

        # 默认只替换第一个匹配，replace_all=True 时替换全部
        if arguments.replace_all:
            updated = original.replace(arguments.old_str, arguments.new_str)
        else:
            updated = original.replace(arguments.old_str, arguments.new_str, 1)

        path.write_text(updated, encoding="utf-8")
        return ToolResult(output=f"Updated {path}")
```

**设计思路**：
- **字符串匹配替换**而非行号定位 — 更稳健，不怕并发修改导致行号偏移
- `old_str` 必须在文件中找到 — 找不到直接报错，LLM 会重新读文件再尝试
- 默认只替换第一个匹配 — 防止误改

### 4.4 WebFetchTool — 网页抓取（62 行）

```python
class WebFetchToolInput(BaseModel):
    url: str = Field(description="HTTP or HTTPS URL to fetch")
    max_chars: int = Field(default=12000, ge=500, le=50000)

class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "Fetch one web page and return compact readable text."
    input_model = WebFetchToolInput

    async def execute(self, arguments: WebFetchToolInput, context: ToolExecutionContext) -> ToolResult:
        del context  # 不需要 cwd，忽略上下文

        # 1. HTTP GET（跟随重定向，20秒超时）
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
                response = await client.get(arguments.url, headers={"User-Agent": "OpenHarness/0.1"})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return ToolResult(output=f"web_fetch failed: {exc}", is_error=True)

        # 2. HTML → 纯文本（去除 script/style 标签）
        content_type = response.headers.get("content-type", "")
        body = response.text
        if "html" in content_type:
            body = _html_to_text(body)

        # 3. 截断
        if len(body) > arguments.max_chars:
            body = body[: arguments.max_chars].rstrip() + "\n...[truncated]"

        # 4. 返回 URL + 状态码 + 内容
        return ToolResult(output=f"URL: {response.url}\nStatus: {response.status_code}\n...\n{body}")

    def is_read_only(self, arguments: BaseModel) -> bool:
        return True  # 网页抓取是只读的
```

**注意**：`del context` 这个写法在项目中很常见 — 表示"我知道有这个参数但不需要用"，避免 linter 报未使用变量的警告。

### 4.5 McpToolAdapter — MCP 工具适配器（56 行，重要设计模式）

这是一个**适配器模式**的典型应用 — 把外部 MCP 协议的工具包装成内部 `BaseTool` 接口。

```python
class McpToolAdapter(BaseTool):
    """把一个 MCP 工具伪装成普通的 OpenHarness 工具"""

    def __init__(self, manager: McpClientManager, tool_info: McpToolInfo) -> None:
        self._manager = manager
        self._tool_info = tool_info

        # 命名规则：mcp__{server}__{tool}（双下划线分隔）
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"  # 如 "mcp__github__list_repos"

        self.description = tool_info.description or f"MCP tool {tool_info.name}"

        # 动态生成 Pydantic 模型（从 MCP 工具的 JSON Schema 转换）
        self.input_model = _input_model_from_schema(self.name, tool_info.input_schema)

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del context
        # 委托给 MCP 管理器调用远程工具
        output = await self._manager.call_tool(
            self._tool_info.server_name,
            self._tool_info.name,
            arguments.model_dump(mode="json"),
        )
        return ToolResult(output=output)
```

**动态模型生成**：

```python
def _input_model_from_schema(tool_name: str, schema: dict) -> type[BaseModel]:
    """从 MCP 的 JSON Schema 动态创建 Pydantic 模型"""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}
    for key in properties:
        default = ... if key in required else None  # ... 表示必填
        fields[key] = (object | None, Field(default=default))

    # Pydantic 的 create_model 可以在运行时动态创建模型类
    return create_model(f"{tool_name.title()}Input", **fields)
```

**这个设计的精妙之处**：
1. MCP 工具在运行时才知道有哪些、参数是什么
2. 通过 `create_model` 动态创建 Pydantic 模型 → 复用了相同的参数验证和 Schema 生成机制
3. 对 `_execute_tool_call()` 来说，MCP 工具和内置工具**完全透明** — 同样的 6 道关卡

---

## 五、第 3 层：注册层（__init__.py）

### 文件：`src/openharness/tools/__init__.py`（104 行）

```python
def create_default_tool_registry(mcp_manager=None) -> ToolRegistry:
    """创建并返回包含所有工具的注册表"""
    registry = ToolRegistry()

    # ── 注册 36 个内置工具 ──
    for tool in (
        BashTool(),           # Shell 命令
        AskUserQuestionTool(),# 向用户提问
        FileReadTool(),       # 读文件
        FileWriteTool(),      # 写文件
        FileEditTool(),       # 编辑文件
        NotebookEditTool(),   # 编辑 Jupyter Notebook
        LspTool(),            # 语言服务器协议
        McpAuthTool(),        # MCP 认证
        GlobTool(),           # 文件名搜索
        GrepTool(),           # 文件内容搜索
        SkillTool(),          # 技能加载
        ToolSearchTool(),     # 工具搜索
        WebFetchTool(),       # 网页抓取
        WebSearchTool(),      # 网页搜索
        ConfigTool(),         # 配置管理
        BriefTool(),          # 简洁模式切换
        SleepTool(),          # 等待
        EnterWorktreeTool(),  # 进入 Git worktree
        ExitWorktreeTool(),   # 退出 Git worktree
        TodoWriteTool(),      # TODO 管理
        EnterPlanModeTool(),  # 进入计划模式
        ExitPlanModeTool(),   # 退出计划模式
        CronCreateTool(),     # 创建定时任务
        CronListTool(),       # 列出定时任务
        CronDeleteTool(),     # 删除定时任务
        CronToggleTool(),     # 切换定时任务
        RemoteTriggerTool(),  # 远程触发
        TaskCreateTool(),     # 创建子任务
        TaskGetTool(),        # 查询子任务
        TaskListTool(),       # 列出子任务
        TaskStopTool(),       # 停止子任务
        TaskOutputTool(),     # 获取子任务输出
        TaskUpdateTool(),     # 更新子任务
        AgentTool(),          # 派生子 Agent
        SendMessageTool(),    # Agent 间通信
        TeamCreateTool(),     # 创建团队
        TeamDeleteTool(),     # 删除团队
    ):
        registry.register(tool)

    # ── 注册 MCP 工具（动态数量） ──
    if mcp_manager is not None:
        registry.register(ListMcpResourcesTool(mcp_manager))  # MCP 资源列表
        registry.register(ReadMcpResourceTool(mcp_manager))   # MCP 资源读取
        for tool_info in mcp_manager.list_tools():
            registry.register(McpToolAdapter(mcp_manager, tool_info))  # 每个 MCP 工具一个适配器

    return registry
```

### 36 个内置工具分类

| 类别 | 工具 | 数量 |
|------|------|------|
| **文件操作** | `read_file`, `write_file`, `edit_file`, `notebook_edit`, `glob`, `grep` | 6 |
| **系统命令** | `bash`, `sleep` | 2 |
| **网络** | `web_fetch`, `web_search` | 2 |
| **用户交互** | `ask_user_question` | 1 |
| **配置管理** | `config`, `brief` | 2 |
| **Git** | `enter_worktree`, `exit_worktree` | 2 |
| **定时任务** | `cron_create`, `cron_list`, `cron_delete`, `cron_toggle` | 4 |
| **子任务/Agent** | `task_create`, `task_get`, `task_list`, `task_stop`, `task_output`, `task_update`, `agent` | 7 |
| **团队协作** | `send_message`, `team_create`, `team_delete` | 3 |
| **扩展系统** | `skill`, `tool_search`, `mcp_auth`, `lsp`, `enter_plan_mode`, `exit_plan_mode` | 6 |
| **其他** | `todo_write`, `remote_trigger` | 2 |
| **MCP** | `list_mcp_resources`, `read_mcp_resource`, `mcp__*` (动态) | 2+N |

---

## 六、工具与 LLM 的通信协议

### 6.1 工具定义是怎么传给 LLM 的？

在 `run_query()` 阶段 B 调用 LLM 时：

```python
async for event in context.api_client.stream_message(
    ApiMessageRequest(
        model=context.model,
        messages=messages,
        system_prompt=context.system_prompt,
        max_tokens=context.max_tokens,
        tools=context.tool_registry.to_api_schema(),  # ← 这里
    )
)
```

`to_api_schema()` 生成的格式（以 `read_file` 为例）：

```json
{
  "name": "read_file",
  "description": "Read a text file from the local repository.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path of the file to read"
      },
      "offset": {
        "type": "integer",
        "default": 0,
        "minimum": 0,
        "description": "Zero-based starting line"
      },
      "limit": {
        "type": "integer",
        "default": 200,
        "minimum": 1,
        "maximum": 2000,
        "description": "Number of lines to return"
      }
    },
    "required": ["path"]
  }
}
```

**数据转换链**：

```
Python Pydantic Field(description=..., ge=..., le=...)
  → model_json_schema()（Pydantic 内置方法）
  → JSON Schema 格式的 dict
  → 作为 API 请求的 tools 参数发送给 LLM
  → LLM 根据 JSON Schema 生成合法的参数
  → 回传后用 model_validate() 验证
```

### 6.2 LLM 调用工具后的数据流

```
LLM 响应中包含 ToolUseBlock:
  {"type": "tool_use", "id": "toolu_xxx", "name": "read_file", "input": {"path": "main.py"}}
    │
    ├─ tool_name = "read_file"
    ├─ tool_use_id = "toolu_xxx"
    └─ tool_input = {"path": "main.py"}
        │
        ▼
_execute_tool_call(context, "read_file", "toolu_xxx", {"path": "main.py"})
    │
    ├─ 关卡2: tool = registry.get("read_file") → FileReadTool 实例
    ├─ 关卡3: parsed = FileReadToolInput.model_validate({"path": "main.py"})
    ├─ 关卡4: permission_checker.evaluate("read_file", is_read_only=True, ...)
    │          → allowed（只读工具自动放行）
    └─ 关卡5: tool.execute(parsed, context)
        │
        ▼
    ToolResult(output="     1\timport os\n     2\t...", is_error=False)
        │
        ▼
    ToolResultBlock(tool_use_id="toolu_xxx", content="     1\timport os\n...", is_error=False)
        │
        ▼
    追加到 messages（role="user"），配对 tool_use_id
        │
        ▼
    下一轮 LLM 调用时，LLM 看到文件内容，决定下一步
```

---

## 七、工具与权限系统的交互

`is_read_only()` 是工具与权限系统的**唯一接口**。权限检查在 `_execute_tool_call()` 的关卡 4 中进行：

```python
decision = context.permission_checker.evaluate(
    tool_name,
    is_read_only=tool.is_read_only(parsed_input),  # ← 工具提供的信号
    file_path=_file_path,
    command=_command,
)
```

各工具的 `is_read_only()` 实现：

| 工具 | `is_read_only()` | 原因 |
|------|-----------------|------|
| `FileReadTool` | 始终 `True` | 只读取文件内容 |
| `GrepTool` | 始终 `True` | 只搜索不修改 |
| `GlobTool` | 始终 `True` | 只列出文件名 |
| `WebFetchTool` | 始终 `True` | 只读取网页 |
| `WebSearchTool` | 始终 `True` | 只搜索网页 |
| `SkillTool` | 始终 `True` | 只读取技能内容 |
| `FileEditTool` | 默认 `False` | 修改文件 → 需要权限 |
| `FileWriteTool` | 默认 `False` | 创建/覆盖文件 → 需要权限 |
| `BashTool` | 默认 `False` | 执行命令 → 需要权限 |

**权限决策流程**（简化版）：

```
is_read_only = True  → DEFAULT 模式自动放行，不弹窗
is_read_only = False → DEFAULT 模式需要用户确认（弹窗）
                       FULL_AUTO 模式自动放行
                       PLAN 模式直接拒绝
```

---

## 八、更多工具实现模式

### 8.1 不需要 context 的工具

```python
# WebFetchTool.execute()
async def execute(self, arguments, context):
    del context  # 明确表示不使用
    ...
```

网络工具（`web_fetch`、`web_search`）不需要 `cwd`，直接忽略 `context`。

### 8.2 通过 metadata 访问外部依赖的工具

```python
# SkillTool — 需要通过 context.cwd 加载技能
async def execute(self, arguments, context):
    registry = load_skill_registry(context.cwd)
    skill = registry.get(arguments.name)
    ...

# AgentTool — 复杂工具，需要 metadata 中的多个依赖
async def execute(self, arguments, context):
    registry = get_backend_registry()          # 全局注册表
    executor = registry.get_executor("in_process")
    result = await executor.spawn(config)
    ...
```

### 8.3 路径解析的公共模式

几乎所有文件工具都有相同的 `_resolve_path` 辅助函数：

```python
def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()   # 展开 ~
    if not path.is_absolute():
        path = base / path                # 相对路径 → 基于 cwd 的绝对路径
    return path.resolve()                 # 规范化（消除 .., symlink 等）
```

**为什么每个文件都有一份**？因为这个函数太小（4 行），抽到公共模块反而增加复杂度。

---

## 九、如何自己写一个新工具

遵循 3 步模式：

### 第 1 步：创建文件 `tools/my_tool.py`

```python
"""My custom tool."""
from __future__ import annotations
from pydantic import BaseModel, Field
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class MyToolInput(BaseModel):
    """输入参数定义"""
    query: str = Field(description="搜索关键词")
    max_results: int = Field(default=10, ge=1, le=100, description="最大结果数")


class MyTool(BaseTool):
    """我的自定义工具"""
    name = "my_tool"
    description = "做某件特定的事情。"
    input_model = MyToolInput

    def is_read_only(self, arguments: MyToolInput) -> bool:
        return True  # 如果是只读操作

    async def execute(
        self,
        arguments: MyToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        # 你的逻辑
        result = f"Found {arguments.max_results} results for '{arguments.query}'"
        return ToolResult(output=result)
```

### 第 2 步：在 `__init__.py` 中注册

```python
from openharness.tools.my_tool import MyTool

def create_default_tool_registry(mcp_manager=None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        ...
        MyTool(),        # ← 加在这里
        ...
    ):
        registry.register(tool)
    return registry
```

### 第 3 步：完成

不需要修改 `engine/query.py`、`runtime.py` 或任何其他文件 — 注册表机制会自动：
- 将工具 Schema 包含在 LLM 的 API 请求中
- 当 LLM 调用 `my_tool` 时找到正确的实例
- 执行 6 道关卡的完整流程

---

## 十、设计模式总结

| 设计模式 | 体现 | 好处 |
|---------|------|------|
| **策略模式** | `BaseTool` 抽象类 + 36 个具体实现 | 新增工具无需改引擎代码 |
| **注册表模式** | `ToolRegistry` 统一管理工具映射 | 运行时动态查找，支持 MCP 动态注册 |
| **适配器模式** | `McpToolAdapter` 包装 MCP 工具 | 对引擎层完全透明，MCP 工具和内置工具一视同仁 |
| **数据传输对象** | `ToolResult` 标准化输出格式 | 工具间结果格式统一 |
| **命令模式** | 每个工具 = 一个 `execute()` 命令 | 可以统一做权限检查、Hook 拦截等横切关注点 |
| **依赖注入** | `ToolExecutionContext.metadata` | 工具不直接依赖全局状态，可测试性好 |

---

## 十一、完整数据流图

以用户说"帮我读一下 main.py"为例：

```
LLM 决定调用工具
  │
  │ ApiMessageCompleteEvent(tool_uses=[
  │   ToolUse(name="read_file", id="toolu_abc", input={"path": "main.py"})
  │ ])
  │
  ▼
run_query() 阶段 D
  │
  ├─ yield ToolExecutionStarted(tool_name="read_file", ...)  → 前端显示 spinner
  │
  └─ _execute_tool_call(context, "read_file", "toolu_abc", {"path": "main.py"})
       │
       ├─ 关卡1: PreToolUse Hook → pass（无钩子拦截）
       │
       ├─ 关卡2: registry.get("read_file")
       │          → FileReadTool 实例 ✓
       │
       ├─ 关卡3: FileReadToolInput.model_validate({"path": "main.py"})
       │          → FileReadToolInput(path="main.py", offset=0, limit=200) ✓
       │
       ├─ 关卡4: permission_checker.evaluate("read_file", is_read_only=True)
       │          → PermissionDecision(allowed=True)  ← 只读自动放行 ✓
       │
       ├─ 关卡5: FileReadTool.execute(arguments, context)
       │     ├─ _resolve_path(cwd, "main.py") → /project/main.py
       │     ├─ path.exists() → True ✓
       │     ├─ read_bytes + decode
       │     ├─ 添加行号
       │     └─ return ToolResult(output="     1\timport os\n...")
       │
       └─ 关卡6: PostToolUse Hook → pass
       │
       ▼
  ToolResultBlock(tool_use_id="toolu_abc", content="     1\timport os\n...")
       │
       ▼
  yield ToolExecutionCompleted(tool_name="read_file", output="...", is_error=False)
       │                                                          → 前端显示工具结果
       ▼
  messages.append(ConversationMessage(role="user", content=[ToolResultBlock(...)]))
       │
       ▼
  回到阶段 A → LLM 看到文件内容 → 决定下一步
```

---

## 十二、关键要点回顾

1. **76 行定义了整个接口** — `base.py` 极其精简，只有 4 个类
2. **Pydantic 一举三得** — JSON Schema 生成（给 LLM）+ 参数验证（安全）+ 类型安全（开发体验）
3. **注册 = 唯一的修改点** — 新增工具只需写实现文件 + 在 `__init__.py` 注册一行
4. **MCP 工具对引擎透明** — 适配器模式让 MCP 工具和内置工具走完全相同的 6 道关卡
5. **错误不中断循环** — `ToolResult(is_error=True)` 反馈给 LLM，让 LLM 自行调整
6. **`is_read_only()` 是权限系统的唯一桥梁** — 只读工具自动放行，写操作需要确认

---

*阅读完本文档后，你应该能够：*
1. *理解工具系统的三层架构（抽象 → 实现 → 注册）*
2. *看懂任何一个工具的源码（都是同一个模式）*
3. *理解工具定义如何变成 LLM 的 JSON Schema*
4. *理解 MCP 工具如何通过适配器模式融入系统*
5. *自己写一个新工具并注册到系统中*

---

*下一步建议：方向 B（API 客户端）— 理解 LLM 调用的底层细节。*
