"""
logos.adapters.hermes.adapter — HermesAdapter wraps Hermes' AIAgent.

This is a pure facade: it holds a reference to an existing AIAgent instance
and forwards run() calls to run_conversation() without altering any internal
behaviour.  The AIAgent itself is unchanged.

Usage (CLI creates AIAgent as normal, then wraps it):

    agent = AIAgent(model=..., session_db=..., ...)
    runner = AgentRunner(HermesAdapter(agent))
    result = runner.run(AgentContext(user_message="Hello")).to_dict()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from logos.agent.interface import (
    AgentAdapter,
    AgentCapabilities,
    AgentContext,
    AgentResult,
)

if TYPE_CHECKING:
    from agents.hermes.agent import AIAgent


class HermesAdapter(AgentAdapter):
    """
    AgentAdapter implementation that delegates to Hermes' AIAgent.

    The adapter owns no state beyond the wrapped AIAgent reference; all
    session state, tool execution, and model communication remain inside
    the existing AIAgent implementation.
    """

    AGENT_ID = "hermes"

    def __init__(self, agent: "AIAgent") -> None:
        """
        Args:
            agent: An already-constructed AIAgent instance.  The adapter
                   does NOT take ownership; the caller remains responsible
                   for the agent's lifetime.
        """
        self._agent = agent

    # ── AgentAdapter interface ────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self.AGENT_ID

    def capabilities(self) -> AgentCapabilities:
        agent = self._agent
        toolsets: list = []
        if hasattr(agent, "enabled_toolsets") and agent.enabled_toolsets:
            toolsets = list(agent.enabled_toolsets)
        return AgentCapabilities(
            agent_id=self.AGENT_ID,
            supports_streaming=True,
            supports_reasoning=True,
            supports_parallel_tools=True,
            available_toolsets=toolsets,
        )

    def run(self, context: AgentContext) -> AgentResult:
        """Delegate to AIAgent.run_conversation() and wrap the result."""
        raw = self._agent.run_conversation(
            user_message=context.user_message,
            system_message=context.system_message,
            conversation_history=context.conversation_history,
            task_id=context.task_id,
            stream_callback=context.stream_callback,
            persist_user_message=context.persist_user_message,
        )
        return AgentResult.from_dict(raw)

    # ── Direct access (for callers that still need the underlying agent) ──

    @property
    def agent(self) -> "AIAgent":
        """Return the wrapped AIAgent (use sparingly — prefer the interface)."""
        return self._agent
