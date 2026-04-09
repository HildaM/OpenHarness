"""Tests for InProcessBackend: spawn, shutdown, send_message, and contextvars."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.query import QueryContext
from openharness.config.settings import PermissionSettings
from openharness.permissions.checker import PermissionChecker
from openharness.tools.agent_tool import AgentTool, AgentToolInput
from openharness.swarm.in_process import (
    InProcessBackend,
    TeammateContext,
    get_teammate_context,
    set_teammate_context,
)
from openharness.swarm.mailbox import TeammateMailbox
from openharness.swarm.types import TeammateMessage, TeammateSpawnConfig
from openharness.tools import create_default_tool_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spawn_config():
    return TeammateSpawnConfig(
        name="worker",
        team="test-team",
        prompt="hello",
        cwd="/tmp",
        parent_session_id="sess-001",
    )


class _StaticApiClient:
    def __init__(self, text: str = "worker complete") -> None:
        self._text = text

    async def stream_message(self, request):
        del request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=self._text)]),
            usage=UsageSnapshot(input_tokens=2, output_tokens=3),
            stop_reason=None,
        )


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return InProcessBackend()


# ---------------------------------------------------------------------------
# TeammateContext
# ---------------------------------------------------------------------------


def test_teammate_context_defaults():
    ctx = TeammateContext(
        agent_id="w@t",
        agent_name="w",
        team_name="t",
    )
    assert ctx.color is None
    assert ctx.plan_mode_required is False
    assert not ctx.cancel_event.is_set()


# ---------------------------------------------------------------------------
# ContextVar get / set
# ---------------------------------------------------------------------------


def test_get_teammate_context_returns_none_outside_task():
    # Outside any async task, the contextvar should be None
    result = get_teammate_context()
    assert result is None


async def test_set_and_get_teammate_context():
    ctx = TeammateContext(agent_id="x@y", agent_name="x", team_name="y")
    set_teammate_context(ctx)
    assert get_teammate_context() is ctx


# ---------------------------------------------------------------------------
# InProcessBackend.spawn
# ---------------------------------------------------------------------------


async def test_spawn_returns_success_result(backend, spawn_config):
    result = await backend.spawn(spawn_config)
    assert result.success is True
    assert result.agent_id == "worker@test-team"
    assert result.backend_type == "in_process"
    assert result.task_id.startswith("in_process_")


async def test_spawn_duplicate_returns_failure(backend, spawn_config):
    await backend.spawn(spawn_config)
    # Spawn again while first is still running
    result = await backend.spawn(spawn_config)
    assert result.success is False
    assert result.error is not None


async def test_spawn_creates_active_agent(backend, spawn_config):
    await backend.spawn(spawn_config)
    assert backend.is_active("worker@test-team")


# ---------------------------------------------------------------------------
# InProcessBackend.shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_unknown_agent_returns_false(backend):
    result = await backend.shutdown("nonexistent@team")
    assert result is False


async def test_graceful_shutdown(backend, spawn_config):
    await backend.spawn(spawn_config)
    assert backend.is_active("worker@test-team")

    result = await backend.shutdown("worker@test-team", timeout=2.0)
    assert result is True
    assert not backend.is_active("worker@test-team")


async def test_force_shutdown(backend, spawn_config):
    await backend.spawn(spawn_config)
    result = await backend.shutdown("worker@test-team", force=True, timeout=2.0)
    assert result is True


# ---------------------------------------------------------------------------
# InProcessBackend.send_message
# ---------------------------------------------------------------------------


async def test_send_message_writes_to_mailbox(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = TeammateSpawnConfig(
        name="rcvr",
        team="myteam",
        prompt="wait",
        cwd="/tmp",
        parent_session_id="s",
    )
    await backend.spawn(config)

    msg = TeammateMessage(text="work on it", from_agent="leader")
    # Should not raise
    await backend.send_message("rcvr@myteam", msg)

    # Verify the message was written to mailbox
    from openharness.swarm.mailbox import TeammateMailbox
    mailbox = TeammateMailbox(team_name="myteam", agent_id="rcvr")
    messages = await mailbox.read_all(unread_only=False)
    assert any(m.payload.get("content") == "work on it" for m in messages)

    await backend.shutdown("rcvr@myteam", force=True)


async def test_send_message_invalid_agent_id_raises(backend):
    with pytest.raises(ValueError, match="agentName@teamName"):
        await backend.send_message("no-at-sign", TeammateMessage(text="hi", from_agent="l"))


async def test_spawn_with_query_context_reports_result_to_leader(backend, tmp_path):
    registry = create_default_tool_registry()
    query_context = QueryContext(
        api_client=_StaticApiClient("worker complete"),
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="claude-test",
        system_prompt="system",
        max_tokens=128,
        max_turns=4,
        tool_metadata={"tool_registry": registry},
    )
    config = TeammateSpawnConfig(
        name="worker-real",
        team="team-real",
        prompt="say hi",
        cwd=str(tmp_path),
        parent_session_id="sess-001",
        metadata={"query_context": query_context},
    )

    result = await backend.spawn(config)
    assert result.success is True

    leader_mailbox = TeammateMailbox(team_name="team-real", agent_id="leader")
    for _ in range(40):
        messages = await leader_mailbox.read_all(unread_only=False)
        if messages:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("leader mailbox did not receive a completion notification")

    notification = messages[-1]
    assert notification.type == "idle_notification"
    assert notification.payload["status"] == "completed"
    assert notification.payload["result"] == "worker complete"
    assert notification.payload["usage"]["total_tokens"] == 5


def test_agent_tool_builds_worker_permission_checker_with_auto_allowed_tools(tmp_path):
    registry = create_default_tool_registry()
    tool = AgentTool()
    worker_context = tool._build_worker_query_context(
        arguments=AgentToolInput(
            description="worker",
            prompt="edit files",
            subagent_type="worker",
        ),
        context=type("Ctx", (), {"cwd": tmp_path, "metadata": {
            "tool_registry": registry,
            "full_tool_registry": registry,
            "api_client": _StaticApiClient("worker"),
            "permission_checker": PermissionChecker(PermissionSettings()),
            "permission_settings": PermissionSettings(),
            "permission_prompt": None,
            "ask_user_prompt": None,
            "hook_executor": None,
            "system_prompt": "system",
            "model": "claude-test",
            "max_tokens": 128,
            "max_turns": 4,
            "tool_metadata": {"tool_registry": registry},
        }})(),
        agent_def=None,
    )

    assert worker_context is not None
    decision = worker_context.permission_checker.evaluate("edit_file", is_read_only=False)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# active_agents / shutdown_all
# ---------------------------------------------------------------------------


async def test_active_agents_lists_running(backend, spawn_config):
    await backend.spawn(spawn_config)
    active = backend.active_agents()
    assert "worker@test-team" in active


async def test_shutdown_all(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("a", "b"):
        cfg = TeammateSpawnConfig(
            name=name,
            team="t",
            prompt="run",
            cwd="/tmp",
            parent_session_id="s",
        )
        await backend.spawn(cfg)

    await backend.shutdown_all(force=True, timeout=2.0)
    assert backend.active_agents() == []
