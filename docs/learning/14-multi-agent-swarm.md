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

## 二、5 层架构逐层剖析——精确到行号的调用链

### 第 1 层：Coordinator 协调器

#### 2.1 AgentDefinition 数据模型

> 📄 `coordinator/agent_definitions.py:60-134` — 43 个字段的 Pydantic 模型

```python
class AgentDefinition(BaseModel):
    name: str                                    # L89: 路由键 (如 "Explore", "worker")
    description: str                             # L90: 何时使用此 Agent
    system_prompt: str | None = None             # L93: 角色 System Prompt
    tools: list[str] | None = None               # L94: None=全部工具, ["*"]=同上
    disallowed_tools: list[str] | None = None    # L95: 黑名单
    model: str | None = None                     # L98: 模型覆盖 (如 "haiku")
    permission_mode: str | None = None           # L102: "dontAsk" / "plan" / ...
    color: str | None = None                     # L116: UI 颜色
    background: bool = False                     # L119: 后台任务标记
    permissions: list[str] = []                  # L132: 额外权限规则
```

#### 2.2 7 个内置 Agent（各有专属 System Prompt）

| Agent 名 | 定义位置 | System Prompt 位置 | 核心约束 |
|----------|---------|-------------------|---------|
| `general-purpose` | L160 | L160-163 `_GENERAL_PURPOSE_SYSTEM_PROMPT` | 无限制，通用型 |
| `Explore` | L165 | L165-199 `_EXPLORE_SYSTEM_PROMPT` | **只读！** 禁止 file_edit/file_write/bash 写操作 |
| `Plan` | L199 | L199-368 `_PLAN_SYSTEM_PROMPT` | 只读探索 + 输出结构化实施方案 |
| `worker` | L368 | 无专属 prompt（继承通用） | 全部工具可用 |
| `verification` | L368 | 无专属 prompt | 只读 + `background=True` + `color="red"` |
| `statusline-setup` | L369 | L369-451 `_STATUSLINE_SYSTEM_PROMPT` | 只有 Read + Edit |
| `claude-code-guide` | L451 | L451-505 `_CLAUDE_CODE_GUIDE_SYSTEM_PROMPT` | Glob/Grep/Read/WebFetch/WebSearch |

#### 2.3 Agent 定义的三级覆盖加载

> 📄 `coordinator/agent_definitions.py:905-945`

```
加载优先级（同名覆盖）：内置 Agent < 用户 ~/.openharness/agents/*.md < 插件 Agent
```

用户自定义用 `.md` + YAML frontmatter 格式，`parse_agent_markdown()` 函数负责解析。

#### 2.4 Coordinator 系统提示词

> 📄 `coordinator/coordinator_mode.py:251-519` — 270 行的超长 Prompt

**关键内容**（逐行定位）：
- **L267-277**: 角色定义（"你是协调者，不写代码"）
- **L279-284**: 可用工具（只有 `agent` + `send_message` + `task_stop`）
- **L286-291**: 使用 agent 工具的规则（不要用 worker 检查另一个 worker）
- **L293-311**: `<task-notification>` XML 格式定义（Worker 完成后的结果通知）
- **L349-368**: 四阶段工作流（Research → Synthesis → Implementation → Verification）
- **L402-484**: 编写 Worker Prompt 的指南（**最核心**——"Workers can't see your conversation"）

```python
# L185-188 — 如何判断是否处于 Coordinator 模式
def is_coordinator_mode() -> bool:
    val = os.environ.get("CLAUDE_CODE_COORDINATOR_MODE", "")
    return val.lower() in {"1", "true", "yes"}

# L215-217 — Coordinator 只有 3 个工具
def get_coordinator_tools() -> list[str]:
    return ["agent", "send_message", "task_stop"]
```

---

### 第 2 层：Tools 工具层——精确调用链

#### 2.5 AgentTool：LLM → spawn Worker 的完整链路

> 📄 `tools/agent_tool.py` (98 行)

**入参模型** `AgentToolInput`（L18-33）：
```python
class AgentToolInput(BaseModel):
    description: str       # L21: "调查认证代码"
    prompt: str            # L22: Worker 要执行的完整任务描述 ← 这是唯一传给 Worker 的上下文！
    subagent_type: str     # L23: "Explore" / "worker" / "verification"
    model: str | None      # L27: 模型覆盖
    team: str | None       # L29: 团队名
    mode: str = "local_agent"  # L30: 执行模式
```

