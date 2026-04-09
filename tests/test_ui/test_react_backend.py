"""Tests for the React backend host protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.swarm.in_process import InProcessBackend
from openharness.swarm.mailbox import TeammateMailbox, create_idle_notification
from openharness.swarm.registry import get_backend_registry
from openharness.swarm.types import TeammateSpawnConfig
from openharness.ui.backend_host import BackendHostConfig, ReactBackendHost
from openharness.ui.runtime import build_runtime, close_runtime, start_runtime


class StaticApiClient:
    """Fake streaming client for backend host tests."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream_message(self, request):
        del request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=self._text)]),
            usage=UsageSnapshot(input_tokens=2, output_tokens=3),
            stop_reason=None,
        )


@pytest.mark.asyncio
async def test_backend_host_processes_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("unused")))
    host._bundle = await build_runtime(api_client=StaticApiClient("unused"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("/version")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(event.type == "transcript_item" and event.item and event.item.role == "user" for event in events)
    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "system"
        and "OpenHarness" in event.item.text
        for event in events
    )
    assert any(event.type == "state_snapshot" for event in events)


@pytest.mark.asyncio
async def test_backend_host_processes_model_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("hello from react backend")))
    host._bundle = await build_runtime(api_client=StaticApiClient("hello from react backend"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        should_continue = await host._process_line("hi")
    finally:
        await close_runtime(host._bundle)

    assert should_continue is True
    assert any(
        event.type == "assistant_complete" and event.message == "hello from react backend"
        for event in events
    )
    assert any(
        event.type == "assistant_complete"
        and event.item
        and event.item.role == "assistant"
        and "hello from react backend" in event.item.text
        for event in events
    )


@pytest.mark.asyncio
async def test_build_runtime_filters_tools_in_coordinator_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CODE_COORDINATOR_MODE", "1")

    bundle = await build_runtime(api_client=StaticApiClient("unused"))
    try:
        assert {tool.name for tool in bundle.tool_registry.list_tools()} == {
            "agent",
            "send_message",
            "task_stop",
        }
        decision = bundle.engine._permission_checker.evaluate(
            "agent",
            is_read_only=False,
        )
        assert decision.allowed is True
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_backend_host_processes_leader_notification(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    host = ReactBackendHost(BackendHostConfig(api_client=StaticApiClient("coordinator follow-up")))
    host._bundle = await build_runtime(api_client=StaticApiClient("coordinator follow-up"))
    events = []

    async def _emit(event):
        events.append(event)

    host._emit = _emit  # type: ignore[method-assign]
    await start_runtime(host._bundle)
    try:
        leader_mailbox = TeammateMailbox(team_name="default", agent_id="leader")
        await leader_mailbox.write(
            create_idle_notification(
                sender="worker@default",
                recipient="leader",
                summary="worker finished",
                status="completed",
                result="fixed the issue",
                usage={"total_tokens": 9, "tool_uses": 1, "duration_ms": 120},
            )
        )

        await host._process_leader_notifications_once()
    finally:
        await close_runtime(host._bundle)

    assert any(
        event.type == "transcript_item"
        and event.item
        and event.item.role == "user"
        and "<task-notification>" in event.item.text
        and "fixed the issue" in event.item.text
        for event in events
    )
    assert any(
        event.type == "assistant_complete" and event.message == "coordinator follow-up"
        for event in events
    )


@pytest.mark.asyncio
async def test_close_runtime_cleans_in_process_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    bundle = await build_runtime(api_client=StaticApiClient("unused"))
    await start_runtime(bundle)

    registry = get_backend_registry()
    executor = registry.get_executor("in_process")
    assert isinstance(executor, InProcessBackend)

    config = TeammateSpawnConfig(
        name="worker",
        team="default",
        prompt="run",
        cwd=str(tmp_path),
        parent_session_id="main",
    )

    first = await executor.spawn(config)
    assert first.success is True
    assert executor.is_active("worker@default") is True

    await close_runtime(bundle)
    assert executor.is_active("worker@default") is False

    next_executor = get_backend_registry().get_executor("in_process")
    assert isinstance(next_executor, InProcessBackend)

    second = await next_executor.spawn(config)
    try:
        assert second.success is True
    finally:
        await next_executor.shutdown("worker@default", force=True, timeout=2.0)
