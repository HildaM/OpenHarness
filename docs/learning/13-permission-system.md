# 13 — 权限系统：AI 安全的多层防线

> 涉及源文件：`permissions/modes.py` (14行) · `permissions/checker.py` (107行) · `permissions/__init__.py` (27行) · `config/settings.py` (226行) · `engine/query.py` (361行) · `tools/base.py` (76行) · `ui/backend_host.py` (553行) · `ui/textual_app.py` (412行) · `ui/permission_dialog.py` (15行) · `ui/app.py` (163行) · `commands/registry.py` (1374行) · `cli.py` (682行) · `sandbox/adapter.py` (138行) · `swarm/permission_sync.py` (1186行) · `coordinator/agent_definitions.py` (976行)
>
> 预计阅读时间：35 分钟
>
> 前置知识：已理解 Agent 循环（06）、工具执行 6 道关卡（06）、API 客户端（12）

---

## 本章核心问题

在 `_execute_tool_call()` 的关卡 4 中，一行代码就能决定工具是否被允许执行：

```python
decision = context.permission_checker.evaluate(tool_name, is_read_only=..., ...)
```

但这行代码背后：3 种权限模式怎么工作？8 级优先级怎么判定？用户确认弹窗怎么跨进程交互？多 Agent 协作时权限怎么协调？OS 级沙箱又是什么？

---

## 一、全景架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                         用户层 (User Layer)                           │
│                                                                      │
│  CLI 参数:  --permission-mode default|plan|full_auto                 │
│             --dangerously-skip-permissions   (= full_auto)           │
│             --allowed-tools file_read,grep                           │
│             --disallowed-tools bash                                  │
│                                                                      │
│  斜杠命令:  /permissions show|set MODE                               │
│             /plan on|off                                              │
│                                                                      │
│  配置文件:  ~/.openharness/settings.json → permission 字段            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      配置层 (Settings)                               │
│                                                                     │
│  PermissionSettings {                                               │
│    mode: DEFAULT | PLAN | FULL_AUTO                                 │
│    allowed_tools: ["file_read", ...]                                │
│    denied_tools: ["bash", ...]                                      │
│    path_rules: [{pattern: "/etc/*", allow: false}, ...]             │
│    denied_commands: ["rm -rf *", ...]                                │
│  }                                                                  │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    决策引擎 (PermissionChecker)                       │
│                                                                     │
│  evaluate(tool_name, is_read_only, file_path, command)              │
│    → PermissionDecision(allowed, requires_confirmation, reason)     │
│                                                                     │
│  8 级优先级（从高到低）：                                             │
│    ① denied_tools   → 拒绝                                          │
│    ② allowed_tools  → 放行                                          │
│    ③ path_rules     → 匹配则拒绝                                    │
│    ④ denied_commands → 匹配则拒绝                                    │
│    ⑤ FULL_AUTO      → 放行                                          │
│    ⑥ is_read_only   → 放行                                          │
│    ⑦ PLAN mode      → 拒绝                                          │
│    ⑧ DEFAULT mode   → requires_confirmation=True                    │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         直接放行         直接拒绝      需要用户确认
                                              │
                              ┌────────────────┼────────────────┐
                              ▼                ▼                ▼
                       React 前端弹窗   Textual TUI 弹窗   prompt_toolkit
                      (backend_host)  (PermissionScreen)  (permission_dialog)
                              │                │                │
                              └────────────────┼────────────────┘
                                               ▼
                                          用户按 y/n
                                               │
                              ┌────────────────┼────────────────┐
                              ▼                                 ▼
                         y → 放行                          n → 拒绝
                     → 关卡 5 执行                    → is_error=True 返回 LLM
```

---

## 二、权限模式：三选一

### 2.1 枚举定义（14 行代码，整个文件就这么多）

```python
# permissions/modes.py — 完整文件
class PermissionMode(str, Enum):
    """Supported permission modes."""
    DEFAULT = "default"      # 只读工具自动放行，写操作弹窗确认
    PLAN = "plan"            # 只读工具放行，写操作直接拒绝（"只看不动"）
    FULL_AUTO = "full_auto"  # 所有工具自动放行（"全信任"）