**execute() 内部的 4 步调用链**（L43-97）：

```
步骤 1 (L50-53): 查找 Agent 定义
  agent_tool.py:52  →  agent_definitions.py:get_agent_definition()
                       → 从 7 个内置 + 用户 + 插件中按名称查找

步骤 2 (L59-67): 选择后端
  agent_tool.py:60  →  registry.py:400  get_backend_registry()  (模块单例)
  agent_tool.py:62  →  registry.py:270  get_executor("in_process")
                       → 如果 KeyError:
  agent_tool.py:65  →  registry.py:270  get_executor("subprocess")

步骤 3 (L69-78): 构建 TeammateSpawnConfig
  TeammateSpawnConfig 定义在 swarm/types.py:257-307
  关键字段：
    prompt = arguments.prompt          # L72: 唯一传给 Worker 的对话内容
    cwd = str(context.cwd)             # L73: 继承 Leader 的工作目录
    model = arguments.model or agent_def.model  # L75: 模型继承或覆盖
    system_prompt = agent_def.system_prompt      # L76: Agent 定义的角色提示词
    permissions = agent_def.permissions          # L77: 权限列表

步骤 4 (L80-97): 执行 spawn
  agent_tool.py:81  →  in_process.py:436  InProcessBackend.spawn(config)
                   或→  subprocess_backend.py:47  SubprocessBackend.spawn(config)
```

#### 2.6 SendMessageTool：Leader → Worker 消息路由

> 📄 `tools/send_message_tool.py` (63 行)

**两种路由**（L31-40）——按 `@` 符号区分：

```
路由 A: task_id 包含 "@"（如 "Explore@default"）→ Swarm Agent 路由
  send_message_tool.py:34-35
    → L42 _send_swarm_message()
      → L44-52 get_backend_registry().get_executor("in_process")  [registry.py:400→270]
      → L54 构建 TeammateMessage(text=message, from_agent="coordinator")  [types.py:336-343]
      → L56 executor.send_message(agent_id, msg)
        → in_process.py:494-527  InProcessBackend.send_message()
          → L514-526 构建 MailboxMessage(type="user_message", ...)  [mailbox.py:37-47]
          → L525-526 TeammateMailbox(team, agent_name).write(msg)  [mailbox.py:122-153]
              → L140-149 _write_atomic(): fcntl.flock + .tmp + os.rename（原子写入）

路由 B: 普通 task_id → Legacy Task 路由
  send_message_tool.py:36-40
    → get_task_manager().write_to_task(task_id, message)  [tasks/manager.py]
    → 直接写 stdin 管道
```

---

### 第 3 层：Swarm 核心层

#### 2.7 TeammateExecutor Protocol

> 📄 `swarm/types.py:351-382` — 后端统一接口

```python
@runtime_checkable
class TeammateExecutor(Protocol):
    type: BackendType                     # L358: "subprocess" | "in_process" | "tmux" | "iterm2"
    def is_available(self) -> bool: ...   # L360
    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult: ...      # L364
    async def send_message(self, agent_id: str, message: TeammateMessage) -> None: ...  # L368
    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool: ...  # L372
```

**隐式实现**（Protocol 模式，不需要写继承声明）：
- `InProcessBackend`（`in_process.py:413`）
- `SubprocessBackend`（`subprocess_backend.py:28`）

#### 2.8 BackendRegistry — 单例 + 自动检测

> 📄 `swarm/registry.py` (411 行)

```
模块单例（L397-405）:
  _registry: BackendRegistry | None = None
  get_backend_registry() → 首次调用创建，后续复用

初始化（L112-117 __init__ → L379-391 _register_defaults）:
  始终注册: SubprocessBackend  [subprocess_backend.py:28]
  POSIX 平台额外注册: InProcessBackend  [in_process.py:413]
  条件: get_platform_capabilities().supports_swarm_mailbox == True

自动检测优先级（L128-183 detect_backend）:
  ① in_process_fallback_active? → "in_process"           [L148-157]
  ② _detect_tmux() + tmux 已注册? → "tmux"               [L159-169]
  ③ 兜底 → "subprocess"                                   [L176-183]
```

#### 2.9 四种后端对比

