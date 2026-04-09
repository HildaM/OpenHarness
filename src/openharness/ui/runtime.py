"""runtime.py — 共享运行时装配与消息路由

本文件是「外壳层」和「核心层」的分界线，包含两个最重要的函数：
  - build_runtime()  — 装配工厂：创建所有子系统，返回 RuntimeBundle
  - handle_line()    — 消息路由：每次用户输入的统一入口

在架构中的位置：
  上层调用方:
    - app.py run_print_mode()           — 非交互模式直接调用
    - backend_host.py _process_line()   — 交互模式通过回调调用
  下层被调用:
    - engine/query_engine.py            — engine.submit_message() 进入 Agent 循环
    - commands/registry.py              — commands.lookup() 处理斜杠命令
    - prompts/context.py                — build_runtime_system_prompt() 组装 Prompt
    - services/session_storage.py       — save_session_snapshot() 保存会话

数据流:
  正向: handle_line → engine.submit_message → run_query → LLM API + 工具
  反向: run_query yield → submit_message yield → handle_line async for → render_event 回调
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from openharness.api.client import AnthropicApiClient, SupportsStreamingMessages
from openharness.api.copilot_client import CopilotClient
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.provider import auth_status, detect_provider
from openharness.bridge import get_bridge_manager
from openharness.commands import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    create_default_command_registry,
)
from openharness.config import get_config_file_path, load_settings
from openharness.config.settings import PermissionSettings
from openharness.coordinator.coordinator_mode import (
    TaskNotification,
    format_task_notification,
    get_coordinator_tools,
    is_coordinator_mode,
)
from openharness.engine import QueryEngine
from openharness.engine.messages import ConversationMessage, ToolResultBlock, ToolUseBlock
from openharness.engine.query import MaxTurnsExceeded
from openharness.engine.stream_events import StreamEvent
from openharness.hooks import HookEvent, HookExecutionContext, HookExecutor, load_hook_registry
from openharness.hooks.hot_reload import HookReloader
from openharness.mcp.client import McpClientManager
from openharness.mcp.config import load_mcp_server_configs
from openharness.permissions import PermissionChecker
from openharness.plugins import load_plugins
from openharness.prompts import build_runtime_system_prompt
from openharness.state import AppState, AppStateStore
from openharness.services.session_storage import save_session_snapshot
from openharness.swarm.mailbox import MailboxMessage, TeammateMailbox
from openharness.tools import ToolRegistry, create_default_tool_registry
from openharness.keybindings import load_keybindings

# ── 回调函数类型别名 ──
# 这些类型定义了 handle_line() 的 3 个渲染回调 + 2 个引擎注入回调
# 不同模式提供不同实现：
#   交互模式: backend_host.py 的 _emit 系列方法
#   非交互模式: app.py 的 _print_system / _render_event / _noop_permission
PermissionPrompt = Callable[[str, str], Awaitable[bool]]    # 权限确认：(tool_name, reason) → 允许?
AskUserPrompt = Callable[[str], Awaitable[str]]             # 用户提问：(question) → 回答
SystemPrinter = Callable[[str], Awaitable[None]]            # 系统消息渲染
StreamRenderer = Callable[[StreamEvent], Awaitable[None]]   # 流事件渲染（4 种 StreamEvent）
ClearHandler = Callable[[], Awaitable[None]]                # 清屏处理


@dataclass
class RuntimeBundle:
    """整个会话的运行时上下文——所有子系统的容器。

    由 build_runtime() 创建，贯穿整个会话生命周期。
    handle_line() 在每次用户输入时使用它来访问所有子系统。

    组件分为 3 类：
      核心引擎:  engine（对话管理 + Agent 循环）
      基础设施:  api_client, tool_registry, mcp_manager, hook_executor, app_state
      辅助功能:  commands（斜杠命令注册表）

    方法都是「实时查询」——每次调用都重新读取配置/插件，支持热更新。
    """

    api_client: SupportsStreamingMessages
    cwd: str
    mcp_manager: McpClientManager
    tool_registry: ToolRegistry
    app_state: AppStateStore
    hook_executor: HookExecutor
    engine: QueryEngine
    commands: CommandRegistry
    external_api_client: bool
    session_id: str = ""

    def current_settings(self):
        """Return the latest persisted settings."""
        return load_settings()

    def current_plugins(self):
        """Return currently visible plugins for the working tree."""
        return load_plugins(self.current_settings(), self.cwd)

    def hook_summary(self) -> str:
        """Return the current hook summary."""
        return load_hook_registry(self.current_settings(), self.current_plugins()).summary()

    def plugin_summary(self) -> str:
        """Return the current plugin summary."""
        plugins = self.current_plugins()
        if not plugins:
            return "No plugins discovered."
        lines = ["Plugins:"]
        for plugin in plugins:
            state = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.manifest.name} [{state}] {plugin.manifest.description}")
        return "\n".join(lines)

    def mcp_summary(self) -> str:
        """Return the current MCP summary."""
        statuses = self.mcp_manager.list_statuses()
        if not statuses:
            return "No MCP servers configured."
        lines = ["MCP servers:"]
        for status in statuses:
            suffix = f" - {status.detail}" if status.detail else ""
            lines.append(f"- {status.name}: {status.state}{suffix}")
            if status.tools:
                lines.append(f"  tools: {', '.join(tool.name for tool in status.tools)}")
            if status.resources:
                lines.append(f"  resources: {', '.join(resource.uri for resource in status.resources)}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# build_runtime() — 整个系统的「装配工厂」
#
# 这是项目中最重要的函数之一。它按照依赖顺序创建所有子系统，
# 并将它们组装成一个 RuntimeBundle，供整个会话生命周期使用。
#
# 装配顺序（后面的依赖前面的）：
#   settings → plugins → api_client → mcp_manager → tool_registry
#   → provider → app_state → hook_executor → engine → RuntimeBundle
#
# 调用方：
#   - run_print_mode()（非交互模式）
#   - ReactBackendHost.run()（交互模式后端）
# ═══════════════════════════════════════════════════════════════════════════
async def build_runtime(
    *,
    # prompt: 用户的初始输入（用于 System Prompt 中的记忆检索）
    prompt: str | None = None,
    # model ~ api_format: CLI 参数透传，非 None 时覆盖配置文件值
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    # api_client: 外部注入的 API 客户端（用于单元测试 mock，生产环境为 None）
    api_client: SupportsStreamingMessages | None = None,
    # permission_prompt: 权限确认回调（交互模式=弹窗，非交互模式=自动放行）
    permission_prompt: PermissionPrompt | None = None,
    # ask_user_prompt: 向用户提问的回调（交互模式=输入框，非交互模式=返回空）
    ask_user_prompt: AskUserPrompt | None = None,
    # restore_messages: 恢复会话时传入的历史消息（oh -c / oh -r）
    restore_messages: list[dict] | None = None,
) -> RuntimeBundle:
    """Build the shared runtime for an OpenHarness session."""

    # ──── 第 1 步：加载配置 ────
    # load_settings(): 从 ~/.openharness/settings.json 读取，叠加环境变量覆盖
    # merge_cli_overrides(): 用 CLI 参数覆盖配置文件值（非 None 的才覆盖）
    # 最终优先级：CLI 参数 > 环境变量 > settings.json > 代码默认值
    settings = load_settings().merge_cli_overrides(
        model=model,
        max_turns=max_turns,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
    )

    # ──── 第 2 步：加载插件 ────
    # 扫描 ~/.openharness/plugins/ 和 .openharness/plugins/ 目录
    # 兼容 claude-code 插件格式（plugin.json 或 .claude-plugin/plugin.json）
    cwd = str(Path.cwd())
    plugins = load_plugins(settings, cwd)

    # [DEBUG] 提前构建 System Prompt 并写入文件，方便学习和调试
    # 放在 API 客户端创建之前，这样即使没有 API Key 也能看到完整 Prompt
    # 运行后查看: cat debug_system_prompt.md
    # 不再需要时删除这段即可
    _debug_prompt = build_runtime_system_prompt(settings, cwd=cwd, latest_user_prompt=prompt)
    (Path(cwd) / "debug_system_prompt.md").write_text(
        f"# System Prompt Debug Output\n"
        f"# User prompt: {prompt!r}\n"
        f"# Model: {settings.model}\n"
        f"# Length: {len(_debug_prompt)} chars\n"
        f"# {'=' * 60}\n\n"
        f"{_debug_prompt}\n",
        encoding="utf-8",
    )

    # ──── 第 3 步：创建 API 客户端 ────
    # 根据 api_format 选择对应的客户端实现
    # 三种客户端都实现 SupportsStreamingMessages Protocol，后续代码完全不关心具体类型
    if api_client:
        # 外部注入（测试用）：直接使用
        resolved_api_client = api_client
    elif settings.api_format == "copilot":
        # GitHub Copilot：OAuth 认证，基于 OpenAI 兼容客户端封装
        from openharness.api.copilot_client import COPILOT_DEFAULT_MODEL
        copilot_model = settings.model if settings.model != "claude-sonnet-4-20250514" else COPILOT_DEFAULT_MODEL
        resolved_api_client = CopilotClient(model=copilot_model)
    elif settings.api_format == "openai":
        # OpenAI 兼容格式：DashScope、DeepSeek、Groq、Ollama 等
        resolved_api_client = OpenAICompatibleClient(
            api_key=settings.resolve_api_key(),
            base_url=settings.base_url,
        )
    else:
        # 默认 Anthropic 格式：Claude 原生、Kimi/Moonshot、Vertex、Bedrock
        resolved_api_client = AnthropicApiClient(
            api_key=settings.resolve_api_key(),
            base_url=settings.base_url,
        )

    # ──── 第 4 步：连接 MCP 服务器 ────
    # 合并 settings.mcp_servers + 插件中的 MCP 配置
    # 异步连接所有配置的 MCP 服务器（Stdio / HTTP / WebSocket）
    mcp_manager = McpClientManager(load_mcp_server_configs(settings, plugins))
    await mcp_manager.connect_all()

    # ──── 第 5 步：创建工具注册表 ────
    # 注册 36 个内置工具（Bash, FileRead, Grep, WebFetch, Agent, ...）
    # 如果有 MCP 服务器，为每个 MCP 工具创建 McpToolAdapter 并注册
    # 最终 registry 包含所有可用工具，工具定义（JSON Schema）会发送给 LLM
    full_tool_registry = create_default_tool_registry(mcp_manager)
    tool_registry = (
        full_tool_registry.filtered(allow=get_coordinator_tools())
        if is_coordinator_mode()
        else full_tool_registry
    )

    # ──── 第 6 步：检测 Provider 信息 ────
    # 根据 base_url 和 model 名自动识别当前 Provider（如 moonshot、dashscope）
    # 仅用于 UI 显示，不影响客户端行为
    provider = detect_provider(settings)

    # ──── 第 7 步：初始化应用状态 ────
    # AppStateStore 持有所有 UI 需要展示的状态信息
    # 前端通过 state_snapshot 事件获取这些信息用于渲染 StatusBar 等
    bridge_manager = get_bridge_manager()
    app_state = AppStateStore(
        AppState(
            model=settings.model,
            permission_mode=settings.permission.mode.value,
            theme=settings.theme,
            cwd=cwd,
            provider=provider.name,
            auth_status=auth_status(settings),
            base_url=settings.base_url or "",
            vim_enabled=settings.vim_mode,
            voice_enabled=settings.voice_mode,
            voice_available=provider.voice_supported,
            voice_reason=provider.voice_reason,
            fast_mode=settings.fast_mode,
            effort=settings.effort,
            passes=settings.passes,
            mcp_connected=sum(1 for status in mcp_manager.list_statuses() if status.state == "connected"),
            mcp_failed=sum(1 for status in mcp_manager.list_statuses() if status.state == "failed"),
            bridge_sessions=len(bridge_manager.list_sessions()),
            output_style=settings.output_style,
            keybindings=load_keybindings(),
        )
    )

    # ──── 第 8 步：创建 Hook 执行器 ────
    # HookReloader: 监听 settings.json 文件变更，支持热重载钩子
    # HookExecutor: 在工具执行前后触发 PreToolUse / PostToolUse 钩子
    # 如果是外部注入的 api_client（测试场景），跳过热重载直接加载
    hook_reloader = HookReloader(get_config_file_path())
    hook_executor = HookExecutor(
        hook_reloader.current_registry() if api_client is None else load_hook_registry(settings, plugins),
        HookExecutionContext(
            cwd=Path(cwd).resolve(),
            api_client=resolved_api_client,
            default_model=settings.model,
        ),
    )

    # ──── 第 9 步：创建 QueryEngine（对话引擎，核心中的核心） ────
    # QueryEngine 管理对话历史、调度 Agent 循环（run_query）
    # build_runtime_system_prompt() 组装 8 个片段的 System Prompt:
    #   ① 角色定义 ② 环境信息 ③ Fast Mode ④ Effort/Passes
    #   ⑤ 技能列表 ⑥ CLAUDE.md ⑦ Issue/PR 上下文 ⑧ 记忆检索
    _system_prompt = _debug_prompt  # 复用前面 debug 阶段已构建的 Prompt
    permission_settings = (
        _extend_allowed_tools(settings.permission, get_coordinator_tools())
        if is_coordinator_mode()
        else settings.permission
    )
    permission_checker = PermissionChecker(permission_settings)

    engine = QueryEngine(
        api_client=resolved_api_client,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=cwd,
        model=settings.model,
        system_prompt=_system_prompt,
        max_tokens=settings.max_tokens,
        max_turns=settings.max_turns,
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        hook_executor=hook_executor,
        # tool_metadata 通过 ToolExecutionContext 传递给工具，
        # 让工具能访问运行时依赖，并在 in-process worker 中复用完整上下文
        tool_metadata={
            "mcp_manager": mcp_manager,
            "bridge_manager": bridge_manager,
            "full_tool_registry": full_tool_registry,
            "api_client": resolved_api_client,
            "permission_checker": permission_checker,
            "permission_settings": permission_settings,
            "permission_prompt": permission_prompt,
            "ask_user_prompt": ask_user_prompt,
            "hook_executor": hook_executor,
            "system_prompt": _system_prompt,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "max_turns": settings.max_turns,
            "session_id": "main",
            "tool_metadata": {"mcp_manager": mcp_manager, "bridge_manager": bridge_manager},
        },
    )

    # ──── 第 10 步：恢复会话历史（可选） ────
    # 当用户使用 oh -c / oh -r 恢复会话时，将历史消息加载到引擎中
    # model_validate 将 JSON dict 反序列化为 ConversationMessage 对象
    if restore_messages:
        restored = [
            ConversationMessage.model_validate(m) for m in restore_messages
        ]
        engine.load_messages(restored)

    # ──── 第 11 步：打包所有组件为 RuntimeBundle ────
    from uuid import uuid4

    return RuntimeBundle(
        api_client=resolved_api_client,
        cwd=cwd,
        mcp_manager=mcp_manager,
        tool_registry=tool_registry,
        app_state=app_state,
        hook_executor=hook_executor,
        engine=engine,
        # create_default_command_registry() 注册 54 个斜杠命令（/help, /model, /clear, ...）
        commands=create_default_command_registry(),
        # external_api_client 标记是否外部注入，影响 Hook 热重载行为
        external_api_client=api_client is not None,
        # 12 位随机 hex 作为会话 ID，用于会话快照文件命名
        session_id=uuid4().hex[:12],
    )


# ═══════════════════════════════════════════════════════════════════════════
# 生命周期函数
# ═══════════════════════════════════════════════════════════════════════════

async def start_runtime(bundle: RuntimeBundle) -> None:
    """触发 SESSION_START 钩子。在 build_runtime 之后、第一次 handle_line 之前调用。"""
    await bundle.hook_executor.execute(
        HookEvent.SESSION_START,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_START.value},
    )


async def close_runtime(bundle: RuntimeBundle) -> None:
    """关闭运行时资源。在会话结束时调用（无论正常退出还是异常）。

    交互模式: backend_host.py run() 的 finally 块调用
    非交互模式: app.py run_print_mode() 的 finally 块调用
    """
    from openharness.swarm.registry import get_backend_registry
    from openharness.swarm.team_lifecycle import cleanup_session_teams

    registry = get_backend_registry()
    try:
        with contextlib.suppress(Exception):
            await cleanup_session_teams()
        with contextlib.suppress(Exception):
            await registry.shutdown_all(force=True, timeout=2.0)
    finally:
        registry.reset()

    await bundle.mcp_manager.close()     # 关闭所有 MCP 服务器连接
    await bundle.hook_executor.execute(
        HookEvent.SESSION_END,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_END.value},
    )


# ═══════════════════════════════════════════════════════════════════════════
# 辅助函数（handle_line 内部使用）
# ═══════════════════════════════════════════════════════════════════════════

def _last_user_text(messages: list[ConversationMessage]) -> str:
    """从历史消息中找到最后一条用户文本（用于 /continue 时重建 System Prompt 的记忆检索）。"""
    for msg in reversed(messages):
        if msg.role == "user" and msg.text.strip():
            return msg.text.strip()
    return ""


def _extend_allowed_tools(
    permission_settings: PermissionSettings,
    tool_names: list[str],
) -> PermissionSettings:
    """Return a copy of permission settings with extra auto-allowed tools."""
    merged = list(dict.fromkeys([*permission_settings.allowed_tools, *tool_names]))
    return permission_settings.model_copy(update={"allowed_tools": merged})


def _truncate(text: str, limit: int) -> str:
    """截断文本到指定长度，超长时加省略号。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _format_pending_tool_results(messages: list[ConversationMessage]) -> str | None:
    """当 Agent 循环因 MaxTurnsExceeded 中断时，生成「未完成」提示信息。

    场景：Agent 执行了工具但还没来得及让 LLM 看结果就达到了 max_turns 限制。
    此时最后一条消息是 user(tool_results)，但 LLM 还没有回复。
    提示用户可以用 /continue 命令继续。
    """
    if not messages:
        return None

    last = messages[-1]
    if last.role != "user":
        return None
    tool_results = [block for block in last.content if isinstance(block, ToolResultBlock)]
    if not tool_results:
        return None

    tool_uses_by_id: dict[str, ToolUseBlock] = {}
    assistant_text = ""
    for msg in reversed(messages[:-1]):
        if msg.role != "assistant":
            continue
        if not msg.tool_uses:
            continue
        assistant_text = msg.text.strip()
        for tu in msg.tool_uses:
            tool_uses_by_id[tu.id] = tu
        break

    lines: list[str] = [
        "Pending continuation: tool results were produced, but the model did not get a chance to respond yet."
    ]
    if assistant_text:
        lines.append(f"Last assistant message: {_truncate(assistant_text, 400)}")

    max_results = 3
    for tr in tool_results[:max_results]:
        tu = tool_uses_by_id.get(tr.tool_use_id)
        if tu is not None:
            raw_input = json.dumps(tu.input, ensure_ascii=True, sort_keys=True)
            lines.append(
                f"- {tu.name} {_truncate(raw_input, 200)} -> {_truncate(tr.content.strip(), 400)}"
            )
        else:
            lines.append(
                f"- tool_result[{tr.tool_use_id}] -> {_truncate(tr.content.strip(), 400)}"
            )

    if len(tool_results) > max_results:
        lines.append(f"(+{len(tool_results) - max_results} more tool results)")

    lines.append("To continue from these results, run: /continue 32 (or any count).")
    return "\n".join(lines)