```

**继承 `str, Enum`** 是 Python 的一个技巧——让枚举值同时是字符串，可以直接和 `"default"` 比较，也可以存入 JSON。

### 2.2 三种模式对照表

| 模式 | 只读工具 | 写入工具 | 典型场景 |
|------|---------|---------|---------|
| **DEFAULT** | ✅ 自动放行 | ⚠️ 弹窗确认 | 日常开发（默认值） |
| **PLAN** | ✅ 自动放行 | ❌ 直接拒绝 | 让 AI 规划方案但不执行 |
| **FULL_AUTO** | ✅ 自动放行 | ✅ 自动放行 | CI/CD 流水线、沙箱环境 |

### 2.3 设置方式（3 种途径，优先级递增）

```
配置文件 (~/.openharness/settings.json):
  {"permission": {"mode": "default"}}

CLI 参数（覆盖配置文件）:
  oh --permission-mode plan "设计一个新功能"

危险快捷方式（等价于 full_auto）:
  oh --dangerously-skip-permissions "部署到生产"
```

对应 CLI 代码：

```python
# cli.py:581-585
if dangerously_skip_permissions:
    permission_mode = "full_auto"   # 直接覆盖，跳过所有检查
```

---

## 三、决策引擎：PermissionChecker（107 行，核心中的核心）

### 3.1 数据结构

```python
# permissions/checker.py:15-21
@dataclass(frozen=True)
class PermissionDecision:
    """权限检查的三态返回值。"""
    allowed: bool                    # True=放行, False=不放行
    requires_confirmation: bool = False  # True=需要用户确认（仅 DEFAULT 模式）
    reason: str = ""                 # 原因说明（给用户/LLM 看）
```

注意 `frozen=True`——决策一旦做出就不能修改，跟 `ApiMessageRequest` 的设计理念一致。

**三态而非二态**是关键设计——`allowed=False` 时还有两种情况：

```
allowed=False, requires_confirmation=False → 直接拒绝（黑名单/PLAN 模式/路径规则）
allowed=False, requires_confirmation=True  → 可以确认（DEFAULT 模式下的写操作）
```

### 3.2 路径规则数据结构

```python
# permissions/checker.py:24-29
@dataclass(frozen=True)
class PathRule:
    """基于 glob 的文件路径权限规则。"""
    pattern: str    # glob 模式，如 "/etc/*", "*.env"
    allow: bool     # True=允许, False=拒绝
