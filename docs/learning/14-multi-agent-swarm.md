# 14 — 多 Agent 协作（Swarm）：Leader-Worker 分布式编排系统

> 涉及源文件：`swarm/` (10个文件, ~2900行) · `coordinator/` (3个文件, ~1500行) · `tasks/` (6个文件, ~400行) · `tools/agent_tool.py` (98行) · `tools/send_message_tool.py` (63行) · `tools/task_*_tool.py` (6个, ~220行) · `tools/team_*_tool.py` (2个, ~60行) · `tools/enter_worktree_tool.py` (81行) · `tools/exit_worktree_tool.py` (43行)
>
> 预计阅读时间：50 分钟
>
> 前置知识：已理解 Agent 循环（06）、工具系统（10-11）、API 客户端（12）、权限系统（13）

---

## 本章核心问题

当一个任务太复杂（如"重构整个认证模块"），单个 Agent 循环不够用时——怎么让多个 Agent **同时工作、互相通信、协调权限**？

---

## 一、全景架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                    用户 / Coordinator Agent                       │
│  使用 3 个核心工具：agent（生成）、send_message（通信）、         │
│                    task_stop（终止）                               │
└───────────────┬────────────────┬───────────────┬────────────────┘
                │                │               │
         ┌──────▼──────┐  ┌─────▼─────┐  ┌──────▼──────┐
         │  AgentTool   │  │SendMessage│  │  TaskStop   │
         │  agent_tool  │  │  Tool     │  │   Tool      │
         └──────┬──────┘  └─────┬─────┘  └──────┬──────┘
                │               │                │
         ┌──────▼───────────────▼────────────────▼────────────────┐
         │              BackendRegistry (单例)                      │
         │   自动检测最佳后端：in_process > tmux > subprocess       │
         └──────┬───────────────┬──────────────────┬──────────────┘
                │               │                  │
    ┌───────────▼───┐   ┌──────▼──────┐   ┌───────▼───────┐
    │ InProcess     │   │ Subprocess  │   │ Tmux/iTerm2   │
    │ Backend       │   │ Backend     │   │ (可扩展)       │
    │               │   │             │   │               │
    │ asyncio.Task  │   │ 子进程      │   │ 终端 pane     │
    │ + ContextVar  │   │ + stdin/out │   │ + 文件 mailbox│
    └───────┬───────┘   └──────┬──────┘   └───────────────┘
            │                  │
            ▼                  ▼
    ┌──────────────────────────────────────┐
    │          run_query() 循环             │
    │   (每个 Agent 复用同一个引擎核心)     │
    │   LLM 调用 → 工具执行 → 下一轮...    │
    └──────────────────────────────────────┘

