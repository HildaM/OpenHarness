"""query.py — Agent 循环核心（整个项目最重要的文件）

本文件包含 3 个组件：
  - MaxTurnsExceeded   异常类，循环超限时抛出
  - QueryContext       一次循环所需的所有依赖的打包
  - run_query()        Agent 循环本体
  - _execute_tool_call() 单个工具的 6 道关卡执行流水线

调用关系：
  query_engine.py submit_message()
      → run_query(context, messages)      ← 本文件
          → api_client.stream_message()   ← 调 LLM
          → _execute_tool_call()          ← 执行工具
              → tool.execute()            ← 具体工具实现
              → permission_prompt()       ← 反向回调到 backend_host

数据流：
  正向: messages → api_client → LLM → assistant_msg → tool → tool_result → messages（循环）
  反向: yield StreamEvent → query_engine yield → handle_line async for → render_event 回调
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiTextDeltaEvent,
    SupportsStreamingMessages,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ToolResultBlock
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolExecutionContext
from openharness.tools.base import ToolRegistry


# ── 回调类型别名 ──
# 这两个回调在 build_runtime() 时注入到 QueryContext：
#   交互模式 → backend_host._ask_permission / _ask_question（弹窗等待用户）
#   非交互模式 → app.py _noop_permission / _noop_ask（自动放行）
PermissionPrompt = Callable[[str, str], Awaitable[bool]]    # (tool_name, reason) → 允许?
AskUserPrompt = Callable[[str], Awaitable[str]]             # (question) → 用户回答


class MaxTurnsExceeded(RuntimeError):
    """Agent 循环超过最大轮数限制时抛出。

    由 run_query() 在 for 循环耗尽后抛出，
    被 handle_line() 捕获并提示用户可以用 /continue 继续。
    """

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"Exceeded maximum turn limit ({max_turns})")
        self.max_turns = max_turns


# ═══════════════════════════════════════════════════════════════
# QueryContext — 一次 Agent 循环所需的全部依赖
#
# 为什么打包成 dataclass 而非逐个传参？
# 因为 _execute_tool_call() 需要访问 api_client、tool_registry、
# permission_checker、hook_executor、permission_prompt 等大部分字段，
# 逐个传参会有 10+ 个参数，非常冗长。
# ═══════════════════════════════════════════════════════════════
@dataclass
class QueryContext:
    """Context shared across a query run."""

    api_client: SupportsStreamingMessages       # LLM API 客户端（Anthropic/OpenAI/Copilot）
    tool_registry: ToolRegistry                  # 42+ 工具的注册表
    permission_checker: PermissionChecker        # 权限检查器（3 级模式 + 路径/命令规则）
    cwd: Path                                    # 当前工作目录
    model: str                                   # 模型名（如 "claude-sonnet-4-20250514"）
    system_prompt: str                           # 完整的 System Prompt（8 个片段拼接）
    max_tokens: int                              # 单次 LLM 调用的最大输出 Token
    permission_prompt: PermissionPrompt | None = None  # 权限确认回调（可能触发前端弹窗）
    ask_user_prompt: AskUserPrompt | None = None       # 用户提问回调
    max_turns: int = 200                         # 最大循环轮数（默认 200）
    hook_executor: HookExecutor | None = None    # PreToolUse / PostToolUse 钩子执行器
    tool_metadata: dict[str, object] | None = None  # 额外元数据（mcp_manager 等，传给工具）


# ═══════════════════════════════════════════════════════════════
# run_query() — Agent 循环本体
#
# 这是整个项目最核心的函数（~90 行有效代码）。
#
# 循环结构：
#   for turn in range(max_turns):
#       A. auto_compact_if_needed()  → Token 超阈值(167K)？压缩！
#       B. api_client.stream_message() → 调 LLM（流式返回）
#          yield TextDelta × N        → 每个 token 立即传给上层
#          yield TurnComplete         → 回合结束
#       C. tool_uses 为空？ → return  → 循环结束
#       D. 执行工具
#          单个：顺序执行，立即 yield 事件
#          多个：asyncio.gather 并发，之后 yield 事件
#          messages.append(tool_results)
#       → 回到 A
#
# 关键设计：
#   - messages 是 query_engine._messages 的引用，直接 append 修改
#   - yield 的元组 (StreamEvent, UsageSnapshot|None)，只有 TurnComplete 携带 usage
#   - 函数是 AsyncIterator，上层通过 async for 消费事件
# ═══════════════════════════════════════════════════════════════
async def run_query(
    context: QueryContext,
    messages: list[ConversationMessage],
) -> AsyncIterator[tuple[StreamEvent, UsageSnapshot | None]]:
    """Run the conversation loop until the model stops requesting tools.

    Auto-compaction is checked at the start of each turn.  When the
    estimated token count exceeds the model's auto-compact threshold,
    the engine first tries a cheap microcompact (clearing old tool result
    content) and, if that is not enough, performs a full LLM-based
    summarization of older messages.
    """
    # 延迟导入：compact 模块较大（493 行），只在实际执行时加载
    from openharness.services.compact import (
        AutoCompactState,
        auto_compact_if_needed,
    )

    # 压缩状态：跟踪是否已压缩、连续失败次数（最多 3 次后放弃）
    compact_state = AutoCompactState()

    for _ in range(context.max_turns):

        # ════ 阶段 A：自动压缩检查 ════
        # 每轮开始前估算 messages 的 Token 量，超过 167K 时触发：
        #   第一级 microcompact：清除旧工具结果内容（免费，不调 LLM）
        #   第二级 full compact：调 LLM 生成结构化摘要（消耗 Token，但大幅缩减历史）
        messages, was_compacted = await auto_compact_if_needed(
            messages,
            api_client=context.api_client,
            model=context.model,
            system_prompt=context.system_prompt,
            state=compact_state,
        )

        # ════ 阶段 B：调用 LLM API（流式） ════
        final_message: ConversationMessage | None = None
        usage = UsageSnapshot()

        # stream_message 是 AsyncIterator，每收到一个 token 就 yield 一个事件
        # 发送给 LLM 的内容包括：model + messages(对话历史) + system_prompt + tools(42个工具定义)
        async for event in context.api_client.stream_message(
            ApiMessageRequest(
                model=context.model,
                messages=messages,
                system_prompt=context.system_prompt,
                max_tokens=context.max_tokens,
                tools=context.tool_registry.to_api_schema(),  # 所有工具的 JSON Schema
            )
        ):
            if isinstance(event, ApiTextDeltaEvent):
                # 模型产出了一个 token → 立即 yield 给上层（零缓冲流式传输）
                yield AssistantTextDelta(text=event.text), None
                continue

            if isinstance(event, ApiMessageCompleteEvent):
                # 流结束，拿到完整的 assistant 消息（可能包含 TextBlock + ToolUseBlock）
                final_message = event.message
                usage = event.usage  # 本轮的 input_tokens + output_tokens

        if final_message is None:
            raise RuntimeError("Model stream finished without a final message")

        # 将模型回复追加到对话历史（直接修改 query_engine._messages 引用）
        messages.append(final_message)
        # 通知上层「回合结束」，携带 usage 供 CostTracker 累加
        yield AssistantTurnComplete(message=final_message, usage=usage), usage

        # ════ 阶段 C：检查是否需要工具调用 ════
        if not final_message.tool_uses:
            return  # 模型没有请求工具 → Agent 循环正常结束 ✓

        # ════ 阶段 D：执行工具 ════
        tool_calls = final_message.tool_uses

        if len(tool_calls) == 1:
            # 单工具：顺序执行，可以立即 yield Started 事件（前端立刻显示 spinner）
            tc = tool_calls[0]
            yield ToolExecutionStarted(tool_name=tc.name, tool_input=tc.input), None
            result = await _execute_tool_call(context, tc.name, tc.id, tc.input)
            yield ToolExecutionCompleted(
                tool_name=tc.name,
                output=result.content,
                is_error=result.is_error,
            ), None
            tool_results = [result]
        else:
            # 多工具：并发执行（asyncio.gather）
            # 先 yield 所有 Started（前端同时显示多个 spinner）
            for tc in tool_calls:
                yield ToolExecutionStarted(tool_name=tc.name, tool_input=tc.input), None

            # 并发执行所有工具
            async def _run(tc):
                return await _execute_tool_call(context, tc.name, tc.id, tc.input)

            results = await asyncio.gather(*[_run(tc) for tc in tool_calls])
            tool_results = list(results)

            # 全部完成后再 yield Completed 事件
            for tc, result in zip(tool_calls, tool_results):
                yield ToolExecutionCompleted(
                    tool_name=tc.name,
                    output=result.content,
                    is_error=result.is_error,
                ), None

        # 工具结果以 role="user" 追加到历史
        # 原因：Anthropic API 要求 user/assistant 严格交替，工具结果属于「用户侧反馈」
        messages.append(ConversationMessage(role="user", content=tool_results))

        # → 回到阶段 A，开始下一轮

    # for 循环耗尽 → 超过最大轮数
    raise MaxTurnsExceeded(context.max_turns)


# ═══════════════════════════════════════════════════════════════
# _execute_tool_call() — 单个工具的 6 道关卡执行流水线
#
# 每个关卡失败都返回 ToolResultBlock(is_error=True)，不抛异常。
# 这样 LLM 能看到错误信息并自行调整策略（换工具/换参数/放弃）。
#
# 关卡顺序：
#   1. PreToolUse Hook   → 插件/钩子可以阻止执行
#   2. 工具查找           → 工具名不存在则报错
#   3. 输入验证           → Pydantic model_validate 校验参数
#   4. 权限检查           → 3 级模式 + 路径规则 + 命令拒绝模式
#   5. 实际执行           → tool.execute(parsed_input, context)
#   6. PostToolUse Hook  → 通知插件/钩子执行结果
# ═══════════════════════════════════════════════════════════════
async def _execute_tool_call(
    context: QueryContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:

    # ──── 关卡 1：PreToolUse Hook ────
    # 插件可以注册 PreToolUse 钩子来拦截工具调用
    # 例如 security-guidance 插件会在修改敏感文件时发出警告
    if context.hook_executor is not None:
        pre_hooks = await context.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {"tool_name": tool_name, "tool_input": tool_input, "event": HookEvent.PRE_TOOL_USE.value},
        )
        if pre_hooks.blocked:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=pre_hooks.reason or f"pre_tool_use hook blocked {tool_name}",
                is_error=True,
            )

    # ──── 关卡 2：工具查找 ────
    # 从 ToolRegistry 中按名称查找（42 个内置 + MCP 动态工具）
    tool = context.tool_registry.get(tool_name)
    if tool is None:
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Unknown tool: {tool_name}",
            is_error=True,
        )

    # ──── 关卡 3：Pydantic 输入验证 ────
    # 每个工具定义了 input_model（Pydantic BaseModel），自动校验参数类型和必填字段
    try:
        parsed_input = tool.input_model.model_validate(tool_input)
    except Exception as exc:
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Invalid input for {tool_name}: {exc}",
            is_error=True,
        )

    # ──── 关卡 4：权限检查 ────
    # 检查优先级（见 permissions/checker.py）：
    #   工具黑名单 → 工具白名单 → 路径拒绝规则 → 命令拒绝模式
    #   → FULL_AUTO 放行 → 只读放行 → PLAN 拒绝 → DEFAULT 需确认
    _file_path = str(tool_input.get("file_path", "")) or None
    _command = str(tool_input.get("command", "")) or None
    decision = context.permission_checker.evaluate(
        tool_name,
        is_read_only=tool.is_read_only(parsed_input),
        file_path=_file_path,
        command=_command,
    )
    if not decision.allowed:
        if decision.requires_confirmation and context.permission_prompt is not None:
            # DEFAULT 模式下的写操作 → 调用权限回调（弹窗让用户确认 y/n）
            # 交互模式: 回调指向 backend_host._ask_permission → 前端弹窗 → 等待用户
            # 非交互模式: 回调指向 _noop_permission → 直接返回 True
            confirmed = await context.permission_prompt(tool_name, decision.reason)
            if not confirmed:
                return ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=f"Permission denied for {tool_name}",
                    is_error=True,
                )
        else:
            # 直接拒绝（黑名单/路径规则/PLAN 模式），不可确认
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=decision.reason or f"Permission denied for {tool_name}",
                is_error=True,
            )

    # ──── 关卡 5：实际执行 ────
    # parsed_input 是 Pydantic 验证后的类型安全对象
    # ToolExecutionContext 携带 cwd + metadata（含 mcp_manager、ask_user_prompt 等）
    result = await tool.execute(
        parsed_input,
        ToolExecutionContext(
            cwd=context.cwd,
            metadata={
                "tool_registry": context.tool_registry,
                "ask_user_prompt": context.ask_user_prompt,
                **(context.tool_metadata or {}),
            },
        ),
    )

    # 将工具返回的 ToolResult 转为 ToolResultBlock（加上 tool_use_id 用于配对）
    tool_result = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=result.output,
        is_error=result.is_error,
    )

    # ──── 关卡 6：PostToolUse Hook ────
    # 通知插件工具执行完毕，可用于日志记录、审计等
    if context.hook_executor is not None:
        await context.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_result.content,
                "tool_is_error": tool_result.is_error,
                "event": HookEvent.POST_TOOL_USE.value,
            },
        )
    return tool_result