```

配置来源：

```json
// ~/.openharness/settings.json
{
  "permission": {
    "path_rules": [
      {"pattern": "/etc/*", "allow": false},
      {"pattern": "*.env", "allow": false},
      {"pattern": "~/.ssh/*", "allow": false}
    ]
  }
}
```

### 3.3 evaluate() 方法——8 级优先级决策链

这是整个权限系统最核心的方法，完整代码只有 57 行，但逻辑精密：

```python
# permissions/checker.py:50-106
def evaluate(
    self,
    tool_name: str,
    *,
    is_read_only: bool,        # 工具是否只读（由 tool.is_read_only() 判定）
    file_path: str | None = None,   # 操作的文件路径（file_edit, file_write 等）
    command: str | None = None,     # 执行的命令（bash 工具）
) -> PermissionDecision:

    # ① 工具黑名单 — 最高优先级，无条件拒绝
    if tool_name in self._settings.denied_tools:
        return PermissionDecision(allowed=False, reason=f"{tool_name} is explicitly denied")

    # ② 工具白名单 — 第二优先级，无条件放行
    if tool_name in self._settings.allowed_tools:
        return PermissionDecision(allowed=True, reason=f"{tool_name} is explicitly allowed")

    # ③ 路径拒绝规则 — glob 匹配
    if file_path and self._path_rules:
        for rule in self._path_rules:
            if fnmatch.fnmatch(file_path, rule.pattern):
                if not rule.allow:
                    return PermissionDecision(
                        allowed=False,
                        reason=f"Path {file_path} matches deny rule: {rule.pattern}",
                    )

    # ④ 命令拒绝模式 — 防止危险命令
    if command:
        for pattern in self._settings.denied_commands:
            if fnmatch.fnmatch(command, pattern):
                return PermissionDecision(
                    allowed=False,
                    reason=f"Command matches deny pattern: {pattern}",
                )

    # ⑤ FULL_AUTO 模式 — 全部放行
    if self._settings.mode == PermissionMode.FULL_AUTO:
        return PermissionDecision(allowed=True, reason="Auto mode allows all tools")

    # ⑥ 只读工具 — 在 DEFAULT 和 PLAN 模式下都放行
    if is_read_only:
        return PermissionDecision(allowed=True, reason="read-only tools are allowed")

    # ⑦ PLAN 模式 — 拒绝所有写操作
    if self._settings.mode == PermissionMode.PLAN:
        return PermissionDecision(
            allowed=False,
            reason="Plan mode blocks mutating tools until the user exits plan mode",
        )

    # ⑧ DEFAULT 模式 — 需要用户确认
    return PermissionDecision(
        allowed=False,
        requires_confirmation=True,
        reason="Mutating tools require user confirmation in default mode",
    )
```

**为什么 ①② 的优先级高于 ⑤⑥⑦？**

因为黑名单/白名单是**用户的显式意图**——即使在 FULL_AUTO 模式下，黑名单中的工具也会被拒绝。这是安全设计的重要原则：**显式规则 > 模式默认行为**。

### 3.4 优先级决策流程图

```
evaluate(tool_name, is_read_only, file_path, command)
    │
    ├─ tool_name ∈ denied_tools?    ──YES──→ 拒绝 ①
    │
    ├─ tool_name ∈ allowed_tools?   ──YES──→ 放行 ②
    │
    ├─ file_path matches deny rule? ──YES──→ 拒绝 ③
    │
    ├─ command matches deny pattern? ──YES──→ 拒绝 ④
    │
    ├─ mode == FULL_AUTO?           ──YES──→ 放行 ⑤
    │
    ├─ is_read_only?                ──YES──→ 放行 ⑥
    │
    ├─ mode == PLAN?                ──YES──→ 拒绝 ⑦
    │
    └─ mode == DEFAULT              ────────→ 需确认 ⑧
```

---

## 四、权限检查在执行流水线中的位置

权限检查是 `_execute_tool_call()` 6 道关卡中的**第 4 关**：

```python
# engine/query.py:293-323 — 关卡 4
_file_path = str(tool_input.get("file_path", "")) or None
_command = str(tool_input.get("command", "")) or None
decision = context.permission_checker.evaluate(
    tool_name,
    is_read_only=tool.is_read_only(parsed_input),   # ← 问工具自己
    file_path=_file_path,                            # ← 从入参提取
    command=_command,                                 # ← 从入参提取
)
if not decision.allowed:
    if decision.requires_confirmation and context.permission_prompt is not None:
        # ⑧ DEFAULT 模式 → 弹窗让用户确认
        confirmed = await context.permission_prompt(tool_name, decision.reason)
        if not confirmed:
            return ToolResultBlock(tool_use_id=..., content="Permission denied", is_error=True)
    else:
        # ①③④⑦ 直接拒绝，不可确认
        return ToolResultBlock(tool_use_id=..., content=decision.reason, is_error=True)
```

**两个重要入参的来源**：

1. **`is_read_only`** — 来自工具自身的判断：

```python
# tools/base.py:41-44
class BaseTool(ABC):
    def is_read_only(self, arguments: BaseModel) -> bool:
        """默认返回 False（写操作）。只读工具子类覆盖此方法返回 True。"""
        return False