| 后端 | 类定义位置 | spawn 实现 | 通信方式 | 隔离机制 |
|------|-----------|-----------|---------|---------|
| **in_process** | `in_process.py:413` | L436-492 `asyncio.create_task()` | ContextVar + 文件邮箱 | ContextVar copy-on-create |
| **subprocess** | `subprocess_backend.py:28` | L47-94 `TaskManager.create_agent_task()` | stdin/stdout JSON | 进程隔离 |
| **tmux** | 可扩展（未内置） | — | 文件邮箱 | pane 隔离 |
| **iterm2** | 可扩展（未内置） | — | 文件邮箱 | tab 隔离 |

---

### 第 4 层：InProcessBackend 深度剖析

#### 2.10 ContextVar 隔离

> 📄 `swarm/in_process.py:173-188`

```python
_teammate_context_var: ContextVar[TeammateContext | None] = ContextVar(  # L173
    "_teammate_context_var", default=None
)
def get_teammate_context() -> TeammateContext | None:  # L178 — 任何代码都能调用
    return _teammate_context_var.get()
def set_teammate_context(ctx: TeammateContext) -> None:  # L186 — 只在 start_in_process_teammate 调用
    _teammate_context_var.set(ctx)
```

**TeammateContext 数据结构**（L113-170）：
| 字段 | 行号 | 用途 |
|------|------|------|
| `agent_id` | L121 | 唯一标识 "agentName@teamName" |
| `agent_name` | L124 | 人可读名 "researcher" |
| `team_name` | L127 | 所属团队 |
| `abort_controller` | L139 | 双信号取消控制器 |
| `message_queue` | L144 | 内存消息队列（Leader 发来的消息暂存于此） |
| `status` | L153 | "starting" → "running" → "idle" → "stopped" |
| `tool_use_count` | L159 | 累计工具调用次数 |
| `total_tokens` | L162 | 累计 Token 用量 |

#### 2.11 双信号取消机制

> 📄 `swarm/in_process.py:52-102`

```python
class TeammateAbortController:
    cancel_event: asyncio.Event    # L64: 优雅取消
    force_cancel: asyncio.Event    # L67: 强制取消

    def request_cancel(self, reason=None, *, force=False):  # L77
        if force:
            self.force_cancel.set()   # L90: 立即终止
            self.cancel_event.set()   # L91: 同时设优雅信号
        else:
            self.cancel_event.set()   # L97: 只设优雅信号
```

**谁调用 request_cancel？**
- `InProcessBackend.shutdown(force=False)` → L568 `request_cancel(reason="graceful shutdown")`
- `InProcessBackend.shutdown(force=True)` → L562 `request_cancel(reason="force shutdown", force=True)`
- `_drain_mailbox()` 收到 shutdown 消息 → L317 `request_cancel(reason="shutdown message received")`

#### 2.12 spawn → 执行循环 完整调用链

**这是多 Agent 最核心的链路**，从 spawn 到 Worker 运行 `run_query()` 的完整路径：

```
① AgentTool.execute()                         [agent_tool.py:81]
   └→ InProcessBackend.spawn(config)           [in_process.py:436-492]
      ├─ L443: agent_id = f"{config.name}@{config.team}"
      ├─ L460: abort_controller = TeammateAbortController()
      ├─ L464-471: task = asyncio.create_task(    ← Python copy-on-create 隔离
      │      start_in_process_teammate(
      │          config=config,
      │          agent_id=agent_id,
      │          abort_controller=abort_controller,
      │      )
      │  )
      ├─ L473-478: 注册到 _active[agent_id] = _TeammateEntry(task, abort, task_id)
      └─ L488-492: return SpawnResult(task_id, agent_id, backend_type)

② start_in_process_teammate()                  [in_process.py:196-292]
   ├─ L232-242: 创建 TeammateContext（agent_id, name, team, abort_controller...）
   ├─ L243: set_teammate_context(ctx)           ← 绑定到当前 asyncio Task 的 ContextVar
   ├─ L245: mailbox = TeammateMailbox(team, agent_id)  [mailbox.py:101-108]
   ├─ L250: ctx.status = "running"
   ├─ L252-253: if query_context:
   │      await _run_query_loop(query_context, config, ctx, mailbox)
   │  else:
   │      L254-268: Stub 模式（无 QueryContext 时占位运行）
   └─ finally (L275-292):
       ├─ L276: ctx.status = "stopped"
       ├─ L279-285: 写 idle_notification 到 Leader 邮箱
       │      create_idle_notification()         [mailbox.py:269-275]
       │      TeammateMailbox("leader").write()   [mailbox.py:122-153]
       └─ L287-292: 日志记录

③ _run_query_loop()                            [in_process.py:335-395]
   ├─ L350: from engine.query import run_query   ← 延迟导入（避免循环依赖）
   ├─ L353-355: messages = [ConversationMessage.from_user_text(config.prompt)]
   │            ↑ 全新的消息列表！只有 Leader 传来的 prompt 一条消息
   ├─ L357: async for event, usage in run_query(query_context, messages):
   │         ↑ 复用与主 Agent 完全相同的 run_query()！ [engine/query.py:119-233]
   │
   │  每个事件循环中：
   ├─ L359-362: 累计 token 用量 → ctx.total_tokens
   ├─ L370-375: 检查 abort_controller.is_cancelled → return 退出
   ├─ L378-380: _drain_mailbox(mailbox, ctx)     [in_process.py:295-332]
   │      ├─ L305: mailbox.read_all(unread_only=True)   [mailbox.py:155-183]
   │      ├─ L315-318: shutdown 消息 → request_cancel + return True
   │      └─ L320-330: user_message → ctx.message_queue.put(msg)
   └─ L383-393: 从 message_queue 取消息 → 注入为新 user turn
          messages.append(ConversationMessage(role="user", content=queued.text))
```

