# OpenHarness 项目分析文档

> 分析时间：2026 年 4 月 5 日 | 版本：v0.1.0 | 许可证：MIT

---

## 一、项目概览

**OpenHarness** 是由香港大学数据科学实验室 (HKUDS) 开源的 **AI 编码助手基础设施（Agent Harness）**，用 Python 实现。

**核心定位**：为 LLM 提供完整的 Agent 基础设施——工具使用、技能加载、记忆持久化、安全权限和多智能体协调，让模型从"大脑"变成具备"手、眼、记忆和安全边界"的完整 Agent。

**仓库地址**：`https://github.com/HKUDS/OpenHarness`

**一句话描述**：一个命令 `oh` 启动 Agent，解锁全部 Harness 能力。

---

## 二、技术栈

### 后端（Python）

| 组件         | 技术                                  |
| ------------ | ------------------------------------- |
| 构建系统     | Hatchling                             |
| Python 版本  | >= 3.10                               |
| CLI 框架     | Typer >= 0.12                         |
| 数据建模     | Pydantic v2                           |
| LLM API      | Anthropic SDK >= 0.40、OpenAI SDK >= 1.0 |
| MCP 协议     | mcp >= 1.0.0                          |
| HTTP 客户端  | httpx >= 0.27                         |
| 终端 UI      | Textual >= 0.80、Rich >= 13.0         |
| 异步         | asyncio 原生                          |
| 配置解析     | PyYAML >= 6.0                         |
| 定时任务     | croniter >= 2.0                       |
| 文件监听     | watchfiles >= 0.20                    |
| 测试         | pytest >= 8.0 + pytest-asyncio >= 0.23 |
| 代码质量     | ruff >= 0.5、mypy >= 1.10             |

### 前端（React TUI）

| 组件       | 技术                             |
| ---------- | -------------------------------- |
| 框架       | React 18                         |
| 终端渲染   | Ink 5（React for CLI）           |
| 运行时     | tsx（Node.js 18+）               |
| 类型系统   | TypeScript 5.7                   |
| 输入组件   | ink-text-input 6                 |

---

## 三、项目目录结构

```
OpenHarness/
├── .github/                    # GitHub Actions CI 配置
├── assets/                     # 图片资源（logo、架构图、Demo GIF）
├── docs/
│   └── SHOWCASE.md             # 使用案例展示
├── frontend/
│   └── terminal/               # React/Ink TUI 前端（20 个文件）
├── scripts/                    # 9 个 E2E/集成测试脚本
├── src/
│   └── openharness/            # 核心 Python 包（169 个文件）
├── tests/                      # 66 个单元/集成测试文件
├── CHANGELOG.md                # 变更日志
├── CONTRIBUTING.md             # 贡献指南
├── LICENSE                     # MIT 许可证
├── pyproject.toml              # Python 包配置
└── README.md                   # 项目文档
```

### 核心包结构（`src/openharness/`）

