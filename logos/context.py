"""Logos AgentContext — portable context carrier for a single agent run.

AgentContext decouples tool dispatch from the agent's internal state by
carrying a ToolRegistry instance (with policy enforcement built in) plus
the session-scoped parameters the registry needs at invoke() time.

Usage::

    from logos.context import AgentContext
    from logos.tools.registry import registry

    ctx = AgentContext(
        session_id=session_id,
        tool_registry=registry,
        action_policy=action_policy,
        workspace_path=workspace_path,
        auth_user_id=user_id,
    )

    # Dispatch any tool through the policy gate:
    result = ctx.invoke("terminal", {"command": "ls"})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from tools.registry import ToolRegistry


@dataclass
class AgentContext:
    """Portable context for a single Logos agent run.

    Carries the tool registry with session-scoped parameters so agents
    can dispatch tools via ``ctx.invoke()`` without knowing the policy
    details inline.

    Attributes:
        session_id:     Unique session identifier (used by approval DB).
        tool_registry:  The ToolRegistry singleton (or a session-scoped copy).
        action_policy:  ActionPolicy instance, or None for fully-permissive.
        workspace_path: Per-run isolated workspace directory (str or Path).
        auth_user_id:   Auth DB user ID for approval request attribution.
    """

    session_id: str
    tool_registry: Any  # ToolRegistry — typed as Any to avoid circular import
    action_policy: Any = None
    workspace_path: Optional[str] = None
    auth_user_id: Optional[str] = None

    def invoke(
        self,
        tool_name: str,
        tool_args: dict,
        *,
        task_id: Optional[str] = None,
        user_task: Optional[str] = None,
        enabled_tools: Optional[list] = None,
    ) -> str:
        """Invoke a tool through the registry's policy-enforced dispatch.

        Equivalent to::

            registry.invoke(
                tool_name, tool_args,
                policy=self.action_policy,
                session_id=self.session_id,
                workspace_path=self.workspace_path,
                auth_user_id=self.auth_user_id,
                task_id=task_id,
                ...
            )
        """
        return self.tool_registry.invoke(
            tool_name,
            tool_args,
            policy=self.action_policy,
            session_id=self.session_id,
            workspace_path=self.workspace_path,
            auth_user_id=self.auth_user_id,
            task_id=task_id,
            user_task=user_task,
            enabled_tools=enabled_tools,
        )