#### 2.13 send_message → Worker 收到消息的完整链路

```
① SendMessageTool.execute()                    [send_message_tool.py:34-35]
   └→ _send_swarm_message(agent_id, message)   [send_message_tool.py:42-62]
      └→ InProcessBackend.send_message()        [in_process.py:494-527]
         ├─ L510: agent_name, team_name = agent_id.split("@")
         ├─ L514-524: 构建 MailboxMessage(type="user_message", payload={content: text})
         ├─ L525: mailbox = TeammateMailbox(team_name, agent_name)
         └─ L526: await mailbox.write(msg)      [mailbox.py:122-153]
                  └─ L140-149: _write_atomic()
                     ├─ fcntl.flock(LOCK_EX)    # 加排他锁
                     ├─ tmp_path.write_text()    # 写临时文件
                     ├─ os.rename(tmp → final)   # 原子重命名
                     └─ fcntl.flock(LOCK_UN)     # 释放锁

② Worker 在 _run_query_loop 每轮末尾轮询邮箱    [in_process.py:378]
   └→ _drain_mailbox(mailbox, ctx)              [in_process.py:295-332]
      ├─ L305: pending = mailbox.read_all()     [mailbox.py:155-183]
      │         └─ 扫描 inbox/*.json，跳过 .tmp 和 .lock
      ├─ L320-330: user_message 类型
      │    └─ ctx.message_queue.put(TeammateMessage(text=content))
      └─ 返回 _run_query_loop

③ _run_query_loop 下一轮开始前 drain queue      [in_process.py:383-393]
   └─ messages.append(ConversationMessage(role="user", content=queued.text))
      ↑ 消息被注入为 Worker 对话历史中的新 user 消息
      ↑ Worker 的 LLM 在下一轮 run_query 中会看到这条消息
```

#### 2.14 SubprocessBackend 的对比链路

> 📄 `subprocess_backend.py` (151 行)

```
spawn（L47-94）:
  ├─ L55-58: build_inherited_cli_flags()         [spawn_utils.py:93-165]
  │           → "--headless" + "--model xxx" + "--permission-mode xxx"
  ├─ L59: build_inherited_env_vars()              [spawn_utils.py:168-186]
  │           → ANTHROPIC_API_KEY + HTTPS_PROXY + ... (共 20+ 个变量)
  ├─ L64-66: command = "python -m openharness --headless --model ..."
  │           ↑ 就是启动了一个新的 oh 实例！
  └─ L70-77: manager.create_agent_task(prompt, command, cwd)
             ↑ prompt 通过 stdin 传入子进程

send_message（L96-118）:
  ├─ L106-114: payload = {"text": ..., "from": ..., "timestamp": ...}
  └─ L117: manager.write_to_task(task_id, json.dumps(payload))
           ↑ 通过 stdin 管道发送 JSON
```

**关键区别**：InProcess 用 asyncio Task + ContextVar + 文件邮箱；Subprocess 用 fork 新进程 + stdin/stdout。Worker 内部跑的都是 `run_query()` 循环，但 Subprocess 是完整的 `oh` 实例（经过 CLI → build_runtime → 全套初始化）。

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