```
openharness/
├── __init__.py                 # 包标识
├── __main__.py                 # python -m openharness 入口
├── cli.py                      # CLI 入口（Typer，667 行）
│
├── api/                        # LLM API 客户端层（7 个文件）
│   ├── client.py               # Anthropic API 客户端（带重试、指数退避）
│   ├── copilot_auth.py         # GitHub Copilot OAuth Device Flow 认证
│   ├── copilot_client.py       # Copilot API 客户端
│   ├── openai_client.py        # OpenAI 兼容客户端（343 行）
│   ├── provider.py             # Provider 自动检测
│   ├── errors.py               # API 错误类型
│   └── usage.py                # Token 用量快照
│
├── engine/                     # 核心 Agent 循环（6 个文件）
│   ├── query.py                # run_query() — Agent 循环核心（244 行）
│   ├── query_engine.py         # QueryEngine 类 — 管理对话历史
│   ├── messages.py             # ConversationMessage、TextBlock、ToolUseBlock
│   ├── stream_events.py        # 流事件类型定义
│   └── cost_tracker.py         # Token 成本累积
│
├── tools/                      # 42 个工具实现
│   ├── base.py                 # BaseTool 抽象类 + ToolRegistry
│   ├── bash_tool.py            # Shell 命令执行
│   ├── file_read_tool.py       # 文件读取
│   ├── file_write_tool.py      # 文件写入
│   ├── file_edit_tool.py       # 文件编辑
│   ├── glob_tool.py            # 文件模式搜索
│   ├── grep_tool.py            # 内容搜索
│   ├── web_fetch_tool.py       # 网页抓取
│   ├── web_search_tool.py      # 网络搜索
│   ├── agent_tool.py           # 子 Agent 派生
│   ├── send_message_tool.py    # Agent 间消息
│   ├── notebook_edit_tool.py   # Jupyter 笔记本编辑
│   ├── lsp_tool.py             # LSP 语言服务工具
│   ├── mcp_tool.py             # MCP 工具适配器
│   ├── cron_*_tool.py          # Cron 工具（4 个）
│   ├── task_*_tool.py          # 任务管理工具（6 个）
│   ├── team_*_tool.py          # 团队工具（2 个）
│   └── ...                     # 其他工具
│
├── commands/                   # 54 个斜杠命令
│   └── registry.py             # 命令注册表（约 65KB，1374 行）
│
├── permissions/                # 权限系统
│   ├── checker.py              # PermissionChecker（3 种模式 + 路径/命令规则）
│   └── modes.py                # PermissionMode 枚举
│
├── config/                     # 配置系统
│   ├── paths.py                # 路径解析（~/.openharness/）
│   └── settings.py             # Settings Pydantic 模型（多层配置优先级）
│
├── prompts/                    # System Prompt 构建
│   ├── system_prompt.py        # 基础 System Prompt 模板
│   ├── context.py              # build_runtime_system_prompt（组合所有上下文）
│   ├── claudemd.py             # CLAUDE.md 发现和注入
│   └── environment.py          # 环境信息检测
│
├── skills/                     # 技能系统
│   ├── bundled/content/        # 7 个内置技能（.md 格式）
│   ├── loader.py               # 技能加载器
│   ├── registry.py             # 技能注册表
│   └── types.py                # 技能类型定义
│
├── plugins/                    # 插件系统
│   ├── loader.py               # 插件发现/加载（兼容 claude-code 格式）
│   ├── installer.py            # 插件安装/卸载
│   ├── schemas.py              # PluginManifest
│   └── types.py                # LoadedPlugin
│
├── hooks/                      # 生命周期钩子
│   ├── executor.py             # 钩子执行器
│   ├── events.py               # HookEvent 枚举
│   ├── hot_reload.py           # 配置热重载
│   └── loader.py               # 钩子注册/加载
│
├── memory/                     # 持久化记忆系统
│   ├── manager.py              # 记忆条目管理
│   ├── memdir.py               # MEMORY.md 加载
│   ├── scan.py                 # 记忆文件扫描（YAML frontmatter 解析）
│   └── search.py               # 相关记忆检索（含中文分词支持）
│
├── mcp/                        # Model Context Protocol
│   ├── client.py               # MCP 客户端管理器
│   ├── config.py               # MCP 服务器配置加载
│   └── types.py                # MCP 类型定义
│
├── services/                   # 基础服务
│   ├── compact/                # 对话压缩（microcompact + LLM 摘要）
│   ├── session_storage.py      # 会话持久化/恢复
│   ├── cron.py                 # Cron 作业管理
│   ├── cron_scheduler.py       # Cron 调度守护进程
│   ├── lsp/                    # LSP 语言服务
│   ├── oauth/                  # OAuth 认证
│   └── token_estimation.py     # Token 估算
│
├── coordinator/                # 多 Agent 协调
│   ├── agent_definitions.py    # Agent 定义（约 44KB）
│   └── coordinator_mode.py     # Team 注册/管理
│
├── swarm/                      # 多 Agent Swarm 后端
│   ├── in_process.py           # 进程内 Agent（约 24KB）
│   ├── mailbox.py              # Agent 间消息邮箱（约 19KB）
│   ├── permission_sync.py      # 权限同步（约 37KB）
│   ├── registry.py             # 后端注册表
│   ├── subprocess_backend.py   # 子进程 Agent
│   ├── team_lifecycle.py       # 团队生命周期（约 28KB）
│   └── worktree.py             # Git worktree 管理
│
├── tasks/                      # 后台任务管理
│   ├── manager.py              # BackgroundTaskManager
│   ├── local_agent_task.py     # 本地 Agent 任务
│   └── local_shell_task.py     # Shell 任务
│
├── bridge/                     # 会话桥接/外部连接
├── state/                      # 应用状态管理
├── ui/                         # UI 层
│   ├── app.py                  # 入口（React TUI 启动 / 非交互模式）
│   ├── runtime.py              # RuntimeBundle + handle_line（核心运行时装配）
│   ├── backend_host.py         # 结构化后端主机（WebSocket 通信）
│   └── react_launcher.py       # React TUI 启动器
│
├── keybindings/                # 快捷键系统
├── output_styles/              # 输出样式
├── vim/                        # Vim 模式（预留）
└── voice/                      # 语音模式（预留）
```