```

例如 `FileReadTool` 会覆盖为 `return True`，而 `BashTool` 可能根据命令内容动态判断。

2. **`file_path` / `command`** — 从 LLM 传入的工具参数中提取，用于路径规则和命令模式匹配。

---

## 五、用户确认的跨进程交互（三种 UI 实现）

当权限检查返回 `requires_confirmation=True` 时，需要弹窗让用户确认。项目为 3 种 UI 模式各提供了一个实现：

### 5.1 React 前端模式（最复杂）

```python
# backend_host.py:439-484 — 完整的跨进程异步交互
async def _ask_permission(self, tool_name: str, reason: str) -> bool:
    request_id = uuid4().hex
    future = asyncio.get_running_loop().create_future()  # 1. 创建空承诺
    self._permission_requests[request_id] = future

    await self._emit(BackendEvent(                        # 2. 发给前端
        type="modal_request",
        modal={"kind": "permission", "request_id": request_id,
               "tool_name": tool_name, "reason": reason},
    ))

    try:
        return await future    # 3. 挂起等待前端回复
    finally:
        self._permission_requests.pop(request_id, None)
```

```
时序图：

后端                                    前端（React）
  │                                        │
  ├── _ask_permission("file_edit", ...)    │
  │   创建 Future                           │
  │   ────── modal_request ──────────→     │  弹出弹窗
  │   await future（挂起）                  │  "Allow file_edit? [y] [n]"
  │                                        │
  │                                        │  用户按 y
  │   ←──── permission_response ─────      │  {allowed: true}
  │   future.set_result(True)              │
  │   返回 True → 引擎继续执行              │
```

### 5.2 Textual TUI 模式

```python
# textual_app.py:42-80
class PermissionScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "allow", "Allow"),       # 按 y = 允许
        Binding("n", "deny", "Deny"),         # 按 n = 拒绝
        Binding("escape", "deny", "Deny"),    # 按 Esc = 拒绝
    ]

    def compose(self):
        yield Container(
            Static(Panel.fit(
                f"Allow tool [bold]{self._tool_name}[/bold]?\n\n{self._reason}",
                title="Permission Required",
            )),
            Horizontal(
                Button("Allow", id="allow", variant="success"),
                Button("Deny", id="deny", variant="error"),
            ),
        )

    def action_allow(self): self.dismiss(True)
    def action_deny(self): self.dismiss(False)
```

### 5.3 命令行 prompt_toolkit 模式

```python
# permission_dialog.py — 完整文件（只有 15 行）
async def ask_permission(tool_name: str, reason: str) -> bool:
    session = PromptSession()
    response = await session.prompt_async(
        f"Allow tool '{tool_name}'? [{reason}] [y/N]: "
    )
    return response.strip().lower() in {"y", "yes"}
```

### 5.4 非交互模式（自动放行）

```python
# app.py:81-82
async def _noop_permission(tool_name: str, reason: str) -> bool:
    return True    # 非交互模式下，所有权限请求自动放行
```

### 5.5 四种模式的注入点

```python
# runtime.py:289-299 — build_runtime 第 9 步
engine = QueryEngine(
    ...
    permission_checker=PermissionChecker(settings.permission),  # 决策引擎
    permission_prompt=permission_prompt,   # ← 在这里注入回调
    ...
)
```

| 模式 | `permission_prompt` 指向 | 行为 |
|------|------------------------|------|
| React 交互 | `backend_host._ask_permission` | 发 JSON → 前端弹窗 → 等回复 |
| Textual TUI | `PermissionScreen` 弹窗 | Textual 模态窗口 |
| 命令行 | `permission_dialog.ask_permission` | 终端文本提示 |
| 非交互 | `_noop_permission` | 直接返回 True |

---

## 六、运行时热切换——斜杠命令

权限模式可以在会话进行中**实时切换**，不需要重启：

```python
# commands/registry.py:977-998 — /permissions 命令
async def _permissions_handler(args, context):
    if tokens[0] == "set" and len(tokens) == 2:
        settings.permission.mode = PermissionMode(tokens[1])     # 1. 更新配置
        save_settings(settings)                                   # 2. 持久化
        context.engine.set_permission_checker(                    # 3. 替换检查器
            PermissionChecker(settings.permission)
        )
        context.app_state.set(permission_mode=...)                # 4. 通知前端更新 UI
