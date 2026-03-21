"""
logos.agent.runner — AgentRunner orchestrates a single agent turn.

Phase 1: thin wrapper around an AgentAdapter.  Future phases will add
pre/post hooks, run-record enrichment, routing logic, and eval harness
integration here without touching individual adapters.
"""

from __future__ import annotations

from logos.agent.interface import AgentAdapter, AgentContext, AgentResult


class AgentRunner:
    """
    Runs an agent turn through an AgentAdapter.

    AgentRunner is the stable entry point for platform-level concerns
    (logging, metrics, routing).  In Phase 1 it simply delegates to the
    adapter; hooks and observability will be layered in during Phase 2+.
    """

    def __init__(self, adapter: AgentAdapter) -> None:
        if not isinstance(adapter, AgentAdapter):
            raise TypeError(
                f"adapter must be an AgentAdapter, got {type(adapter).__name__}"
            )
        self._adapter = adapter

    @property
    def adapter(self) -> AgentAdapter:
        return self._adapter

    def run(self, context: AgentContext) -> AgentResult:
        """Execute one agent turn and return the structured result."""
        return self._adapter.run(context)