通信基础设施：
    ┌──────────────────────────────────────┐
    │  TeammateMailbox (文件邮箱系统)       │
    │  ~/.openharness/teams/<team>/        │
    │    agents/<agent>/inbox/*.json       │
    │  7 种消息类型 + 原子写入 + fcntl 锁  │
    └──────────────────────────────────────┘

    ┌──────────────────────────────────────┐
    │  TeamLifecycleManager (团队持久化)    │
    │  ~/.openharness/teams/<team>/        │
    │    team.json (成员、权限路径、状态)   │
    └──────────────────────────────────────┘

    ┌──────────────────────────────────────┐
    │  WorktreeManager (Git 隔离)          │
    │  ~/.openharness/worktrees/<slug>/    │
    │  每个 Agent 独立的 git worktree       │
    └──────────────────────────────────────┘
```

---

## 二、5 层架构逐层剖析

### 第 1 层：Coordinator 协调器——"总指挥"

#### 2.1 7 个内置 Agent 定义

```python
# coordinator/agent_definitions.py — 7 个内置 Agent
_BUILTIN_AGENTS = [
    AgentDefinition(name="general-purpose", tools=["*"], ...),         # 通用型
    AgentDefinition(name="Explore",                                    # 只读探索
        disallowed_tools=["file_edit", "file_write", ...],
        model="haiku"),
    AgentDefinition(name="Plan",                                       # 只读规划
        disallowed_tools=["file_edit", "file_write", ...]),
    AgentDefinition(name="worker", tools=None, ...),                   # 实现型
    AgentDefinition(name="verification",                               # 验证型
        disallowed_tools=["file_edit", "file_write", ...],
        background=True, color="red"),
    AgentDefinition(name="statusline-setup", tools=["Read", "Edit"]),  # 配置型
    AgentDefinition(name="claude-code-guide",                          # 指南型
        tools=["Glob", "Grep", "Read", "WebFetch", "WebSearch"],
        permission_mode="dontAsk"),
]
```

**关键设计**：每个 Agent 通过 `tools` / `disallowed_tools` 限制能力范围——Explore 不能写，verification 不能改，guide 只能搜。

#### 2.2 Agent 定义加载的三级覆盖

```python
# coordinator/agent_definitions.py:905-945
def get_all_agent_definitions():
    agent_map = {}
    # 1. 内置 Agent（最低优先级）
    for agent in get_builtin_agent_definitions():
        agent_map[agent.name] = agent
    # 2. 用户自定义 (~/.openharness/agents/*.md)
    for agent in load_agents_dir(_get_user_agents_dir()):
        agent_map[agent.name] = agent          # 同名覆盖
    # 3. 插件 Agent（最高优先级）
    for plugin in load_plugins(settings, cwd):
        for agent_def in plugin.agents:
            agent_map[agent_def.name] = agent_def
    return list(agent_map.values())
```

用户可以用 `.md` + YAML frontmatter 自定义 Agent：

```markdown
---
name: my-reviewer
description: Code review specialist
tools: Read, Glob, Grep
model: haiku
permissionMode: dontAsk
color: purple
---
You are a code review agent. Focus on finding bugs, security issues, and code smell.
```

#### 2.3 Coordinator 系统提示词

```python
# coordinator/coordinator_mode.py:267-519 — 超长的协调者系统提示词
"""You are Claude Code, an AI assistant that orchestrates software engineering tasks.

## Your Tools
- agent      — Spawn a new worker
- send_message — Continue an existing worker
- task_stop  — Stop a running worker

## Task Workflow
| Phase          | Who         | Purpose                      |
|----------------|-------------|------------------------------|
| Research       | Workers     | 并行调查代码库               |
| Synthesis      | Coordinator | 你综合研究结果，制定实施方案   |
| Implementation | Workers     | 按方案做具体修改              |
| Verification   | Workers     | 测试验证修改是否正确          |

## Writing Worker Prompts
Workers can't see your conversation. Every prompt must be self-contained.
Never write "based on your findings" — synthesize the findings yourself."""
```

**核心哲学**：Coordinator 不写代码，只做**综合和决策**。Worker 做具体的搜索、编码、测试。

---

### 第 2 层：Tools 工具层——LLM 的操控接口

#### 2.4 AgentTool — 生成 Worker

```python
# tools/agent_tool.py:43-97
async def execute(self, arguments, context):
    # 1. 查找 Agent 定义
    agent_def = get_agent_definition(arguments.subagent_type)

    # 2. 选择执行后端（优先 in_process，其次 subprocess）
    registry = get_backend_registry()
    executor = registry.get_executor("in_process")   # 或 fallback

    # 3. 构建 spawn 配置
    config = TeammateSpawnConfig(
        name=agent_name,
        team=team,
        prompt=arguments.prompt,           # Coordinator 写的具体任务描述
        cwd=str(context.cwd),
        model=agent_def.model,             # 继承 Agent 定义的模型
        system_prompt=agent_def.system_prompt,
    )

    # 4. 生成！
    result = await executor.spawn(config)
    return ToolResult(output=f"Spawned agent {result.agent_id} (task_id={result.task_id})")
```

#### 2.5 SendMessageTool — 与 Worker 通信

```python
# tools/send_message_tool.py:31-62
async def execute(self, arguments, context):
    if "@" in arguments.task_id:
        # Swarm Agent 格式: "worker@myteam"
        return await self._send_swarm_message(arguments.task_id, arguments.message)
    else:
        # 普通 Task 格式: 直接写 stdin
        await get_task_manager().write_to_task(arguments.task_id, arguments.message)

async def _send_swarm_message(self, agent_id, message):
    executor = get_backend_registry().get_executor("in_process")
    await executor.send_message(agent_id, TeammateMessage(text=message, from_agent="coordinator"))
```

**两种路由**：`@` 符号区分 Swarm Agent（邮箱通信）和普通 Task（stdin 管道通信）。

---

### 第 3 层：Swarm 核心层——后端抽象与通信

#### 2.6 TeammateExecutor Protocol — 后端接口

```python
# swarm/types.py:351-382
@runtime_checkable
class TeammateExecutor(Protocol):
    type: BackendType

    def is_available(self) -> bool: ...
    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult: ...
    async def send_message(self, agent_id: str, message: TeammateMessage) -> None: ...
    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool: ...
```

又是 Protocol 模式！和 `SupportsStreamingMessages` 一样的设计——只看方法签名，不要求继承。

#### 2.7 BackendRegistry — 后端选择

```python
# swarm/registry.py:128-183
def detect_backend(self) -> BackendType:
    # 优先级 1: in_process（POSIX 平台，asyncio Task，零外部依赖）
    if self._in_process_fallback_active:
        return "in_process"
    # 优先级 2: tmux（在 tmux 会话内，可视化调试）
    if _detect_tmux() and "tmux" in self._backends:
        return "tmux"
    # 优先级 3: subprocess（永远可用，安全 fallback）
    return "subprocess"
```

#### 2.8 四种后端对比

| 后端 | 进程模型 | 通信方式 | 隔离机制 | 适用场景 |
|------|---------|---------|---------|---------|
| **in_process** | asyncio.Task（同进程） | ContextVar + 文件邮箱 | ContextVar 任务隔离 | 默认首选（POSIX） |
| **subprocess** | 独立子进程 | stdin/stdout JSON | 进程隔离 | 通用 fallback |
| **tmux** | tmux pane | 文件邮箱 | pane 隔离 | 可视化调试 |
| **iterm2** | iTerm2 tab | 文件邮箱 | tab 隔离 | macOS 可视化 |

---

### 第 4 层：InProcessBackend 深度剖析——最核心的后端实现

#### 2.9 ContextVar 隔离——Python 版 AsyncLocalStorage

```python
# swarm/in_process.py:173-188
_teammate_context_var: ContextVar[TeammateContext | None] = ContextVar(
    "_teammate_context_var", default=None
)

def get_teammate_context() -> TeammateContext | None:
    return _teammate_context_var.get()

def set_teammate_context(ctx: TeammateContext) -> None:
    _teammate_context_var.set(ctx)
```

**为什么用 ContextVar？** 当多个 Agent 在同一个进程中以 asyncio Task 并发运行时，每个 Task 需要知道自己的身份（agent_id、team、abort 信号等）。`ContextVar` 就像 Go 的 goroutine-local storage 或 Node.js 的 `AsyncLocalStorage`——每个 Task 看到自己独立的上下文副本。

#### 2.10 双信号取消机制

```python
# swarm/in_process.py:52-102
class TeammateAbortController:
    def __init__(self):
        self.cancel_event = asyncio.Event()    # 优雅取消（完成当前工具后退出）
        self.force_cancel = asyncio.Event()    # 强制取消（立即取消 Task）

    def request_cancel(self, reason=None, *, force=False):
        if force:
            self.force_cancel.set()
            self.cancel_event.set()    # 两个都设
        else:
            self.cancel_event.set()    # 只设优雅取消
```

**为什么需要两级？** 想象 Worker 正在执行 `bash: pytest -x`（可能运行 5 分钟）：
- **优雅取消**：等 pytest 跑完，然后退出
- **强制取消**：立即杀掉，不管 pytest 跑到哪

#### 2.11 Agent 执行循环

```python
# swarm/in_process.py:196-292
async def start_in_process_teammate(*, config, agent_id, abort_controller, query_context=None):
    # 1. 绑定上下文（ContextVar）
    ctx = TeammateContext(agent_id=agent_id, agent_name=config.name, ...)
    set_teammate_context(ctx)

    # 2. 创建邮箱
    mailbox = TeammateMailbox(team_name=config.team, agent_id=agent_id)

    try:
        ctx.status = "running"
        if query_context:
            await _run_query_loop(query_context, config, ctx, mailbox)  # 真正的 Agent 循环
        else:
            # Stub 模式（无 QueryContext 时的占位运行）
            ...
    finally:
        ctx.status = "stopped"
        # 通知 Leader: "我完成了"
        idle_msg = create_idle_notification(sender=agent_id, recipient="leader", ...)
        await leader_mailbox.write(idle_msg)
```

#### 2.12 邮箱轮询与消息注入

```python
# swarm/in_process.py:335-395
async def _run_query_loop(query_context, config, ctx, mailbox):
    messages = [ConversationMessage.from_user_text(config.prompt)]   # 初始提示

    async for event, usage in run_query(query_context, messages):    # 复用核心 Agent 循环！
        # 追踪 token 用量
        if usage: ctx.total_tokens += usage.input_tokens + usage.output_tokens

        # 检查取消信号
        if ctx.abort_controller.is_cancelled:
            return

        # 轮询邮箱——处理 shutdown 请求和新消息
        should_stop = await _drain_mailbox(mailbox, ctx)
        if should_stop:
            return

        # 注入排队的消息为新的 user turn
        while not ctx.message_queue.empty():
            queued = ctx.message_queue.get_nowait()
            messages.append(ConversationMessage(role="user", content=queued.text))

    ctx.status = "idle"
```

**关键洞察**：每个 Agent 内部运行的是**完全相同的 `run_query()` 循环**——和主 Agent 用的是同一个引擎核心！区别只在于：
- 初始消息来自 Coordinator 的 prompt（而非用户输入）
- 每轮之间会检查邮箱（而非等用户输入）
- 有双信号取消机制（而非 MaxTurnsExceeded）

---

### 第 5 层：通信基础设施

#### 2.13 文件邮箱系统

```
~/.openharness/teams/myteam/
    agents/
        worker1/
            inbox/
                1712345678.123456_uuid-1.json     # 消息文件
                1712345679.654321_uuid-2.json
                .write_lock                        # POSIX 文件锁
        leader/
            inbox/
                ...
    permissions/
        pending/                                   # 权限请求（Worker → Leader）
        resolved/                                  # 权限决策（Leader → Worker）
    team.json                                      # 团队元数据
```

```python
# swarm/mailbox.py:122-153 — 原子写入
async def write(self, msg: MailboxMessage):
    filename = f"{msg.timestamp:.6f}_{msg.id}.json"
    tmp_path = inbox / f"{filename}.tmp"
    final_path = inbox / filename

    def _write_atomic():
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)     # 加排他锁
            try:
                tmp_path.write_text(payload)                     # 先写临时文件
                os.rename(tmp_path, final_path)                  # 原子重命名
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # 释放锁

    await loop.run_in_executor(None, _write_atomic)  # 在线程池执行（不阻塞事件循环）
```

**三重保证**：
1. **原子性**：`.tmp` + `os.rename()` → 读取方永远不会看到半写的文件
2. **并发安全**：`fcntl.LOCK_EX` 排他锁 → 多个 Writer 不会冲突
3. **非阻塞**：`run_in_executor` → 文件 I/O 不冻结 asyncio 事件循环

#### 2.14 七种消息类型

```python
# swarm/mailbox.py:26-34
MessageType = Literal[
    "user_message",                  # Coordinator → Worker 的指令
    "permission_request",            # Worker → Leader 的权限请求
    "permission_response",           # Leader → Worker 的权限决策
    "sandbox_permission_request",    # Worker → Leader 的沙箱网络权限
    "sandbox_permission_response",   # Leader → Worker 的沙箱权限决策
    "shutdown",                      # Leader → Worker 的关机指令
    "idle_notification",             # Worker → Leader 的"我完成了"通知
]
```

---

## 三、完整数据流——一次多 Agent 协作的旅程

```
用户: "重构认证模块，增加 JWT 支持"
    │
    ▼
Coordinator Agent (run_query 循环)
    │
    ├── LLM 决策: "需要先研究，再实现，最后验证"
    │
    ├── 工具调用: agent(description="调查认证代码", subagent_type="Explore", prompt="...")
    │       │
    │       ▼ AgentTool.execute()
    │           → BackendRegistry.get_executor("in_process")
    │           → InProcessBackend.spawn(config)
    │               → asyncio.create_task(start_in_process_teammate)
    │               → 返回 SpawnResult(agent_id="Explore@default", task_id="t_abc123")
    │
    ├── 工具调用: agent(description="研究 JWT 最佳实践", subagent_type="worker", prompt="...")
    │       → 同上，另一个 asyncio.Task
    │
    │   (两个 Worker 并行运行，各自有自己的 run_query 循环)
    │
    │   Worker "Explore@default":
    │       → run_query: LLM → Glob → Grep → Read → 报告结果
    │       → idle_notification → Leader 邮箱
    │
    │   Coordinator 收到 <task-notification> (XML 格式):
    │       <task-id>Explore@default</task-id>
    │       <status>completed</status>
    │       <result>Found auth module at src/auth/... 42 files involved...</result>
    │
    ├── Coordinator 综合研究结果，制定实施方案
    │
    ├── 工具调用: send_message(to="Explore@default", message="Fix the null pointer in...")
    │       │
    │       ▼ SendMessageTool.execute()
    │           → InProcessBackend.send_message()
    │               → TeammateMailbox.write(user_message)
    │               → Worker 的 _drain_mailbox() 读到消息
    │               → 注入为新的 user turn
    │               → Worker 继续执行 run_query
    │
    ├── Worker 完成实现，提交 commit
    │
    ├── 工具调用: agent(subagent_type="verification", prompt="Verify the JWT changes...")
    │       → 验证 Agent 运行测试、检查类型
    │       → 返回 VERDICT: PASS
    │
    └── Coordinator: "JWT 支持已实现并验证通过。"
```

---

## 四、SubprocessBackend——子进程模式

```python
# swarm/subprocess_backend.py:47-94
async def spawn(self, config):
    # 1. 构建继承的 CLI 参数和环境变量
    flags = build_inherited_cli_flags(model=config.model, ...)
    extra_env = build_inherited_env_vars()   # ANTHROPIC_API_KEY, HTTPS_PROXY 等

    # 2. 通过 BackgroundTaskManager 创建子进程
    record = await manager.create_agent_task(
        prompt=config.prompt,                # 初始任务通过 stdin 发送
        command=f"{teammate_cmd} -m openharness {flags}",
        task_type="in_process_teammate",
    )
```

```python
# swarm/subprocess_backend.py:96-118
async def send_message(self, agent_id, message):
    # 通过 stdin 管道发送 JSON
    payload = {"text": message.text, "from": message.from_agent, ...}
    await manager.write_to_task(task_id, json.dumps(payload))
```

**与 InProcess 的关键区别**：
- InProcess: 同进程内 asyncio.Task，低延迟，共享内存
- Subprocess: 独立进程，通过 stdin/stdout 通信，完全隔离

---

## 五、TeamLifecycleManager——团队持久化

```python
# swarm/team_lifecycle.py — TeamFile 结构
@dataclass
class TeamFile:
    name: str
    created_at: float
    lead_agent_id: str              # Leader 的 agent_id
    members: dict[str, TeamMember]  # agent_id → 成员信息
    team_allowed_paths: list[AllowedPath]  # 团队级别的允许路径
    # 持久化到 ~/.openharness/teams/<name>/team.json
```

```python
# swarm/team_lifecycle.py — TeamMember
@dataclass
class TeamMember:
    agent_id: str           # "worker@myteam"
    name: str               # "worker"
    backend_type: BackendType
    status: Literal["active", "idle", "stopped"]
    worktree_path: str | None   # Git worktree 路径（文件隔离）
    color: str | None           # UI 颜色
    model: str | None           # 模型覆盖
```

**会话清理**：
```python
# swarm/team_lifecycle.py:671-692
async def cleanup_session_teams():
    """Leader 退出时清理所有团队资源。"""
    teams = list(_session_created_teams)
    # 1. 先杀孤儿 pane（tmux/iTerm2）
    await asyncio.gather(*(_kill_orphaned_teammate_panes(t) for t in teams))
    # 2. 再删目录（team.json + 邮箱 + worktree）
    await asyncio.gather(*(cleanup_team_directories(t) for t in teams))
```

---

## 六、Git Worktree 隔离——避免文件冲突

当多个 Worker 同时修改文件时，git worktree 提供独立的工作目录：

```python
# swarm/worktree.py:150-211
async def create_worktree(self, repo_path, slug, branch=None, agent_id=None):
    validate_worktree_slug(slug)
    worktree_path = self.base_dir / _flatten_slug(slug)

    # git worktree add -B worktree-<slug> <path> HEAD
    await _run_git("worktree", "add", "-B", worktree_branch, str(worktree_path), "HEAD", cwd=repo_path)

    # 符号链接共享大目录（避免重复 node_modules 等）
    await _symlink_common_dirs(repo_path, worktree_path)
    # 符号链接: node_modules, .venv, __pycache__, .tox

    return WorktreeInfo(slug=slug, path=worktree_path, branch=worktree_branch, ...)
```

**为什么需要？** 假设两个 Worker 同时编辑不同文件：
- 没有 worktree：Worker A 的 `git status` 会看到 Worker B 的未提交修改
- 有 worktree：每个 Worker 在独立目录中工作，互不干扰

---

## 七、Coordinator 模式 vs 普通模式

| 维度 | 普通模式 | Coordinator 模式 |
|------|---------|-----------------|
| 环境变量 | 无 | `CLAUDE_CODE_COORDINATOR_MODE=1` |
| 可用工具 | 42+ 全部工具 | 只有 `agent` + `send_message` + `task_stop` |
| 系统提示词 | 标准 8 片段 | 额外注入 Coordinator 系统提示词 |
| 工作方式 | 自己搜索+编码 | 指挥 Worker 做事，自己只做综合决策 |
| Worker 通知 | 不适用 | `<task-notification>` XML 格式 |

```python
# coordinator/coordinator_mode.py:108-125
def format_task_notification(n: TaskNotification) -> str:
    """将任务结果序列化为 XML——Worker 完成后发给 Coordinator。"""
    return f"""<task-notification>
<task-id>{n.task_id}</task-id>
<status>{n.status}</status>
<summary>{n.summary}</summary>
<result>{n.result}</result>
</task-notification>"""
```

---

## 八、环境变量传承——spawn_utils

```python
# swarm/spawn_utils.py:22-67 — 必须传给 Worker 的环境变量
_TEAMMATE_ENV_VARS = [
    "ANTHROPIC_API_KEY",        # API 密钥（没有就无法调用 LLM）
    "ANTHROPIC_BASE_URL",       # 自定义端点
    "OPENHARNESS_API_FORMAT",   # openai/anthropic/copilot
    "OPENAI_API_KEY",           # OpenAI 兼容 key
    "HTTPS_PROXY", "HTTP_PROXY", # 代理设置
    "SSL_CERT_FILE",            # 自定义 CA 证书
    ...
]
```

**为什么需要显式传递？** tmux 可能启动新的 login shell，不继承父进程环境。如果不传这些变量，Worker 会因为找不到 API key 而全部失败。

---

## 九、设计模式总结

| 模式 | 在哪里 | 作用 |
|------|--------|------|
| **Protocol（策略模式）** | `TeammateExecutor` + `PaneBackend` | 4 种后端可互换 |
| **单例模式** | `BackendRegistry` + `TeamRegistry` + `BackgroundTaskManager` | 进程级唯一实例 |
| **ContextVar 隔离** | `_teammate_context_var` | 同进程多 Agent 并发时的上下文隔离 |
| **双信号取消** | `TeammateAbortController` | 优雅退出 + 强制退出两级控制 |
| **原子文件写入** | `TeammateMailbox.write()` | `.tmp` + `os.rename()` + `fcntl.flock()` |
| **惰性加载** | `swarm/__init__.py` | POSIX-only 模块（fcntl）延迟导入，Windows 不崩溃 |
| **三级覆盖** | Agent 定义加载 | 内置 < 用户 < 插件，同名覆盖 |
| **协调者-工作者** | Coordinator 系统提示词 | Coordinator 不写代码，只综合决策和分发任务 |
| **文件邮箱** | `TeammateMailbox` | 跨进程/跨 Task 的异步消息队列 |

---

## 十、与已学知识的关联

| 已学内容 | 多 Agent 系统的角色 |
|---------|-------------------|
| **06-Agent 循环** | 每个 Worker 内部运行相同的 `run_query()` 循环 |
| **10-工具系统** | `AgentTool` / `SendMessageTool` 就是普通的工具实现 |
| **12-API 客户端** | Worker 复用同一个 API 客户端 Protocol |
| **13-权限系统** | Worker 的权限请求通过邮箱发给 Leader，Leader 代为决策 |
| **05-前后端协议** | TaskNotification XML 类似 OHJSON 协议的设计思路 |

---

## 核心收获清单

1. **复用而非重写**：每个 Agent 内部运行的是完全相同的 `run_query()` 循环，不是另起炉灶
2. **Protocol + Registry**：`TeammateExecutor` Protocol + `BackendRegistry` 单例实现 4 种后端无缝切换
3. **ContextVar 隔离**：Python 的 `contextvars` 模块让同一进程中的多个 asyncio Task 拥有独立上下文，等价于 Go 的 goroutine-local storage
4. **文件邮箱系统**：基于文件系统的消息队列，原子写入 + fcntl 锁 + 7 种消息类型，实现跨进程/跨 Task 通信
5. **双信号取消**：优雅取消（完成当前工具）+ 强制取消（立即中断），两级安全退出
6. **协调者不写代码**：Coordinator 的核心价值是综合研究结果、制定方案、分发任务——不直接操作文件
7. **Git Worktree 隔离**：多 Worker 并行修改不同文件时，各自在独立的 worktree 中工作，避免 git 冲突

---

*下一步建议：方向 D「命令系统」—— 1374 行的 registry.py，54 个斜杠命令的注册和执行。*

---

*最后更新：2026-04-08*