---

## 四、核心架构分析

### 4.1 总体架构流程

```
用户输入 → CLI / React TUI → RuntimeBundle 装配
    → QueryEngine → API Client → LLM 响应
        ├─ 无工具调用 → 返回结果
        └─ 有工具调用 → Permission Check → Hook → Tool Execute → Hook → 继续循环
```

```
┌─────────────┐    WebSocket    ┌─────────────────┐
│  React TUI  │◄──────────────►│  Backend Host    │
│  (Ink/React) │                │  (Python)        │
└─────────────┘                └────────┬────────┘
                                        │
                               ┌────────▼────────┐
                               │  RuntimeBundle   │
                               │  (运行时装配中心) │
                               └────────┬────────┘
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              │                         │                          │
     ┌────────▼────────┐    ┌──────────▼──────────┐    ┌─────────▼────────┐
     │  QueryEngine     │    │  CommandRegistry     │    │  AppStateStore   │
     │  (对话引擎)      │    │  (54 个斜杠命令)     │    │  (状态管理)      │
     └────────┬────────┘    └─────────────────────┘    └──────────────────┘
              │
    ┌─────────▼──────────┐
    │  API Client Layer   │
    │  ┌─────────────────┐│
    │  │ Anthropic Client ││
    │  │ OpenAI Client   ││
    │  │ Copilot Client  ││
    │  └─────────────────┘│
    └─────────┬──────────┘
              │
    ┌─────────▼──────────┐
    │  Tool Execution     │
    │  ┌─────────────────┐│
    │  │ ToolRegistry     ││  42+ 工具
    │  │ PermissionChecker││  权限检查
    │  │ HookExecutor     ││  Pre/Post 钩子
    │  └─────────────────┘│
    └────────────────────┘
```

### 4.2 Agent Loop（核心引擎）

**文件**：`src/openharness/engine/query.py`（244 行）

这是整个系统的心脏，实现了经典的 Agent 循环模式：

```python
while True:  # 最多 max_turns 次循环
    # 1. 自动压缩检查（超阈值先 microcompact，不够再 LLM 摘要）
    messages = await auto_compact_if_needed(messages, ...)

    # 2. 调用 LLM API（流式输出）
    async for event in api_client.stream_message(request):
        yield text_delta / complete_event

    # 3. 检查是否有工具调用
    if no tool_uses: return  # 模型完成

    # 4. 执行工具（单工具顺序，多工具并发 asyncio.gather）
    for tool_call in tool_calls:
        # Pre Hook → 权限检查 → 执行 → Post Hook
        result = await _execute_tool_call(context, tool_call)

    # 5. 将结果追加到消息历史，继续循环
    messages.append(tool_results)
```

**关键特性**：
- **自动压缩（Auto-Compact）**：每次循环前检查 Token 用量，超过阈值先做 microcompact（清除旧工具结果），不够再做 LLM 摘要
- **并行工具执行**：单工具顺序执行，多工具使用 `asyncio.gather` 并发
- **最大回合限制**：超过 `max_turns`（默认 200）抛出 `MaxTurnsExceeded`
- **Pre/Post Hook**：工具执行前后触发生命周期钩子

### 4.3 QueryEngine（对话引擎）

**文件**：`src/openharness/engine/query_engine.py`（149 行）

高层对话管理器，职责：
- 管理对话历史（`_messages` 列表）
- Token 成本追踪（`CostTracker`）
- 模型 / System Prompt 热更新
- 两种主要操作：
  - `submit_message(prompt)` — 新增用户消息并执行 Agent 循环
  - `continue_pending()` — 继续中断的工具循环（不添加新消息）

### 4.4 API 客户端层

三种格式的客户端，均实现 `SupportsStreamingMessages` Protocol，可互换使用：

| 客户端 | 文件 | 协议 | 适用场景 |
|--------|------|------|----------|
| `AnthropicApiClient` | `api/client.py` (186 行) | Anthropic Messages API | Anthropic 原生、Kimi、Vertex、Bedrock |
| `OpenAICompatibleClient` | `api/openai_client.py` (343 行) | OpenAI Chat Completions | DashScope、DeepSeek、OpenAI、Groq、Ollama |
| `CopilotClient` | `api/copilot_client.py` | GitHub Copilot API | GitHub Copilot 订阅用户 |