def sync_app_state(bundle: RuntimeBundle) -> None:
    """重新读取配置并同步到引擎和 UI 状态。

    在每次 handle_line 结束后调用。
    作用：如果用户通过 /model 等命令修改了 settings.json，
    这里会把最新值同步到 engine（max_turns）和 app_state（前端 StatusBar 显示）。
    """
    settings = bundle.current_settings()
    bundle.engine.set_max_turns(settings.max_turns)
    provider = detect_provider(settings)
    bundle.app_state.set(
        model=settings.model,
        permission_mode=settings.permission.mode.value,
        theme=settings.theme,
        cwd=bundle.cwd,
        provider=provider.name,
        auth_status=auth_status(settings),
        base_url=settings.base_url or "",
        vim_enabled=settings.vim_mode,
        voice_enabled=settings.voice_mode,
        voice_available=provider.voice_supported,
        voice_reason=provider.voice_reason,
        fast_mode=settings.fast_mode,
        effort=settings.effort,
        passes=settings.passes,
        mcp_connected=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "connected"),
        mcp_failed=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "failed"),
        bridge_sessions=len(get_bridge_manager().list_sessions()),
        output_style=settings.output_style,
        keybindings=load_keybindings(),
    )


async def read_leader_notifications(
    *,
    team_name: str = "default",
) -> tuple[TeammateMailbox, list[MailboxMessage]]:
    """Read unread leader mailbox messages without consuming them."""
    mailbox = TeammateMailbox(team_name=team_name, agent_id="leader")
    messages = await mailbox.read_all(unread_only=True)
    return mailbox, [msg for msg in messages if msg.type == "idle_notification"]