```

```python
# commands/registry.py:1000-1017 — /plan 命令（快捷方式）
async def _plan_handler(args, context):
    if mode in {"on", "enter"}:
        settings.permission.mode = PermissionMode.PLAN    # 进入规划模式
        ...
    if mode in {"off", "exit"}:
        settings.permission.mode = PermissionMode.DEFAULT  # 退出规划模式
        ...
```

热切换的关键在于 `engine.set_permission_checker()`：

```python
# query_engine.py:127-129
def set_permission_checker(self, checker: PermissionChecker) -> None:
    """切换权限模式。/permissions 命令触发。"""
    self._permission_checker = checker
```

**下一次** `_execute_tool_call()` 就会使用新的 checker——因为 `QueryContext` 在每次 `submit_message()` 时从 `QueryEngine` 重新构建。

---

## 七、配置层——PermissionSettings 模型

```python
# config/settings.py:31-38
class PermissionSettings(BaseModel):
    mode: PermissionMode = PermissionMode.DEFAULT        # 权限模式
    allowed_tools: list[str] = Field(default_factory=list)  # 工具白名单
    denied_tools: list[str] = Field(default_factory=list)   # 工具黑名单
    path_rules: list[PathRuleConfig] = Field(default_factory=list)  # 路径规则
    denied_commands: list[str] = Field(default_factory=list)  # 命令拒绝模式
```

```python
# config/settings.py:24-28
class PathRuleConfig(BaseModel):
    pattern: str         # glob 模式
    allow: bool = True   # True=允许, False=拒绝
```

**配置文件示例**：

```json
{
  "permission": {
    "mode": "default",
    "allowed_tools": ["file_read", "grep", "glob"],
    "denied_tools": ["bash"],
    "path_rules": [
      {"pattern": "/etc/*", "allow": false},
      {"pattern": "*.env", "allow": false}
    ],
    "denied_commands": ["rm -rf *", "sudo *"]
  }
}
```

**配置优先级**（与其他设置一致）：

```
CLI 参数 > 环境变量 > settings.json > 代码默认值
```

---

## 八、Agent 定义中的权限控制

每个 Agent 可以有独立的权限配置：

```python
# coordinator/agent_definitions.py:40-46
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "plan", "dontAsk")
```

```python
# 内置 Agent 的权限示例
AgentDefinition(
    name="Explore",
    disallowed_tools=["agent", "exit_plan_mode", "file_edit", "file_write", "notebook_edit"],
    # ↑ 探索 Agent 被禁止使用任何写入工具
)

AgentDefinition(
    name="claude-code-guide",
    tools=["Glob", "Grep", "Read", "WebFetch", "WebSearch"],  # 白名单
    permission_mode="dontAsk",  # 不弹窗确认
)
```

**设计原则**：不同 Agent 有不同的信任等级。探索 Agent 只能看不能改，指南 Agent 只能搜索不能写。

---

## 九、OS 级沙箱——最后一道防线

权限系统是**应用层**的检查，用户/LLM 如果足够"聪明"可能绕过。`sandbox/adapter.py` 提供了 **OS 级**的强制隔离：

```python
# sandbox/adapter.py:35-48
def build_sandbox_runtime_config(settings):
    return {
        "network": {
            "allowedDomains": list(settings.sandbox.network.allowed_domains),
            "deniedDomains": list(settings.sandbox.network.denied_domains),
        },
        "filesystem": {
            "allowRead": list(settings.sandbox.filesystem.allow_read),
            "denyRead": list(settings.sandbox.filesystem.deny_read),
            "allowWrite": list(settings.sandbox.filesystem.allow_write),
            "denyWrite": list(settings.sandbox.filesystem.deny_write),
        },
    }