**统一重试策略**：
- 指数退避 + 随机抖动（jitter）
- 可重试状态码：429、500、502、503、529
- 最大 3 次重试，最大延迟 30 秒

**消息格式转换**（OpenAI 客户端）：
- Anthropic `tool_use` / `tool_result` → OpenAI `tool_calls` + `role: tool` 消息
- System prompt → `role: system` 消息
- 支持 thinking models 的 `reasoning_content` 回放

### 4.5 工具系统（42+ 工具）

**文件**：`src/openharness/tools/base.py`（76 行）

抽象基类 `BaseTool`：
```python
class BaseTool(ABC):
    name: str                      # 工具名称
    description: str               # 工具描述
    input_model: type[BaseModel]   # Pydantic 输入模型

    async def execute(self, arguments, context) -> ToolResult  # 异步执行
    def is_read_only(self, arguments) -> bool                  # 是否只读
    def to_api_schema(self) -> dict                            # JSON Schema
```

`ToolRegistry` 管理所有工具实例，支持动态注册（包括 MCP 工具适配器）。

**工具分类表**：

| 分类 | 工具 | 说明 |
|------|------|------|
| 文件 I/O | Bash, FileRead, FileWrite, FileEdit, Glob, Grep | 核心文件操作 |
| 搜索 | WebFetch, WebSearch, ToolSearch, LSP | 网络和代码搜索 |
| 笔记本 | NotebookEdit | Jupyter Notebook 编辑 |
| Agent | Agent, SendMessage, TeamCreate, TeamDelete | 子 Agent 派生和协调 |
| 任务 | TaskCreate/Get/List/Update/Stop/Output | 后台任务管理 |
| MCP | McpTool, ListMcpResources, ReadMcpResource, McpAuth | MCP 协议集成 |
| 模式 | EnterPlanMode, ExitPlanMode, EnterWorktree, ExitWorktree | 工作流模式切换 |
| 调度 | CronCreate/List/Delete/Toggle, RemoteTrigger | 定时和远程执行 |
| 辅助 | Skill, Config, Brief, Sleep, AskUser, TodoWrite | 知识加载、配置、交互 |

### 4.6 权限系统

**文件**：`src/openharness/permissions/checker.py`（107 行）

三级权限模式：

| 模式 | 行为 | 使用场景 |
|------|------|----------|
| `DEFAULT` | 只读工具直接通过，写操作需用户确认 | 日常开发 |
| `FULL_AUTO` | 允许所有操作 | 沙箱环境 |
| `PLAN` | 阻止所有写操作 | 大型重构、审查优先 |

**额外规则层**：
- 工具显式允许/拒绝列表
- 路径级 glob 规则（如拒绝 `/etc/*`）
- 命令拒绝模式（如 `rm -rf /`、`DROP TABLE *`）

**检查顺序**：
1. 工具黑名单检查
2. 工具白名单检查
3. 路径规则匹配
4. 命令拒绝模式匹配
5. 权限模式判定

### 4.7 Runtime 装配

**文件**：`src/openharness/ui/runtime.py`（433 行）

`build_runtime()` 函数是整个系统的装配中心，按顺序构建：

1. 加载设置（配置文件 + 环境变量 + CLI 覆盖）
2. 加载插件
3. 创建 API 客户端（根据 `api_format` 选择 Anthropic / OpenAI / Copilot）
4. 创建 MCP 管理器并连接所有服务器
5. 创建工具注册表（含 MCP 工具适配器）
6. 创建权限检查器
7. 创建 Hook 执行器（支持热重载）
8. 创建 QueryEngine
9. 创建命令注册表
10. 返回 `RuntimeBundle`

`handle_line()` 是消息处理核心：
- 先检查是否为斜杠命令
- 命令则交给 CommandRegistry 处理
- 否则交给 QueryEngine 的 Agent 循环
- 每轮结束后自动保存会话快照

### 4.8 System Prompt 构建

**文件**：`src/openharness/prompts/context.py`（102 行）

`build_runtime_system_prompt()` 组合多个上下文源：

```
1. 基础 System Prompt（角色定义 + 行为准则 + 工具使用指南）
2. 环境信息（OS、Git、Python、工作目录等）
3. Fast Mode / Effort / Passes 设置
4. 可用技能列表
5. CLAUDE.md 项目指令
6. Issue / PR Comments 上下文
7. MEMORY.md 持久化记忆
8. 基于用户输入的相关记忆检索
```

