"""Anthropic API client wrapper with retry logic.

本文件是 API 层的「标杆实现」，职责：
1. 定义协议层数据类型（ApiMessageRequest / ApiStreamEvent）
2. 定义 Protocol 接口（SupportsStreamingMessages）—— 4 行代码统一三种 API 客户端
3. 实现 AnthropicApiClient —— 基于官方 SDK 的流式调用 + 重试机制

架构位置：
  engine/query.py (run_query 阶段 B)
      │
      └─→ context.api_client.stream_message(request)
              │
              ├── AnthropicApiClient   ← 本文件
              ├── OpenAICompatibleClient  (openai_client.py)
              └── CopilotClient           (copilot_client.py)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from anthropic import APIError, APIStatusError, AsyncAnthropic

from openharness.api.errors import (
    AuthenticationFailure,       # 401/403 认证失败（不可重试）
    OpenHarnessApiError,         # 统一错误基类，上层只需 catch 这一个
    RateLimitFailure,            # 429 速率限制（可重试）
    RequestFailure,              # 其他通用错误
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, assistant_message_from_api

log = logging.getLogger(__name__)

# ============================================================
# 重试配置
# 策略：指数退避 + 25% jitter + Retry-After 三合一
# ============================================================
MAX_RETRIES = 3          # 最多重试 3 次（加上首次 = 总共 4 次尝试）
BASE_DELAY = 1.0         # 首次退避基准（秒），后续按 2^attempt 增长：1s → 2s → 4s → 8s
MAX_DELAY = 30.0         # 退避上限，防止等太久
RETRYABLE_STATUS_CODES = {
    429,   # Rate Limited — 请求过快，等一会就行
    500,   # Internal Server Error — 服务端暂时故障
    502,   # Bad Gateway — 网关/代理问题
    503,   # Service Unavailable — 服务暂时不可用
    529,   # Anthropic 特有的「过载」状态码
}


# ============================================================
# 协议层：请求 & 事件数据类型
#
# 用 @dataclass(frozen=True) 而非 Pydantic BaseModel，原因：
#   - 这是内部传输对象，数据来自可信的内部代码（不像工具参数来自 LLM）
#   - 不需要 JSON Schema 导出（不需要给 LLM 看）
#   - 不需要自动类型转换和校验
#   - frozen=True 保证不可变 —— 防止重试时 request 被意外篡改
# ============================================================


@dataclass(frozen=True)
class ApiMessageRequest:
    """一次 LLM 调用的完整参数快照（不可变）。

    由 engine/query.py 的 run_query() 构造，传入 api_client.stream_message()。
    frozen=True 确保同一个 request 在多次重试中保持一致。
    """

    model: str                                              # 模型标识，如 "claude-sonnet-4-20250514"
    messages: list[ConversationMessage]                     # 对话历史（内部格式，发送前调 to_api_param() 序列化）
    system_prompt: str | None = None                        # System Prompt（Anthropic 独立参数，OpenAI 需转为 system 角色消息）
    max_tokens: int = 4096                                  # 单次最大输出 token 数
    tools: list[dict[str, Any]] = field(default_factory=list)  # 工具定义列表（Anthropic JSON Schema 格式）


@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """流式文本增量事件 —— 模型每产出一个 token 就 yield 一个。

    收到后立即传给前端渲染，实现「逐字打印」效果（零缓冲流式传输）。
    """

    text: str


@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """流结束事件 —— 携带完整的 assistant 消息 + Token 用量。

    收到此事件后，引擎会：
    1. 将 message 追加到对话历史
    2. 检查 stop_reason 决定下一步（tool_use → 执行工具，end_turn → 结束）
    3. 累计 usage 到成本追踪器
    """

    message: ConversationMessage          # 完整的 assistant 消息（SDK 自动拼装好的）
    usage: UsageSnapshot                  # Token 用量（input_tokens + output_tokens）
    stop_reason: str | None = None        # "end_turn" / "tool_use" / "max_tokens"


# 两事件协议：流传输过程中只会产出这两种事件
# TextDelta: 0~N 个（逐 token 产出）
# MessageComplete: 恰好 1 个（流结束时产出）
ApiStreamEvent = ApiTextDeltaEvent | ApiMessageCompleteEvent


# ============================================================
# Protocol 接口 —— 整个 API 层最重要的 4 行代码
#
# 工作原理（类似 Go 的 interface，即结构化类型/鸭子类型）：
#   任何类只要有签名匹配的 stream_message 方法，就自动满足此协议，
#   不需要显式继承。这使得 AnthropicApiClient / OpenAICompatibleClient /
#   CopilotClient 可以在 runtime.py 中自由互换，引擎代码完全不感知差异。
# ============================================================


class SupportsStreamingMessages(Protocol):
    """策略模式的 Python 式实现 —— 引擎通过此 Protocol 调用 LLM。

    等价于 Go 的：
        type SupportsStreamingMessages interface {
            StreamMessage(request ApiMessageRequest) <-chan ApiStreamEvent
        }
    """

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """对给定请求进行流式 LLM 调用，逐步 yield 事件。"""


# ============================================================
# 重试辅助函数
# ============================================================


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试。

    重试判定规则：
    - APIStatusError：看 HTTP 状态码是否在 RETRYABLE_STATUS_CODES 中
    - APIError（无状态码的底层错误）：通常是网络问题，值得重试
    - ConnectionError/TimeoutError/OSError：网络层异常，值得重试
    - 其他异常：不重试（如 JSON 解析错误、编程错误等）
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, APIError):
        return True  # 无状态码的 API 错误通常是网络问题
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _get_retry_delay(attempt: int, exc: Exception | None = None) -> float:
    """计算第 N 次重试前的等待时间。

    优先级（从高到低）：
    1. 服务器 Retry-After 头 —— 服务器明确告知等多久，最可靠
    2. 指数退避 + 25% jitter —— 自行估算

    为什么需要 jitter（随机抖动）？
      想象 100 个客户端同时收到 429，如果都在 2 秒后精确重试，
      服务器会再次被打爆。jitter 让重试时间分散在 2.0~2.5s 之间，
      避免「雷群效应（Thundering Herd）」。
    """
    import random

    # 优先级 1：服务器通过 Retry-After 头明确告知等待时间
    if isinstance(exc, APIStatusError):
        retry_after = getattr(exc, "headers", {})
        if hasattr(retry_after, "get"):
            val = retry_after.get("retry-after")
            if val:
                try:
                    return min(float(val), MAX_DELAY)
                except (ValueError, TypeError):
                    pass

    # 优先级 2：指数退避 + 随机 jitter
    # attempt=0 → 1s, attempt=1 → 2s, attempt=2 → 4s（上限 30s）
    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
    jitter = random.uniform(0, delay * 0.25)  # 0~25% 随机偏移
    return delay + jitter


# ============================================================
# Anthropic 客户端 —— 标杆实现
#
# 两层结构：
#   stream_message()  ← 外层：重试循环（最多 MAX_RETRIES+1 次尝试）
#       └── _stream_once()  ← 内层：单次 SSE 流式调用
#
# 隐式满足 SupportsStreamingMessages Protocol（不需要写继承声明）
# ============================================================


class AnthropicApiClient:
    """基于 Anthropic 官方 SDK 的异步客户端，封装了流式调用 + 重试逻辑。

    使用方式：
        client = AnthropicApiClient(api_key="sk-xxx")
        async for event in client.stream_message(request):
            if isinstance(event, ApiTextDeltaEvent):
                print(event.text, end="")  # 逐字打印
    """

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url   # 支持自定义 API 端点（如代理、私有部署）
        # AsyncAnthropic 是官方 SDK 的异步客户端，内部使用 httpx 发 HTTP 请求
        self._client = AsyncAnthropic(**kwargs)

    # ----- 外层：重试循环 -----

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """对 LLM 进行流式调用，遇到暂时性错误自动重试。

        重试策略：
        - 认证错误（OpenHarnessApiError）→ 立即抛出，不重试
        - 可重试错误（429/500/502/503/529/网络异常）→ 指数退避后重试
        - 不可重试错误 → 翻译为统一错误类型后抛出

        注意：因为 request 是 frozen 的，每次重试用的都是完全相同的参数，
        不会出现参数被意外修改的 bug。
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):  # 0, 1, 2, 3 = 共 4 次尝试
            try:
                async for event in self._stream_once(request):
                    yield event
                return  # 成功 → 结束整个方法
            except OpenHarnessApiError:
                raise  # 认证错误已经是统一类型了，直接抛出不重试
            except Exception as exc:
                last_error = exc
                if attempt >= MAX_RETRIES or not _is_retryable(exc):
                    # 用尽重试次数，或错误不可重试 → 翻译后抛出
                    if isinstance(exc, APIError):
                        raise _translate_api_error(exc) from exc
                    raise RequestFailure(str(exc)) from exc

                # 可重试 → 计算等待时间，日志记录，然后 sleep
                delay = _get_retry_delay(attempt, exc)
                status = getattr(exc, "status_code", "?")
                log.warning(
                    "API request failed (attempt %d/%d, status=%s), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES + 1, status, delay, exc,
                )
                await asyncio.sleep(delay)

        # 理论上不会走到这里（for 循环内会 return 或 raise），防御性代码
        if last_error is not None:
            if isinstance(last_error, APIError):
                raise _translate_api_error(last_error) from last_error
            raise RequestFailure(str(last_error)) from last_error

    # ----- 内层：单次 SSE 流式调用 -----

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """单次 API 调用：发送请求 → 接收 SSE 流 → yield 事件。

        SSE (Server-Sent Events) 流程：
        1. HTTP POST → api.anthropic.com/v1/messages（带 stream=True）
        2. 服务器逐个返回 SSE 事件（每个 token 一个 content_block_delta）
        3. 我们从中提取 text_delta → yield ApiTextDeltaEvent
        4. 流结束后，SDK 拼装完整消息 → yield ApiMessageCompleteEvent
        """
        # 构建 API 参数（从内部格式转为 Anthropic SDK 格式）
        params: dict[str, Any] = {
            "model": request.model,
            # to_api_param() 将内部 ConversationMessage 序列化为 Anthropic API 格式的 dict
            "messages": [message.to_api_param() for message in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.system_prompt:
            params["system"] = request.system_prompt  # Anthropic: system 是独立参数（非消息）
        if request.tools:
            params["tools"] = request.tools           # 工具定义列表（Anthropic JSON Schema 格式）

        try:
            # messages.stream() 返回一个异步上下文管理器，内部建立 SSE 长连接
            async with self._client.messages.stream(**params) as stream:
                # 逐个接收 SSE 事件，只提取 text_delta（忽略 tool_use 等其他事件类型）
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) != "text_delta":
                        continue
                    text = getattr(delta, "text", "")
                    if text:
                        yield ApiTextDeltaEvent(text=text)  # 每个 token 立即 yield → 零缓冲

                # 流结束后，SDK 已自动把所有 delta 拼装成完整消息
                final_message = await stream.get_final_message()
        except APIError as exc:
            if isinstance(exc, APIStatusError) and exc.status_code in RETRYABLE_STATUS_CODES:
                raise  # 可重试的错误抛给外层 stream_message 处理
            raise _translate_api_error(exc) from exc  # 不可重试的直接翻译

        # 提取 token 用量并生成最终事件
        usage = getattr(final_message, "usage", None)
        yield ApiMessageCompleteEvent(
            # assistant_message_from_api(): SDK Message 对象 → 内部 ConversationMessage
            message=assistant_message_from_api(final_message),
            usage=UsageSnapshot(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
            stop_reason=getattr(final_message, "stop_reason", None),  # "end_turn" / "tool_use" / "max_tokens"
        )


# ============================================================
# 异常翻译层
#
# 将 Anthropic SDK 的特定异常翻译为统一的 OpenHarnessApiError 体系。
# 这样上层代码（run_query / handle_line）只需 catch OpenHarnessApiError，
# 不需要知道底层用的是哪个 SDK。
# ============================================================


def _translate_api_error(exc: APIError) -> OpenHarnessApiError:
    """Anthropic SDK 异常 → 统一错误类型。

    映射规则：
    - AuthenticationError / PermissionDeniedError → AuthenticationFailure（不可重试）
    - RateLimitError → RateLimitFailure（可重试，但已在外层处理）
    - 其他 → RequestFailure（通用兜底）
    """
    name = exc.__class__.__name__
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return AuthenticationFailure(str(exc))
    if name == "RateLimitError":
        return RateLimitFailure(str(exc))
    return RequestFailure(str(exc))