```

```python
# sandbox/adapter.py:104-119
def wrap_command_for_sandbox(command, *, settings=None):
    """把命令包装在 srt（sandbox-runtime）中执行。"""
    availability = get_sandbox_availability(settings)
    if not availability.active:
        return command, None    # 沙箱不可用，原样返回

    settings_path = _write_runtime_settings(config)
    # 原始命令: ["bash", "-lc", "rm -rf /"]
    # 包装后:   ["srt", "--settings", "/tmp/xxx.json", "bash", "-lc", "rm -rf /"]
    wrapped = [availability.command, "--settings", str(settings_path), *command]
    return wrapped, settings_path
```

**沙箱工作原理**：

```
应用层权限（PermissionChecker）
    ↓ 通过了
OS 层沙箱（srt + bwrap/sandbox-exec）
    ↓ 再过一道关
实际执行命令
```

**平台支持**：

| 平台 | 沙箱工具 | 状态 |
|------|---------|------|
| Linux/WSL | `bwrap`（bubblewrap） | 需安装 |
| macOS | `sandbox-exec` | 系统自带 |
| Windows | 不支持 | 建议用 WSL |

---

## 十、Swarm 分布式权限协调（进阶）

多 Agent 协作时，Worker Agent 没有直接的用户交互能力，需要通过 Leader 代为判断：

```
Worker Agent                          Leader Agent
     │                                      │
     ├── 需要执行 file_edit                   │
     │   本地无法弹窗确认                      │
     │                                      │
     ├── send_permission_request ─────→     │  收到权限请求
     │   (邮箱 或 文件系统)                    │
     │                                      │  handle_permission_request()
     │                                      │    只读工具？→ 自动批准
     │                                      │    其他？→ PermissionChecker.evaluate()
     │                                      │
     │   ←── send_permission_response       │  返回决策
     │                                      │
     ├── 收到响应                              │
     │   allowed=True → 继续执行              │
     │   allowed=False → 返回错误给 LLM       │
```

### 10.1 两种通信方式

**文件系统方式**：

```
~/.openharness/teams/<teamName>/permissions/
    ├── pending/     ← Worker 写入请求
    │   └── perm-1234567-abc1234.json
    ├── resolved/    ← Leader 写入决策
    │   └── perm-1234567-abc1234.json
    └── .lock        ← POSIX 文件锁（防并发写冲突）
```

**邮箱方式**：

```python
# Worker → Leader 邮箱
await send_permission_request_via_mailbox(request)

# Leader → Worker 邮箱
await send_permission_response_via_mailbox(worker_name, resolution, request_id)

# Worker 轮询自己的邮箱（0.5s 间隔，60s 超时）
response = await poll_permission_response(team_name, worker_id, request_id)
```

### 10.2 Leader 的自动决策逻辑

```python
# swarm/permission_sync.py:1099-1145
async def handle_permission_request(request, checker):
    # 只读工具（Read, Glob, Grep, WebFetch 等）→ 自动批准
    if _is_read_only(request.tool_name):
        return SwarmPermissionResponse(request_id=request.id, allowed=True)

    # 其他工具 → 委托给 PermissionChecker 决策
    decision = checker.evaluate(
        request.tool_name,
        is_read_only=False,
        file_path=request.input.get("file_path") or request.input.get("path"),
        command=request.input.get("command"),
    )
    return SwarmPermissionResponse(
        request_id=request.id,
        allowed=decision.allowed,
        feedback=None if decision.allowed else decision.reason,
    )
```

---

## 十一、完整数据流——一次权限检查的旅程

以 DEFAULT 模式下 LLM 请求执行 `file_edit` 为例：

```
LLM 返回: tool_use(file_edit, {file_path: "main.py", ...})
    │
    ▼