### 4.9 配置系统

**文件**：`src/openharness/config/settings.py`（184 行）

**优先级（从高到低）**：
1. CLI 参数
2. 环境变量（`ANTHROPIC_API_KEY`、`OPENHARNESS_MODEL`、`OPENHARNESS_API_FORMAT` 等）
3. 配置文件（`~/.openharness/settings.json`）
4. 默认值

**关键默认配置**：
| 配置项 | 默认值 |
|--------|--------|
| model | `claude-sonnet-4-20250514` |
| max_tokens | 16384 |
| max_turns | 200 |
| api_format | `anthropic` |
| permission.mode | `DEFAULT` |
| effort | `medium` |

### 4.10 前端架构

**架构**：React/Ink TUI 通过 WebSocket 与 Python 后端通信。

| 层 | 文件 | 职责 |
|----|------|------|
| Python 后端 | `ui/backend_host.py` | 接收请求：submit_line、permission_response、shutdown 等 |
| React 前端 | `App.tsx` (397 行) | 渲染对话、命令选择器、权限对话框、状态栏 |
| 通信钩子 | `useBackendSession.ts` | 管理 WebSocket 连接和状态 |

**前端组件**（13 个）：
| 组件 | 功能 |
|------|------|
| `App.tsx` | 主应用，包含状态管理、命令处理、脚本自动化 |
| `ConversationView` | 对话视图 |
| `PromptInput` | 输入框 |
| `CommandPicker` | 斜杠命令选择器 |
| `ModalHost` | 模态对话框（权限确认、问题回答） |
| `SelectModal` | 选择模态（权限模式切换、会话恢复） |
| `StatusBar` | 状态栏 |
| `Spinner` | 加载动画 |
| `ToolCallDisplay` | 工具调用展示 |
| `WelcomeBanner` | 欢迎横幅 |
| `SidePanel` | 侧边面板 |
| `Footer` | 底栏 |
| `Composer` | 输入编辑器 |

---

## 五、CLI 命令体系

### 主命令 `oh` / `openharness`

```toml
[project.scripts]
openharness = "openharness.cli:app"
oh = "openharness.cli:app"
```

### CLI 参数分组

| 分组 | 参数 | 说明 |
|------|------|------|
| Session | `-c/--continue`, `-r/--resume`, `-n/--name` | 会话恢复与命名 |
| Model | `-m/--model`, `--effort`, `--max-turns`, `--verbose` | 模型与性能配置 |
| Output | `-p/--print`, `--output-format (text/json/stream-json)` | 输出格式 |
| Permissions | `--permission-mode`, `--dangerously-skip-permissions`, `--allowed-tools`, `--disallowed-tools` | 权限控制 |
| Context | `-s/--system-prompt`, `--append-system-prompt`, `--settings`, `--base-url`, `--api-key`, `--bare`, `--api-format` | 上下文配置 |
| Advanced | `-d/--debug`, `--mcp-config`, `--cwd`, `--backend-only` | 高级选项 |

### 子命令

| 子命令 | 功能 |
|--------|------|
| `oh mcp list/add/remove` | MCP 服务器管理 |
| `oh plugin list/install/uninstall` | 插件管理 |
| `oh auth status/login/logout` | API Key 认证管理 |
| `oh auth copilot-login/copilot-logout` | GitHub Copilot OAuth 认证 |
| `oh cron start/stop/status/list/toggle/history/logs` | Cron 调度管理 |

### 内置斜杠命令（54 个）

命令注册在 `commands/registry.py`（约 65KB），覆盖：
- 会话管理（/clear、/resume、/continue、/save、/export）
- 模型控制（/model、/effort、/compact）
- 权限管理（/permissions、/plan）
- 工具管理（/tools、/mcp）
- 记忆管理（/memory）
- 技能管理（/skills）
- 插件管理（/plugins）
- 帮助系统（/help、/status、/version）

---

## 六、Provider 兼容性

### Anthropic 格式（默认）

| Provider | 检测信号 |
|----------|---------|
| Anthropic（原生） | 默认（无自定义 base_url） |
| Moonshot / Kimi | base_url 含 `moonshot` 或模型以 `kimi` 开头 |
| Vertex 兼容 | base_url 含 `vertex` 或 `aiplatform` |
| Bedrock 兼容 | base_url 含 `bedrock` |
| 通用 Anthropic 兼容 | 其他自定义 base_url |