def mailbox_message_to_task_notification(msg: MailboxMessage) -> str:
    """Translate a leader mailbox message into canonical coordinator XML."""
    payload = msg.payload if isinstance(msg.payload, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    notification = TaskNotification(
        task_id=msg.sender,
        status=str(payload.get("status") or "completed"),
        summary=str(payload.get("summary") or f"{msg.sender} finished"),
        result=str(payload.get("result")) if payload.get("result") is not None else None,
        usage={str(k): int(v) for k, v in usage.items()} if usage else None,
    )
    return format_task_notification(notification)


# ═══════════════════════════════════════════════════════════════════════════
# handle_line() — 每次用户输入的统一入口
#
# 这是「外壳层」调用「核心层」的唯一通道：
#   上层（backend_host / app.py）→ handle_line → engine.submit_message → run_query
#
# 数据流：
#   正向: line → engine.submit_message(line) → run_query → LLM → 工具
#   反向: run_query yield event → submit_message yield → async for → render_event 回调
#
# 两条分支：
#   "/" 开头 → 斜杠命令（commands.lookup → command.handler）
#   其他    → Agent 循环（engine.submit_message → run_query）
# ═══════════════════════════════════════════════════════════════════════════
async def handle_line(
    bundle: RuntimeBundle,
    line: str,
    *,
    # 这 3 个回调由上层提供，实现「策略模式」——同一份逻辑，不同的渲染方式
    # 交互模式: backend_host._print_system / _render_event / _clear_output（发 JSON 给前端）
    # 非交互模式: app.py 的回调（写到 stdout/stderr）
    print_system: SystemPrinter,
    render_event: StreamRenderer,
    clear_output: ClearHandler,
) -> bool:
    """处理一行用户输入。返回 True 继续接受输入，False 表示退出（/exit）。"""

    # 每次处理前热重载 Hook 注册表（支持用户修改 settings.json 后立即生效）
    if not bundle.external_api_client:
        bundle.hook_executor.update_registry(
            load_hook_registry(bundle.current_settings(), bundle.current_plugins())
        )

    # ──── 分支 1：斜杠命令（如 /help, /model, /clear, /exit, /continue）────
    parsed = bundle.commands.lookup(line)
    if parsed is not None:
        command, args = parsed
        # 执行命令处理器（每个命令在 commands/registry.py 中定义）
        result = await command.handler(
            args,
            CommandContext(
                engine=bundle.engine,
                hooks_summary=bundle.hook_summary(),
                mcp_summary=bundle.mcp_summary(),
                plugin_summary=bundle.plugin_summary(),
                cwd=bundle.cwd,
                tool_registry=bundle.tool_registry,
                app_state=bundle.app_state,
            ),
        )
        # 渲染命令结果（显示消息、清屏、回放会话等）
        await _render_command_result(result, print_system, clear_output, render_event)

        # 特殊情况：/continue 命令会设置 continue_pending=True
        # 这时需要继续之前中断的 Agent 循环（不追加新用户消息）
        if result.continue_pending:
            # 加载与更新system prompt
            settings = bundle.current_settings()
            bundle.engine.set_max_turns(settings.max_turns)
            system_prompt = build_runtime_system_prompt(
                settings,
                cwd=bundle.cwd,
                latest_user_prompt=_last_user_text(bundle.engine.messages),
            )
            bundle.engine.set_system_prompt(system_prompt)

            # 从上次中断处继续 Agent 循环
            turns = result.continue_turns if result.continue_turns is not None else bundle.engine.max_turns
            try:
                # engine.continue_pending() 不追加新消息，从上次中断处继续 Agent 循环
                async for event in bundle.engine.continue_pending(max_turns=turns):
                    await render_event(event)
            except MaxTurnsExceeded as exc:
                await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
                pending = _format_pending_tool_results(bundle.engine.messages)
                if pending:
                    await print_system(pending)

            # 保存会话快照
            save_session_snapshot(
                cwd=bundle.cwd,
                model=settings.model,
                system_prompt=system_prompt,
                messages=bundle.engine.messages,
                usage=bundle.engine.total_usage,
                session_id=bundle.session_id,
            )
        sync_app_state(bundle)
        return not result.should_exit   # /exit 命令 → should_exit=True → 返回 False

    # ──── 分支 2：普通消息 → 进入 Agent 循环 ────
    # 每次用户输入都重新读取配置 + 重建 System Prompt
    # 原因：用户可能通过 /model 等命令修改了配置，且记忆检索依赖用户输入内容
    settings = bundle.current_settings()
    bundle.engine.set_max_turns(settings.max_turns)
    system_prompt = build_runtime_system_prompt(settings, cwd=bundle.cwd, latest_user_prompt=line)
    bundle.engine.set_system_prompt(system_prompt)

    try:
        # 核心调用：提交用户消息给 engine，触发 Agent 循环（LLM → 工具 → LLM → ...）
        # engine.submit_message 是 AsyncIterator，通过 yield 流式产出事件
        # 每个事件立即通过 render_event 回调传给上层（前端 or stdout）
        async for event in bundle.engine.submit_message(line):
            await render_event(event)
    except MaxTurnsExceeded as exc:
        # Agent 循环超过最大轮数限制 → 提示用户，但不崩溃
        await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
        pending = _format_pending_tool_results(bundle.engine.messages)
        if pending:
            await print_system(pending)    # 提示用户可以用 /continue 继续
        save_session_snapshot(
            cwd=bundle.cwd,
            model=settings.model,
            system_prompt=system_prompt,
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
        )
        sync_app_state(bundle)
        return True    # 即使超限也继续接受输入

    # 正常完成 → 自动保存会话快照 + 刷新 UI 状态
    save_session_snapshot(
        cwd=bundle.cwd,
        model=settings.model,
        system_prompt=system_prompt,
        messages=bundle.engine.messages,
        usage=bundle.engine.total_usage,
        session_id=bundle.session_id,
    )
    sync_app_state(bundle)
    return True


async def _render_command_result(
    result: CommandResult,
    print_system: SystemPrinter,
    clear_output: ClearHandler,
    render_event: StreamRenderer | None = None,
) -> None:
    """渲染斜杠命令的执行结果。

    CommandResult 有多个可选字段，按优先级处理：
      clear_screen    → 先清屏（/clear 命令）
      replay_messages → 回放恢复的会话历史（/resume 命令）
      message         → 显示文本消息（大部分命令）
    """
    if result.clear_screen:
        await clear_output()
    if result.replay_messages and render_event is not None:
        # Replay restored conversation messages as transcript events
        from openharness.engine.stream_events import AssistantTextDelta, AssistantTurnComplete
        from openharness.api.usage import UsageSnapshot

        await clear_output()
        await print_system("Session restored:")
        for msg in result.replay_messages:
            if msg.role == "user":
                await print_system(f"> {msg.text}")
            elif msg.role == "assistant" and msg.text.strip():
                await render_event(AssistantTextDelta(text=msg.text))
                await render_event(AssistantTurnComplete(message=msg, usage=UsageSnapshot()))
    if result.message and not result.replay_messages:
        await print_system(result.message)
