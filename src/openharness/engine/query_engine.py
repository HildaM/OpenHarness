"""query_engine.py — 对话管理器（engine 包对外的「门面」）

本文件是 engine 包对上层暴露的唯一接口。上层（handle_line）只和 QueryEngine 交互，
不直接接触 run_query()、messages、compact 等内部实现。

架构角色（门面模式）：
  上层 handle_line() 只调用两个方法：
    engine.submit_message(line)      → 新用户输入 → Agent 循环
    engine.continue_pending()        → /continue → 从中断处继续

  内部委托给 query.py 的 run_query() 执行实际循环

管理的三项核心状态：
  _messages       对话历史（与 run_query 共享引用，循环中直接 append）
  _cost_tracker   Token 用量累加（每轮 TurnComplete 时累加）
  _system_prompt  当前 System Prompt（handle_line 每次输入前更新）

数据流（见 09-why-yield.md）：
  submit_message yield event → handle_line async for → render_event 回调 → 前端
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from openharness.api.client import SupportsStreamingMessages
from openharness.engine.cost_tracker import CostTracker
from openharness.engine.messages import ConversationMessage, ToolResultBlock
from openharness.engine.query import AskUserPrompt, PermissionPrompt, QueryContext, run_query
from openharness.engine.stream_events import StreamEvent
from openharness.hooks import HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry


class QueryEngine:
    """对话引擎——管理历史、追踪成本、调度 Agent 循环。

    外部只需要两个方法：submit_message() 和 continue_pending()，
    它们都返回 AsyncIterator[StreamEvent]，通过 yield 流式传出事件。

    由 build_runtime() 在第 9 步创建，注入所有依赖后贯穿整个会话生命周期。
    """

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,       # LLM API 客户端（3 种实现可互换）
        tool_registry: ToolRegistry,                  # 42+ 工具注册表
        permission_checker: PermissionChecker,        # 权限检查器
        cwd: str | Path,                              # 工作目录
        model: str,                                   # 模型名
        system_prompt: str,                           # System Prompt（8 个片段拼接）
        max_tokens: int = 4096,                       # 单次 LLM 输出的最大 Token
        max_turns: int = 8,                           # 每次用户输入的最大 Agent 轮数
        permission_prompt: PermissionPrompt | None = None,  # 权限确认回调
        ask_user_prompt: AskUserPrompt | None = None,       # 用户提问回调
        hook_executor: HookExecutor | None = None,    # PreToolUse / PostToolUse 钩子
        tool_metadata: dict[str, object] | None = None,  # 传给工具的额外元数据
    ) -> None:
        # ── 注入的依赖（创建后不变，除非通过 set_* 方法更新） ──
        self._api_client = api_client
        self._tool_registry = tool_registry
        self._permission_checker = permission_checker
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._max_turns = max_turns
        self._permission_prompt = permission_prompt
        self._ask_user_prompt = ask_user_prompt
        self._hook_executor = hook_executor
        self._tool_metadata = tool_metadata or {}

        # ── 自有状态（随会话进行而变化） ──
        # _messages 是对话历史，run_query() 接收的是这个列表的引用，
        # 循环中直接 append（assistant 回复 + 工具结果），所以外部自动看到更新
        self._messages: list[ConversationMessage] = []
        # _cost_tracker 累加每轮的 input_tokens + output_tokens
        self._cost_tracker = CostTracker()

    # ═══════════════════════════════════════════════════════════
    # 属性（只读访问）
    # ═══════════════════════════════════════════════════════════

    @property
    def messages(self) -> list[ConversationMessage]:
        """返回对话历史的副本。save_session_snapshot 用此保存到磁盘。"""
        return list(self._messages)

    @property
    def max_turns(self) -> int:
        """返回当前的最大轮数。"""
        return self._max_turns

    @property
    def total_usage(self):
        """返回整个会话的累计 Token 用量。/status 命令读取此值显示给用户。"""
        return self._cost_tracker.total

    # ═══════════════════════════════════════════════════════════
    # 热更新方法（handle_line / 斜杠命令 在运行时调用）
    # ═══════════════════════════════════════════════════════════

    def clear(self) -> None:
        """清空对话历史和成本。/clear 命令触发。"""
        self._messages.clear()
        self._cost_tracker = CostTracker()

    def set_system_prompt(self, prompt: str) -> None:
        """更新 System Prompt。handle_line 每次用户输入前调用。

        为什么每次都更新？因为 System Prompt 包含基于用户输入的记忆检索（片段⑧），
        不同输入会匹配到不同的记忆文件。
        """
        self._system_prompt = prompt

    def set_model(self, model: str) -> None:
        """切换模型。/model 命令触发。"""
        self._model = model

    def set_max_turns(self, max_turns: int) -> None:
        """更新最大轮数。sync_app_state 每次 handle_line 结束后调用。"""
        self._max_turns = max(1, int(max_turns))

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        """切换权限模式。/permissions 命令触发。"""
        self._permission_checker = checker

    def load_messages(self, messages: list[ConversationMessage]) -> None:
        """替换对话历史。恢复会话（oh -c / oh -r）时调用。"""
        self._messages = list(messages)

    # ═══════════════════════════════════════════════════════════
    # 状态检查
    # ═══════════════════════════════════════════════════════════

    def has_pending_continuation(self) -> bool:
        """检查对话是否在「工具执行完但 LLM 还没回复」的状态。

        场景：Agent 循环因 MaxTurnsExceeded 中断 → 最后一条消息是
        user(tool_results) → LLM 还没看到结果 → 用户可以用 /continue 继续。

        判断逻辑：
          1. 最后一条消息是 user 角色？
          2. 包含 ToolResultBlock？
          3. 往前找最近的 assistant 消息有 tool_uses？
          → 三个都满足 = 有待续的循环
        """
        if not self._messages:
            return False
        last = self._messages[-1]
        if last.role != "user":
            return False
        if not any(isinstance(block, ToolResultBlock) for block in last.content):
            return False
        for msg in reversed(self._messages[:-1]):
            if msg.role != "assistant":
                continue
            return bool(msg.tool_uses)
        return False

    # ═══════════════════════════════════════════════════════════
    # 核心方法（上层 handle_line 调用的唯二入口）
    # ═══════════════════════════════════════════════════════════

    async def submit_message(self, prompt: str) -> AsyncIterator[StreamEvent]:
        """处理新的用户输入——追加消息后启动 Agent 循环。

        调用链：
          handle_line → submit_message → run_query → LLM → 工具 → 循环

        与 continue_pending 的唯一区别：这里先 append 用户消息。

        返回 AsyncIterator：通过 yield 流式传出事件，上层用 async for 消费。
        每个 token 从 LLM 产出到 yield 到上层只有几毫秒延迟（零缓冲）。
        """
        # 追加用户消息到历史（run_query 会在同一个列表上继续 append）
        self._messages.append(ConversationMessage.from_user_text(prompt))

        # 打包所有依赖为 QueryContext（避免 run_query 需要 12 个参数）
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
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
        )
        # run_query yield 的是 (StreamEvent, UsageSnapshot|None) 元组
        # 只有 TurnComplete 携带 usage，其他事件 usage=None
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)    # 累加本轮 Token 用量
            yield event                           # 透传事件给上层（handle_line）

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        """从中断处继续 Agent 循环——不追加新用户消息。

        触发方式：用户执行 /continue [N] 命令。

        与 submit_message 的区别：
          submit_message：先 append 用户消息 → run_query
          continue_pending：直接 run_query（历史中已有待处理的 tool_results）

        max_turns 可以覆盖默认值，用户可以指定如 /continue 32 只跑 32 轮。
        """
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
        )
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)
            yield event