### OpenAI 格式（`--api-format openai`）

| Provider | Base URL | 示例模型 |
|----------|----------|----------|
| 阿里 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen3.5-flash, qwen3-max |
| DeepSeek | `https://api.deepseek.com` | deepseek-chat, deepseek-reasoner |
| OpenAI | `https://api.openai.com/v1` | gpt-4o, gpt-4o-mini |
| GitHub Models | `https://models.inference.ai.azure.com` | gpt-4o |
| SiliconFlow | `https://api.siliconflow.cn/v1` | DeepSeek-V3 |
| Groq | `https://api.groq.com/openai/v1` | llama-3.3-70b-versatile |
| Ollama（本地） | `http://localhost:11434/v1` | 任意本地模型 |

### Copilot 格式（`--api-format copilot`）

- GitHub OAuth Device Flow 认证（无需 API Key）
- 自动刷新短期会话 Token
- 支持 GitHub Enterprise

---

## 七、子系统详解

### 7.1 技能系统（Skills）

**7 个内置技能**：

| 技能 | 功能 |
|------|------|
| commit | 创建规范的 Git 提交 |
| review | 代码审查（Bug、安全、质量） |
| debug | 系统化诊断和修复 Bug |
| plan | 编码前设计实现计划 |
| test | 编写和运行测试 |
| simplify | 重构代码使其更简洁 |
| diagnose | 诊断 Agent 运行失败和回归 |

