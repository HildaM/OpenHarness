"""Tool for spawning local agent tasks."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from openharness.api.client import SupportsStreamingMessages
from openharness.config.settings import PermissionSettings
from openharness.coordinator.agent_definitions import AgentDefinition, get_agent_definition
from openharness.coordinator.coordinator_mode import get_team_registry
from openharness.permissions.checker import PermissionChecker
from openharness.swarm.registry import get_backend_registry
from openharness.swarm.types import TeammateSpawnConfig
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from openharness.engine.query import QueryContext

logger = logging.getLogger(__name__)

_TOOL_NAME_ALIASES = {
    "read": "read_file",
    "fileread": "read_file",
    "edit": "edit_file",
    "fileedit": "edit_file",
    "write": "write_file",
    "filewrite": "write_file",
    "glob": "glob",
    "grep": "grep",
    "bash": "bash",
    "webfetch": "web_fetch",
    "web_search": "web_search",
    "websearch": "web_search",
    "skill": "skill",
}


class AgentToolInput(BaseModel):
    """Arguments for local agent spawning."""

    description: str = Field(description="Short description of the delegated work")
    prompt: str = Field(description="Full prompt for the local agent")
    subagent_type: str | None = Field(
        default=None,
        description="Agent type for definition lookup (e.g. 'general-purpose', 'Explore', 'worker')",
    )
    model: str | None = Field(default=None)
    command: str | None = Field(default=None, description="Override spawn command")
    team: str | None = Field(default=None, description="Optional team to attach the agent to")
    mode: str = Field(
        default="local_agent",
        description="Agent mode: local_agent, remote_agent, or in_process_teammate",
    )


class AgentTool(BaseTool):
    """Spawn a local background agent task."""

    name = "agent"
    description = "Spawn a local background agent task."
    input_model = AgentToolInput

    async def execute(self, arguments: AgentToolInput, context: ToolExecutionContext) -> ToolResult:
        if arguments.mode not in {"local_agent", "remote_agent", "in_process_teammate"}:
            return ToolResult(
                output="Invalid mode. Use local_agent, remote_agent, or in_process_teammate.",
                is_error=True,
            )

        agent_def = get_agent_definition(arguments.subagent_type) if arguments.subagent_type else None

        team = arguments.team or "default"
        agent_name = arguments.subagent_type or "agent"

        registry = get_backend_registry()
        try:
            executor = registry.get_executor("in_process")
        except KeyError:
            try:
                executor = registry.get_executor("subprocess")
            except KeyError:
                executor = registry.get_executor()

        worker_query_context = self._build_worker_query_context(
            arguments=arguments,
            context=context,
            agent_def=agent_def,
        )

        config = TeammateSpawnConfig(
            name=agent_name,
            team=team,
            prompt=arguments.prompt,
            cwd=str(context.cwd),
            parent_session_id=str(context.metadata.get("session_id", "main")),
            model=arguments.model or (agent_def.model if agent_def and agent_def.model != "inherit" else None),
            system_prompt=agent_def.system_prompt if agent_def else None,
            permissions=agent_def.permissions if agent_def else [],
            metadata={
                "query_context": worker_query_context,
                "description": arguments.description,
            },
        )

        try:
            result = await executor.spawn(config)
        except Exception as exc:
            logger.error("Failed to spawn agent: %s", exc)
            return ToolResult(output=str(exc), is_error=True)

        if not result.success:
            return ToolResult(output=result.error or "Failed to spawn agent", is_error=True)

        if arguments.team:
            get_team_registry().add_agent(arguments.team, result.task_id)

        return ToolResult(
            output=(
                f"Spawned agent {result.agent_id} "
                f"(task_id={result.task_id}, backend={result.backend_type})"
            )
        )

    def _build_worker_query_context(
        self,
        *,
        arguments: AgentToolInput,
        context: ToolExecutionContext,
        agent_def: AgentDefinition | None,
    ) -> "QueryContext | None":
        metadata = context.metadata
        api_client = metadata.get("api_client")
        permission_checker = metadata.get("permission_checker")
        model = metadata.get("model")
        system_prompt = metadata.get("system_prompt")
        max_tokens = metadata.get("max_tokens")
        max_turns = metadata.get("max_turns")

        if not all(
            value is not None
            for value in (api_client, permission_checker, model, system_prompt, max_tokens, max_turns)
        ):
            return None

        full_registry = metadata.get("full_tool_registry") or metadata.get("tool_registry")
        if not isinstance(full_registry, ToolRegistry):
            return None

        worker_registry = self._resolve_worker_registry(full_registry, agent_def)
        worker_system_prompt = agent_def.system_prompt if agent_def and agent_def.system_prompt else str(system_prompt)
        worker_model = arguments.model or (
            agent_def.model if agent_def and agent_def.model not in {None, "inherit"} else str(model)
        )

        tool_metadata = dict(metadata.get("tool_metadata", {}))
        tool_metadata.setdefault("full_tool_registry", full_registry)

        from openharness.engine.query import QueryContext

        resolved_api_client = cast(SupportsStreamingMessages, api_client)
        parent_permission_checker = cast(PermissionChecker, permission_checker)
        worker_permission_checker = self._resolve_worker_permission_checker(
            metadata=metadata,
            worker_registry=worker_registry,
            fallback=parent_permission_checker,
        )
        resolved_model = str(worker_model)
        resolved_max_tokens = int(cast(int | str, max_tokens))
        resolved_max_turns = int(cast(int | str, max_turns))

        return QueryContext(
            api_client=resolved_api_client,
            tool_registry=worker_registry,
            permission_checker=worker_permission_checker,
            cwd=context.cwd,
            model=resolved_model,
            system_prompt=worker_system_prompt,
            max_tokens=resolved_max_tokens,
            max_turns=resolved_max_turns,
            permission_prompt=metadata.get("permission_prompt"),
            ask_user_prompt=metadata.get("ask_user_prompt"),
            hook_executor=metadata.get("hook_executor"),
            tool_metadata=tool_metadata,
        )

    def _resolve_worker_registry(
        self,
        registry: ToolRegistry,
        agent_def: AgentDefinition | None,
    ) -> ToolRegistry:
        if agent_def is None:
            return registry

        allowed = self._normalize_tool_names(agent_def.tools)
        disallowed = self._normalize_tool_names(agent_def.disallowed_tools)

        if allowed is None:
            return registry.filtered(disallow=disallowed)
        return registry.filtered(allow=allowed, disallow=disallowed)

    def _normalize_tool_names(self, names: Iterable[str] | None) -> set[str] | None:
        if names is None:
            return None

        normalized: set[str] = set()
        saw_wildcard = False
        for raw_name in names:
            name = str(raw_name).strip()
            if not name:
                continue
            if name == "*":
                saw_wildcard = True
                continue
            lowered = name.lower().replace("-", "").replace("_", "")
            normalized.add(_TOOL_NAME_ALIASES.get(lowered, name))

        if saw_wildcard and not normalized:
            return None
        return normalized

    def _resolve_worker_permission_checker(
        self,
        *,
        metadata: dict[str, object],
        worker_registry: ToolRegistry,
        fallback: PermissionChecker,
    ) -> PermissionChecker:
        permission_settings = metadata.get("permission_settings")
        if not isinstance(permission_settings, PermissionSettings):
            return fallback

        allowed_tool_names = [tool.name for tool in worker_registry.list_tools()]
        merged_allowed = list(
            dict.fromkeys([*permission_settings.allowed_tools, *allowed_tool_names])
        )
        worker_settings = permission_settings.model_copy(
            update={"allowed_tools": merged_allowed}
        )
        return PermissionChecker(worker_settings)
