"""
logos.agent.interface — Core agent abstractions for the Logos platform.

These interfaces decouple the platform (runner, evals, routing) from any
specific agent implementation.  Hermes is the first adapter; future agents
(e.g. SWE-agent, ACP agents) will implement the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class AgentCapabilities:
    """Static description of what an adapter supports."""

    agent_id: str
    supports_streaming: bool = False
    supports_reasoning: bool = False
    supports_parallel_tools: bool = False
    available_toolsets: List[str] = field(default_factory=list)


@dataclass
class AgentContext:
    """
    All inputs needed for a single agent run.

    Maps 1-to-1 onto AIAgent.run_conversation() parameters so the adapter
    can forward them without loss of fidelity.
    """

    user_message: str
    system_message: Optional[str] = None
    conversation_history: Optional[List[Dict[str, Any]]] = None
    task_id: Optional[str] = None
    stream_callback: Optional[Callable] = None
    # Cleansed message stored in session transcripts (gateway uses this to
    # strip synthetic API prefixes before persisting).
    persist_user_message: Optional[str] = None
    # Optional callbacks for live tool progress reporting (runtimes that
    # don't support these just ignore them).
    tool_progress_callback: Optional[Callable] = None
    tool_complete_callback: Optional[Callable] = None
    step_callback: Optional[Callable] = None


@dataclass
class AgentResult:
    """
    Structured result from a single agent run.

    ``to_dict()`` reproduces the legacy dict shape returned by
    AIAgent.run_conversation() so existing callers need not change.
    """

    final_response: Optional[str]
    messages: List[Dict[str, Any]]
    api_calls: int
    completed: bool
    partial: bool = False
    interrupted: bool = False
    last_reasoning: Optional[str] = None
    response_previewed: bool = False
    interrupt_message: Optional[str] = None
    # Arbitrary extra fields forwarded from the underlying adapter
    # (e.g. "error" on partial runs).
    extras: Dict[str, Any] = field(default_factory=dict)

    # ── Conversion helpers ────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentResult":
        """Wrap a raw run_conversation() result dict."""
        known = {
            "final_response", "messages", "api_calls", "completed",
            "partial", "interrupted", "last_reasoning",
            "response_previewed", "interrupt_message",
        }
        extras = {k: v for k, v in d.items() if k not in known}
        return cls(
            final_response=d.get("final_response"),
            messages=d.get("messages", []),
            api_calls=d.get("api_calls", 0),
            completed=d.get("completed", False),
            partial=d.get("partial", False),
            interrupted=d.get("interrupted", False),
            last_reasoning=d.get("last_reasoning"),
            response_previewed=d.get("response_previewed", False),
            interrupt_message=d.get("interrupt_message"),
            extras=extras,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Reproduce the legacy dict shape for drop-in compatibility."""
        d: Dict[str, Any] = {
            "final_response": self.final_response,
            "last_reasoning": self.last_reasoning,
            "messages": self.messages,
            "api_calls": self.api_calls,
            "completed": self.completed,
            "partial": self.partial,
            "interrupted": self.interrupted,
            "response_previewed": self.response_previewed,
        }
        if self.interrupt_message is not None:
            d["interrupt_message"] = self.interrupt_message
        d.update(self.extras)
        return d


class AgentAdapter(ABC):
    """
    Abstract base class for agent adapters.

    An adapter wraps a concrete agent implementation (e.g. Hermes' AIAgent)
    and exposes it through this uniform interface.  AgentRunner uses this
    interface so it never depends on a specific implementation.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Short stable identifier for this adapter type (e.g. "hermes")."""

    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        """Return a description of what this adapter supports."""

    @abstractmethod
    def run(self, context: AgentContext) -> AgentResult:
        """Execute a single agent turn and return the result."""