_execute_tool_call("file_edit", "tu_123", {file_path: "main.py"})
    │
    ├── 关卡 1: PreToolUse Hook → 未拦截 → 继续
    ├── 关卡 2: tool_registry.get("file_edit") → 找到 FileEditTool
    ├── 关卡 3: model_validate({file_path: "main.py"}) → 校验通过
    │
    ├── 关卡 4: permission_checker.evaluate("file_edit", ...)
    │       │
    │       ├── ① denied_tools 中有 file_edit？  → 没有
    │       ├── ② allowed_tools 中有 file_edit？  → 没有
    │       ├── ③ "main.py" 匹配 path_rules？    → 没有
    │       ├── ④ 无 command                      → 跳过
    │       ├── ⑤ mode == FULL_AUTO？             → 不是
    │       ├── ⑥ is_read_only？                  → False（FileEditTool 不是只读）
    │       ├── ⑦ mode == PLAN？                  → 不是
    │       └── ⑧ mode == DEFAULT                 → requires_confirmation=True
    │
    │   decision = PermissionDecision(allowed=False, requires_confirmation=True)
    │
    │   requires_confirmation=True → 调用 permission_prompt
    │       │
    │       ▼
    │   await context.permission_prompt("file_edit", "Mutating tools require...")
    │       │
    │       ├── (交互模式) _ask_permission → 前端弹窗 → 用户按 y → True
    │       └── (非交互模式) _noop_permission → True
    │
    │   confirmed = True
    │
    ├── 关卡 5: tool.execute(parsed_input, context) → 执行文件编辑
    └── 关卡 6: PostToolUse Hook → 通知插件
```

---

## 十二、设计模式总结

| 模式 | 在哪里 | 作用 |
|------|--------|------|
| **策略模式** | 3 种 UI 的 `permission_prompt` 回调 | 同一个引擎代码，不同 UI 提供不同确认方式 |
| **责任链** | `evaluate()` 的 8 级优先级 | 逐级检查，第一个匹配的规则立即返回 |
| **观察者模式** | `/permissions set` → `set_permission_checker()` → 下次调用生效 | 运行时热切换 |
| **代理/委托** | Swarm 权限协调 | Worker 委托 Leader 做权限决策 |
| **适配器** | `SandboxAdapter` | 将应用层配置转为 OS 沙箱配置 |
| **不可变数据** | `PermissionDecision`、`PathRule`、`SandboxAvailability` | frozen dataclass 防篡改 |

---

## 十三、与已学知识的关联

| 已学内容 | 权限系统的角色 |
|---------|---------------|
| **06-Agent 循环** | `_execute_tool_call()` 关卡 4 调用 `permission_checker.evaluate()` |
| **07-engine 包** | `QueryEngine` 持有 `permission_checker` + `permission_prompt` |
| **10-工具系统** | `BaseTool.is_read_only()` 是权限判定的关键输入 |
| **05-前后端协议** | React 弹窗的 `modal_request` / `permission_response` 事件 |
| **12-API 客户端** | 权限拒绝时 `is_error=True` 返回给 LLM，LLM 可以调整策略 |

---

## 核心收获清单

1. **三态决策**：`PermissionDecision` 不是简单的 bool，而是 `(allowed, requires_confirmation, reason)` 三元组，支持"可确认"的中间状态
2. **8 级优先级**：显式规则（黑名单/白名单） > 路径/命令规则 > 模式默认行为，确保用户意图始终优先
3. **策略模式注入**：权限确认回调在 `build_runtime()` 时注入，引擎代码不关心具体 UI 实现
4. **运行时热切换**：`/permissions set` 和 `/plan` 命令实时替换 `PermissionChecker`，无需重启
5. **多层防线**：应用层（PermissionChecker） + OS 层（sandbox/srt） + 分布式层（Swarm 权限协调）
6. **优雅降级**：权限拒绝不抛异常，而是返回 `is_error=True` 的 `ToolResultBlock`，LLM 能看到原因并自行调整

---

*下一步建议：方向 D「命令系统」—— 1374 行的 registry.py，54 个斜杠命令的注册和执行。*

---

*最后更新：2026-04-08*
