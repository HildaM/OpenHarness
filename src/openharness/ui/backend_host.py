"""JSON-lines backend host for the React terminal frontend.

本文件是交互模式下 Python 后端的「主控制器」。
当 React 前端通过 react_launcher.py spawn 后端进程时，最终会进入这里的事件循环。

═══════════════════════════════════════════════════════════════
整体架构理解指南：

ReactBackendHost 本质是一个「翻译官」——
  左手接前端的 JSON 请求（stdin），右手调 runtime.py 的核心逻辑，
  再把结果翻译成 JSON 事件（stdout）发回前端。

类中的方法按职责分为 4 组：
  ┌──────────────────────────────────────────────────────┐
  │ 第 1 组：生命周期                                      │
  │   run()              — 主入口：初始化 → 事件循环 → 清理 │
  │                                                        │
  │ 第 2 组：通信 I/O                                      │
  │   _read_requests()   — 后台任务：持续从 stdin 读请求    │
  │   _emit()            — 向 stdout 写一条 OHJSON 事件     │
  │                                                        │
  │ 第 3 组：业务处理                                       │
  │   _process_line()    — 处理用户输入（调 handle_line）   │
  │   _handle_list_sessions() — 处理会话列表请求            │
  │   _status_snapshot() — 生成当前状态快照                 │
  │                                                        │
  │ 第 4 组：异步交互（前端弹窗 ↔ 后端等待）               │
  │   _ask_permission()  — 请求权限确认（弹 y/n 对话框）   │
  │   _ask_question()    — 请求用户输入（弹文本输入框）     │
  └──────────────────────────────────────────────────────┘

通信协议：
  前端 → 后端（stdin）:  {"type":"submit_line","line":"Fix the bug"}\n
  后端 → 前端（stdout）: OHJSON:{"type":"assistant_delta","message":"I'll..."}\n
  「OHJSON:」前缀用于区分协议消息和普通 print 输出
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from uuid import uuid4

from openharness.api.client import SupportsStreamingMessages
from openharness.bridge import get_bridge_manager
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.tasks import get_task_manager
from openharness.ui.protocol import BackendEvent, FrontendRequest, TranscriptItem
from openharness.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime

# 协议前缀：后端发给前端的每一行都以此开头，前端据此区分协议消息和普通输出
_PROTOCOL_PREFIX = "OHJSON:"


# ═══════════════════════════════════════════════════════════════
# 配置数据类 — CLI 参数的打包传递
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BackendHostConfig:
    """从 CLI 参数透传过来的配置，frozen=True 表示创建后不可修改。

    这些值最终会传给 build_runtime() 来装配所有子系统。
    """

    model: str | None = None
    max_turns: int | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    api_key: str | None = None
    api_format: str | None = None
    api_client: SupportsStreamingMessages | None = None    # 外部注入（测试用）
    restore_messages: list[dict] | None = None             # 恢复会话（oh -c / oh -r）


# ═══════════════════════════════════════════════════════════════
# ReactBackendHost — 后端主控制器
# ═══════════════════════════════════════════════════════════════

class ReactBackendHost:
    """通过 stdin/stdout JSON 协议驱动 OpenHarness 运行时。

    生命周期：
        前端 spawn 后端进程 → __init__() → run() → [事件循环] → 清理退出

    并发模型：
        - _read_requests() 作为后台 Task 持续读 stdin
        - run() 主循环从队列取请求并处理
        - 两者通过 asyncio.Queue 通信，不需要锁
    """

    def __init__(self, config: BackendHostConfig) -> None:
        self._config = config
        self._bundle = None                        # RuntimeBundle，run() 中初始化

        # ── 通信相关 ──
        self._write_lock = asyncio.Lock()          # stdout 写锁（防止并发写入交织）
        self._request_queue: asyncio.Queue[FrontendRequest] = asyncio.Queue()  # 前端请求队列

        # ── 异步交互相关（权限确认 / 用户提问）──
        # key=request_id, value=Future（等待前端回复后 resolve）
        self._permission_requests: dict[str, asyncio.Future[bool]] = {}
        self._question_requests: dict[str, asyncio.Future[str]] = {}

        # ── 状态标记 ──
        self._busy = False                         # 是否正在处理用户输入（防重入）
        self._running = True                       # 主循环运行标记

    # ═══════════════════════════════════════════════════════════
    # 第 1 组：生命周期
    # ═══════════════════════════════════════════════════════════

    async def run(self) -> int:
        """后端主入口，包含 3 个阶段：初始化 → 事件循环 → 清理。"""

        # ────── 阶段 1：初始化 ──────
        # 装配所有子系统（API 客户端、工具注册表、权限检查器、引擎等）
        # 注意这里注入了 _ask_permission 和 _ask_question 作为回调，
        # 当 Agent 引擎需要权限确认时，会通过这两个回调与前端交互
        self._bundle = await build_runtime(
            model=self._config.model,
            max_turns=self._config.max_turns,
            base_url=self._config.base_url,
            system_prompt=self._config.system_prompt,
            api_key=self._config.api_key,
            api_format=self._config.api_format,
            api_client=self._config.api_client,
            restore_messages=self._config.restore_messages,
            permission_prompt=self._ask_permission,    # ← 权限确认回调（第 4 组）
            ask_user_prompt=self._ask_question,        # ← 用户提问回调（第 4 组）
        )
        await start_runtime(self._bundle)              # 触发 SESSION_START 钩子

        # 通知前端「后端已就绪」，附带初始状态、任务列表、可用命令列表
        await self._emit(
            BackendEvent.ready(
                self._bundle.app_state.get(),
                get_task_manager().list_tasks(),
                [f"/{command.name}" for command in self._bundle.commands.list_commands()],
            )
        )
        await self._emit(self._status_snapshot())      # 发送 MCP 状态等

        # ────── 阶段 2：事件循环 ──────
        # 启动后台 stdin 读取任务（在独立线程中阻塞读取，不影响事件循环）
        reader = asyncio.create_task(self._read_requests())
        try:
            while self._running:
                # 从队列取一个前端请求（阻塞等待）
                request = await self._request_queue.get()

                # ── 请求路由（按 type 分发）──
                if request.type == "shutdown":
                    # 前端请求关闭（Ctrl+C 或 /exit）
                    await self._emit(BackendEvent(type="shutdown"))
                    break

                if request.type == "permission_response":
                    # 前端回复了权限确认弹窗（用户按了 y 或 n）
                    # 找到对应的 Future 并 resolve，解除 _ask_permission 中的 await
                    if request.request_id in self._permission_requests:
                        self._permission_requests[request.request_id].set_result(bool(request.allowed))
                    continue

                if request.type == "question_response":
                    # 前端回复了用户输入（用户在文本框里输入了答案）
                    if request.request_id in self._question_requests:
                        self._question_requests[request.request_id].set_result(request.answer or "")
                    continue

                if request.type == "list_sessions":
                    # 前端请求会话列表（用户输入了 /resume）
                    await self._handle_list_sessions()
                    continue

                if request.type != "submit_line":
                    # 未知请求类型 → 返回错误
                    await self._emit(BackendEvent(type="error", message=f"Unknown request type: {request.type}"))
                    continue

                # ── 以下是 submit_line 的处理 ──
                if self._busy:
                    # 防重入：如果上一个请求还没处理完，拒绝新请求
                    await self._emit(BackendEvent(type="error", message="Session is busy"))
                    continue
                line = (request.line or "").strip()
                if not line:
                    continue

                # 标记为忙碌 → 处理 → 无论成败都解除忙碌
                self._busy = True
                try:
                    should_continue = await self._process_line(line)   # ← 进入核心处理
                finally:
                    self._busy = False
                if not should_continue:
                    # handle_line 返回 False = 用户执行了 /exit
                    await self._emit(BackendEvent(type="shutdown"))
                    break

        # ────── 阶段 3：清理 ──────
        finally:
            reader.cancel()                            # 取消 stdin 读取任务
            with contextlib.suppress(asyncio.CancelledError):
                await reader                           # 等待取消完成（忽略 CancelledError）
            if self._bundle is not None:
                await close_runtime(self._bundle)      # 关闭 MCP 连接 + 触发 SESSION_END 钩子
        return 0

    # ═══════════════════════════════════════════════════════════
    # 第 2 组：通信 I/O
    # ═══════════════════════════════════════════════════════════

    async def _read_requests(self) -> None:
        """后台任务：持续从 stdin 读取前端请求，放入队列。

        为什么用 asyncio.to_thread？
          sys.stdin.readline() 是同步阻塞的，直接在 asyncio 中调用会冻结整个事件循环。
          to_thread 把它放到线程池执行，事件循环可以继续处理其他任务（如发送事件）。

        当前端进程退出时，stdin 管道关闭 → readline 返回空 → 自动发送 shutdown。
        这是「安全网」机制，确保后端不会变成孤儿进程。
        """
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                # stdin EOF = 前端已退出 → 通知主循环关闭
                await self._request_queue.put(FrontendRequest(type="shutdown"))
                return
            payload = raw.decode("utf-8").strip()
            if not payload:
                continue
            try:
                # Pydantic 校验：确保 JSON 符合 FrontendRequest 结构
                request = FrontendRequest.model_validate_json(payload)
            except Exception as exc:  # pragma: no cover - defensive protocol handling
                await self._emit(BackendEvent(type="error", message=f"Invalid request: {exc}"))
                continue
            await self._request_queue.put(request)

    async def _emit(self, event: BackendEvent) -> None:
        """向前端发送一条协议事件。

        格式：OHJSON:{"type":"assistant_delta","message":"Hello"}\n

        _write_lock 防止并发写入：Agent 循环可能并发执行多个工具，
        多个协程同时调用 _emit()，如果不加锁，两条 JSON 可能交织在一行内，
        导致前端 readline 解析出错。
        """
        async with self._write_lock:
            sys.stdout.write(_PROTOCOL_PREFIX + event.model_dump_json() + "\n")
            sys.stdout.flush()  # 立即刷新，不等 Python 缓冲区满

    # ═══════════════════════════════════════════════════════════
    # 第 3 组：业务处理
    # ═══════════════════════════════════════════════════════════

    async def _process_line(self, line: str) -> bool:
        """处理一行用户输入——这是后端最核心的方法。

        职责：
          1. 通知前端「用户说了什么」（transcript_item）
          2. 定义 3 个渲染回调（把引擎事件翻译成前端协议）
          3. 调用 handle_line()（进入 runtime.py 的消息路由 → Agent 循环）
          4. 发送结束事件（status + tasks + line_complete）

        与 engine 层的关系（承上启下）：
          本方法从不直接调用 QueryEngine，而是通过 handle_line() 间接触达：
            _process_line → handle_line → engine.submit_message → run_query

          数据通过 yield + async for 流式穿透 4 层回到这里：
            run_query yield → submit_message yield → handle_line async for
            → await render_event(event) → 回到本方法的 _render_event 回调
            → _emit(BackendEvent) → stdout → 前端

          同时，engine 内部执行工具时如果需要权限确认，会反向回调到
          本类的 _ask_permission()（第 4 组），形成「请求→弹窗→回复」闭环。

        返回 True 继续接受输入，False 表示应该退出（用户执行了 /exit）。
        """
        assert self._bundle is not None

        # 先告诉前端「用户输入了什么」，让前端在对话记录中显示
        await self._emit(
            BackendEvent(type="transcript_item", item=TranscriptItem(role="user", text=line))
        )

        # ── 定义 3 个渲染回调 ──
        # 这些回调会被 handle_line() 在处理过程中调用
        # 它们的作用是把引擎内部的事件「翻译」成前端能理解的协议消息

        async def _print_system(message: str) -> None:
            """渲染系统消息（如 "Stopped after 200 turns"）。"""
            await self._emit(
                BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=message))
            )

        async def _render_event(event: StreamEvent) -> None:
            """渲染引擎产出的流事件——这是「翻译」的核心。

            把 engine 的 4 种 StreamEvent 转换为前端的 BackendEvent：
              AssistantTextDelta    → assistant_delta   （流式文本，前端追加到 buffer）
              AssistantTurnComplete → assistant_complete （回合结束，前端写入 transcript）
              ToolExecutionStarted  → tool_started      （工具开始，前端显示工具名）
              ToolExecutionCompleted→ tool_completed     （工具完成，前端显示结果）
            """
            if isinstance(event, AssistantTextDelta):
                # 模型产出了一个 token → 立即发给前端（流式显示）
                await self._emit(BackendEvent(type="assistant_delta", message=event.text))
                return
            if isinstance(event, AssistantTurnComplete):
                # 模型一个回合结束 → 发送完整文本 + 更新任务列表
                await self._emit(
                    BackendEvent(
                        type="assistant_complete",
                        message=event.message.text.strip(),
                        item=TranscriptItem(role="assistant", text=event.message.text.strip()),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                return
            if isinstance(event, ToolExecutionStarted):
                # 工具即将执行 → 前端显示 "Running read_file..."
                await self._emit(
                    BackendEvent(
                        type="tool_started",
                        tool_name=event.tool_name,
                        tool_input=event.tool_input,
                        item=TranscriptItem(
                            role="tool",
                            text=f"{event.tool_name} {json.dumps(event.tool_input, ensure_ascii=True)}",
                            tool_name=event.tool_name,
                            tool_input=event.tool_input,
                        ),
                    )
                )
                return
            if isinstance(event, ToolExecutionCompleted):
                # 工具执行完毕 → 发送结果 + 更新任务列表 + 更新状态
                await self._emit(
                    BackendEvent(
                        type="tool_completed",
                        tool_name=event.tool_name,
                        output=event.output,
                        is_error=event.is_error,
                        item=TranscriptItem(
                            role="tool_result",
                            text=event.output,
                            tool_name=event.tool_name,
                            is_error=event.is_error,
                        ),
                    )
                )
                # 工具可能改变了后台任务列表或应用状态，所以每次工具完成后都刷新
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                await self._emit(self._status_snapshot())

        async def _clear_output() -> None:
            """清空前端的对话记录（/clear 命令触发）。"""
            await self._emit(BackendEvent(type="clear_transcript"))

        # ── 调用核心处理逻辑 ──
        # handle_line 是 runtime.py 中的函数，会：
        #   1. 判断是斜杠命令还是普通消息
        #   2. 命令 → 执行命令处理器
        #   3. 普通消息 → engine.submit_message() → Agent 循环
        #   4. 在循环过程中，通过上面的回调把事件发给前端
        should_continue = await handle_line(
            self._bundle,
            line,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )

        # 一行处理完毕 → 发送最终状态 + line_complete 标记
        await self._emit(self._status_snapshot())
        await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
        await self._emit(BackendEvent(type="line_complete"))  # ← 前端收到后 setBusy(false)
        return should_continue

    def _status_snapshot(self) -> BackendEvent:
        """生成当前应用状态的快照（模型名、权限模式、MCP 状态等）。

        前端收到后更新 StatusBar 显示。
        """
        assert self._bundle is not None
        return BackendEvent.status_snapshot(
            state=self._bundle.app_state.get(),
            mcp_servers=self._bundle.mcp_manager.list_statuses(),
            bridge_sessions=get_bridge_manager().list_sessions(),
        )

    async def _handle_list_sessions(self) -> None:
        """处理 /resume 命令——向前端发送会话列表让用户选择。

        流程：
          1. 读取磁盘上保存的会话快照
          2. 格式化为选项列表
          3. 通过 select_request 事件发给前端
          4. 前端弹出选择器 → 用户选择 → 前端发送 submit_line（如 "/resume abc123"）
        """
        from openharness.services.session_storage import list_session_snapshots
        import time as _time

        assert self._bundle is not None
        sessions = list_session_snapshots(self._bundle.cwd, limit=10)
        options = []
        for s in sessions:
            ts = _time.strftime("%m/%d %H:%M", _time.localtime(s["created_at"]))
            summary = s.get("summary", "")[:50] or "(no summary)"
            options.append({
                "value": s["session_id"],
                "label": f"{ts}  {s['message_count']}msg  {summary}",
            })
        await self._emit(
            BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": "Resume Session", "submit_prefix": "/resume "},
                select_options=options,
            )
        )

    # ═══════════════════════════════════════════════════════════
    # 第 4 组：异步交互（前端弹窗 ↔ 后端等待）
    #
    # 这两个方法的工作原理相同：
    #   1. 创建一个 asyncio.Future（空的「承诺」）
    #   2. 发送弹窗请求给前端
    #   3. await future → 挂起当前协程，等待前端回复
    #   4. 前端回复时，run() 主循环中的 permission_response/question_response
    #      分支会调用 future.set_result()，解除挂起
    #   5. 返回结果给调用方（engine 的权限检查器或工具）
    #
    # 关键：这实现了「后端发起 → 前端弹窗 → 用户操作 → 结果返回后端」的跨进程异步交互
    # ═══════════════════════════════════════════════════════════

    async def _ask_permission(self, tool_name: str, reason: str) -> bool:
        """请求前端弹出权限确认对话框，等待用户按 y（True）或 n（False）。

        调用方：engine/query.py 的 _execute_tool_call() 中的权限检查，
        当 PermissionChecker 返回 requires_confirmation=True 时触发。

        时序：
          后端: _ask_permission("file_edit", "写操作需要确认")
            → 创建 Future
            → 发送 modal_request 给前端
            → await future（挂起）
          前端: 弹出 "Allow file_edit? [y] [n]"
            → 用户按 y
            → 发送 {"type":"permission_response","request_id":"abc","allowed":true}
          后端: run() 主循环收到 → future.set_result(True)
            → _ask_permission 被唤醒，返回 True
            → 引擎继续执行工具
        """
        request_id = uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._permission_requests[request_id] = future
        await self._emit(
            BackendEvent(
                type="modal_request",
                modal={
                    "kind": "permission",
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "reason": reason,
                },
            )
        )
        try:
            return await future         # ← 在这里挂起，直到前端回复
        finally:
            self._permission_requests.pop(request_id, None)  # 清理，避免内存泄漏

    async def _ask_question(self, question: str) -> str:
        """请求前端弹出文本输入框，等待用户输入答案。

        调用方：工具执行时如果需要向用户提问（如 AskUserQuestionTool）。
        原理与 _ask_permission 完全相同，只是返回 str 而非 bool。
        """
        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._question_requests[request_id] = future
        await self._emit(
            BackendEvent(
                type="modal_request",
                modal={
                    "kind": "question",
                    "request_id": request_id,
                    "question": question,
                },
            )
        )
        try:
            return await future
        finally:
            self._question_requests.pop(request_id, None)


# ═══════════════════════════════════════════════════════════════
# 模块级入口函数 — 供 app.py 的 run_repl(backend_only=True) 调用
# ═══════════════════════════════════════════════════════════════

async def run_backend_host(
    *,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    cwd: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    restore_messages: list[dict] | None = None,
) -> int:
    """启动后端主机的便捷入口。

    调用链：
      cli.py main(backend_only=True)
        → app.py run_repl(backend_only=True)
          → 本函数
            → ReactBackendHost(config).run()
    """
    if cwd:
        os.chdir(cwd)    # 切换工作目录（由 react_launcher 通过 --cwd 参数传入）
    host = ReactBackendHost(
        BackendHostConfig(
            model=model,
            max_turns=max_turns,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            api_client=api_client,
            restore_messages=restore_messages,
        )
    )
    return await host.run()


__all__ = ["run_backend_host", "ReactBackendHost", "BackendHostConfig"]