技能以 `.md` 文件存储，兼容 [anthropics/skills](https://github.com/anthropics/skills) 格式。用户可将自定义技能放到 `~/.openharness/skills/` 目录。

### 7.2 插件系统（Plugins）

兼容 [claude-code plugins](https://github.com/anthropics/claude-code/tree/main/plugins) 格式，插件可包含：
- **Commands** — 自定义斜杠命令（`.md` 文件）
- **Hooks** — 生命周期钩子（`hooks.json`）
- **Agents** — 自定义 Agent 定义（`.md` 文件）
- **MCP Servers** — MCP 服务器配置

### 7.3 记忆系统（Memory）

- **MEMORY.md**：项目级持久化记忆入口
- **记忆扫描**：解析 YAML frontmatter（name、description、type）
- **相关检索**：基于用户输入做关键词匹配，支持中文分词
- **自动注入**：相关记忆会被注入到 System Prompt 中

### 7.4 MCP 协议支持

- 支持 Stdio / HTTP / WebSocket 三种传输方式
- `McpClientManager` 管理多个 MCP 服务器连接
- MCP 工具通过 `McpToolAdapter` 自动适配为 OpenHarness 工具
- 支持 MCP 资源浏览（`ListMcpResources`、`ReadMcpResource`）

### 7.5 多 Agent 协调（Swarm）

- **子 Agent 派生**：`AgentTool` 支持创建独立的子 Agent
- **团队管理**：`TeamCreate/Delete` 工具管理 Agent 团队
- **消息邮箱**：`TeammateMailbox` 实现 Agent 间异步通信
- **权限同步**：`permission_sync.py` 确保子 Agent 的权限与主 Agent 一致
- **Git Worktree**：支持为子 Agent 创建独立的工作树
- **后台任务**：`BackgroundTaskManager` 管理 Agent/Shell 后台任务

### 7.6 对话压缩（Compact）

**文件**：`src/openharness/services/compact/__init__.py`（493 行）

两级压缩策略：
1. **Microcompact**：清除旧工具结果内容（cheap 操作），保留最近 5 条
2. **Full Compact**：调用 LLM 生成结构化摘要（expensive 操作）

自动触发：每轮 Agent 循环前检查 Token 用量，超过阈值自动执行。

### 7.7 生命周期钩子（Hooks）

支持的钩子事件：
- `PreToolUse` — 工具执行前（可阻止执行）
- `PostToolUse` — 工具执行后
- `SessionStart` — 会话开始
- `SessionEnd` — 会话结束

支持热重载：配置文件变更后自动更新钩子注册表。

---

## 八、数据流分析

### 8.1 交互模式数据流

```
用户输入
    ↓
React TUI (Ink)
    ↓ WebSocket
Backend Host
    ↓
handle_line()
    ├─ 斜杠命令 → CommandRegistry → CommandResult → 渲染
    └─ 普通消息 → QueryEngine.submit_message()
                    ↓
                 run_query() ─── Agent Loop ───
                    │                          │
                    ├─ auto_compact_if_needed() │
                    ├─ api_client.stream_message()
                    │       ↓                  │
                    │  AssistantTextDelta ──→ 前端
                    │       ↓                  │
                    │  tool_uses detected       │
                    │       ↓                  │
                    ├─ PreToolUse Hook         │
                    ├─ PermissionChecker       │
                    ├─ tool.execute()          │
                    ├─ PostToolUse Hook        │
                    └─ append results ─────────┘
                    ↓
              save_session_snapshot()
              sync_app_state()
```

### 8.2 非交互模式数据流

```
oh -p "prompt" --output-format text|json|stream-json
    ↓
run_print_mode()
    ↓
build_runtime() → start_runtime()
    ↓
handle_line(prompt)
    ↓
stream events → stdout (text / JSON / stream-JSON)
    ↓
close_runtime()
```

---

## 九、测试覆盖

### 单元/集成测试（tests/ 目录）

| 测试模块 | 文件数 | 关键覆盖 |
|----------|--------|----------|
| test_api/ | 4 | Anthropic/OpenAI/Copilot 客户端、OAuth 认证 |
| test_commands/ | 3 | 命令注册表、CLI、命令流程 |
| test_config/ | 2 | 路径解析、设置加载 |
| test_coordinator/ | 3 | Agent 定义、协调器模式 |
| test_engine/ | 1 | 查询引擎 |
| test_hooks/ | 1 | 钩子执行器 |
| test_mcp/ | 2 | MCP 集成、Stdio 流程 |
| test_memory/ | 1 | 记忆目录 |
| test_permissions/ | 1 | 权限检查器 |
| test_plugins/ | 2 | 插件加载、生命周期 |
| test_prompts/ | 3 | CLAUDE.md、环境信息、System Prompt |
| test_services/ | 4 | 压缩、Cron、调度、会话存储 |
| test_skills/ | 1 | 技能加载器 |
| test_swarm/ | 7 | 邮箱、权限同步、Worktree、团队生命周期 |
| test_tasks/ | 1 | 任务管理器 |
| test_tools/ | 5 | 核心工具、集成流程、MCP Auth、Web Fetch |
| test_ui/ | 4 | 模式、React 后端、启动器、Textual App |
| 集成测试 | 4 | 钩子/技能/插件真实测试、大型任务、PR 自动化 |

### E2E 测试（scripts/ 目录）

| 测试脚本 | 数量 | 说明 |
|----------|------|------|
| e2e_smoke.py | - | 端到端烟雾测试（34KB） |
| test_cli_flags.py | 6 | CLI 参数 E2E（真实模型调用） |
| test_harness_features.py | 9 | 重试、技能、并行、权限 |
| react_tui_e2e.py | 3 | 欢迎、对话、状态 |
| test_tui_interactions.py | 4 | 命令、权限、快捷键 |
| test_real_skills_plugins.py | 12 | anthropics/skills + claude-code/plugins |

### 测试结果汇总

| 套件 | 测试数 | 状态 |
|------|--------|------|
| 单元 + 集成 | 114 | ✅ 全部通过 |
| CLI Flags E2E | 6 | ✅ 真实模型调用 |
| Harness Features E2E | 9 | ✅ 重试、技能、并行、权限 |
| React TUI E2E | 3 | ✅ 欢迎、对话、状态 |
| TUI Interactions E2E | 4 | ✅ 命令、权限、快捷键 |
| Real Skills + Plugins | 12 | ✅ anthropics/skills + claude-code/plugins |

---

## 十、关键设计模式

### 10.1 延迟导入（Lazy Loading）

所有子模块的 `__init__.py` 使用 `__getattr__` 实现延迟导入，显著减少启动时间：

```python
def __getattr__(name: str):
    if name == "QueryEngine":
        from openharness.engine.query_engine import QueryEngine
        return QueryEngine
    raise AttributeError(name)
```

### 10.2 Protocol 模式

`SupportsStreamingMessages` Protocol 使三种 API 客户端可互换，无需继承：

```python
class SupportsStreamingMessages(Protocol):
    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        ...
```

### 10.3 Pydantic 全面验证

- Settings 模型：多层配置合并
- 工具输入：每个工具定义 `input_model`，自动验证参数
- 消息模型：`ConversationMessage`、`ContentBlock` 等

### 10.4 事件驱动流式输出

```python
StreamEvent = (
    AssistantTextDelta          # 增量文本
    | AssistantTurnComplete     # 回合完成
    | ToolExecutionStarted      # 工具开始执行
    | ToolExecutionCompleted    # 工具执行完成
)
```

### 10.5 多层配置合并

优先级：CLI 参数 > 环境变量 > 配置文件 > 默认值

```python
settings = load_settings().merge_cli_overrides(model=model, ...)
```

### 10.6 插件兼容性

- 兼容 `claude-code` 插件格式
- 兼容 `anthropics/skills` 技能格式
- 统一的加载器和注册机制

---

## 十一、代码量统计

| 模块 | Python 文件数 | 估算行数 | 说明 |
|------|-------------|---------|------|
| api/ | 7 | ~850 | API 客户端层 |
| engine/ | 6 | ~650 | 核心 Agent 循环 |
| tools/ | 42 | ~2,800 | 工具实现 |
| commands/ | 2 | ~1,400 | 斜杠命令 |
| permissions/ | 3 | ~170 | 权限系统 |
| config/ | 3 | ~350 | 配置系统 |
| prompts/ | 4 | ~400 | Prompt 构建 |
| skills/ | 5 | ~200 | 技能系统 |
| plugins/ | 5 | ~350 | 插件系统 |
| hooks/ | 6 | ~400 | 生命周期钩子 |
| memory/ | 6 | ~300 | 记忆系统 |
| mcp/ | 4 | ~350 | MCP 协议 |
| services/ | 7 | ~1,200 | 基础服务 |
| coordinator/ | 3 | ~1,000 | 多 Agent 协调 |
| swarm/ | 9 | ~3,500 | Swarm 后端 |
| tasks/ | 5 | ~350 | 任务管理 |
| ui/ | 8 | ~1,200 | UI 层 |
| 其他 | 7 | ~200 | 状态、快捷键、样式、Vim、语音 |
| **后端合计** | **~132** | **~15,700** | |
| 前端 | 15 (.tsx) + 2 (.ts) | ~1,200 | React TUI |
| 测试 | 66 | ~8,500 | 单元/集成/E2E |
| **总计** | **~215** | **~25,400** | |

---

## 十二、项目亮点与不足

### 亮点

1. **架构清晰**：10 个子系统职责分明，模块间通过 Protocol 和注册表松耦合
2. **多 Provider 支持**：一套代码同时支持 Anthropic、OpenAI、Copilot 三种 API 格式
3. **完善的权限体系**：三级权限模式 + 路径规则 + 命令拒绝模式
4. **延迟导入优化**：所有模块使用 `__getattr__` 延迟加载，启动速度快
5. **对话压缩**：两级压缩策略保证长对话不会超出上下文窗口
6. **生态兼容**：兼容 claude-code 插件和 anthropics/skills 格式
7. **测试覆盖完整**：114 个单元测试 + 6 套 E2E 测试

### 可改进方向

1. **Voice 模式**：`voice/` 目录已有框架但功能尚未实现
2. **Vim 模式**：`vim/` 目录仅有占位代码
3. **commands/registry.py 过大**：单文件 65KB/1374 行，建议拆分
4. **coordinator/agent_definitions.py**：44KB 单文件，可按 Agent 类型拆分
5. **swarm/ 模块**：permission_sync.py (37KB) 和 team_lifecycle.py (28KB) 较大
6. **类型检查**：mypy strict 模式尚未作为 CI 必检项
7. **文档**：docs/ 目录仅有 SHOWCASE.md，缺少架构详解和 API 文档

---

## 十三、快速开始指南

```bash
# 克隆并安装
git clone https://github.com/HKUDS/OpenHarness.git
cd OpenHarness
uv sync --extra dev

# 配置 API Key（三选一）
export ANTHROPIC_API_KEY=your_key                    # Anthropic
export OPENHARNESS_API_FORMAT=openai                 # OpenAI 兼容
export OPENHARNESS_API_FORMAT=copilot && oh auth copilot-login  # Copilot

# 启动交互模式
oh                     # 如果 venv 已激活
uv run oh              # 不激活 venv

# 非交互模式
oh -p "Explain this codebase"                         # 文本输出
oh -p "List all functions" --output-format json        # JSON 输出
oh -p "Fix the bug" --output-format stream-json        # 流式 JSON

# 运行测试
uv run pytest -q                                      # 114 个单元/集成测试
python scripts/test_harness_features.py               # Harness E2E
```

---

*本文档由对项目源代码的全面分析生成，涵盖了架构设计、模块分析、数据流、测试覆盖等方面。*
