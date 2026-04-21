"""
World-awareness tool for the Logos capabilities MCP server.

Exposes ``world_snapshot`` — a read-only view of every named agent on
the system: name, soul, model, sprite description, activity status, and
whether the agent is currently busy. An agent calling this sees exactly
what a Logos user sees on the Agents world canvas, in structured form.

Use cases:

- An agent answering "who else is here?" or "who can help me with X?"
- An agent delegating a task but needing to pick a recipient by soul /
  capability rather than hard-coded name.
- A proactive agent checking whether a peer is busy before reaching out.

The snapshot is built by :func:`gateway.world_awareness.build_world_snapshot`,
which reads from the ``agents`` table + the live session state — so the
result is fresh on every call. No caching: the cost is a single SQL
query + an in-memory status join, well under the default 30s timeout.

Schema note: the ``self_agent_id`` parameter lets the caller mark a
specific agent as "self" so the response's ``is_self`` flag identifies
which row corresponds to the calling agent. Phase L.1 of the MCP server
didn't thread ``calling_agent`` resolution end-to-end yet, so the tool
accepts an explicit override — when Phase 5.3 lands full identity
resolution this can become automatic.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "self_agent_id": {
            "type": "string",
            "description": (
                "Agent ID to mark as 'self' in the response. The returned row "
                "for this ID will have is_self=true. Omit if the caller "
                "doesn't need to distinguish itself in the roster."
            ),
        },
        "include_self": {
            "type": "boolean",
            "description": (
                "Include the self agent in the snapshot. Defaults to true. "
                "Set false to get a peers-only view (useful for 'who else "
                "is around?' queries)."
            ),
            "default": True,
        },
    },
    "additionalProperties": False,
}


def _make_world_snapshot_handler():
    async def world_snapshot(
        calling_agent: Optional[dict], args: dict
    ) -> dict:
        from gateway.world_awareness import build_world_snapshot

        self_id = (args or {}).get("self_agent_id") or None
        include_self = (args or {}).get("include_self", True)

        # Pydantic validation is overkill for two optional fields — do a
        # cheap inline sanity check and fall through to defaults on bad
        # types. The MCP envelope will wrap any raised exception as a
        # structured error anyway.
        if self_id is not None and not isinstance(self_id, str):
            self_id = None
        if not isinstance(include_self, bool):
            include_self = True

        snapshot = build_world_snapshot(
            self_agent_id=self_id, include_self=include_self
        )
        # Audit log: who asked for the world. calling_agent may be None
        # until Phase 5.3 wires real identity resolution; log what we
        # have regardless so we can trace tool-use patterns.
        agent_hint = (calling_agent or {}).get("name", "unknown")
        logger.info(
            "mcp_logos.world_snapshot: agent=%s self_id=%s include_self=%s rows=%d",
            agent_hint, self_id, include_self,
            len(snapshot.get("agents", []) or []),
        )
        return snapshot

    return world_snapshot


def register(server: Any) -> None:
    """Register the world_snapshot tool on the given InProcessMCPServer."""
    server.register_tool(
        name="world_snapshot",
        description=(
            "Return a read-only roster of every named agent on the Logos "
            "system: name, soul, model, appearance, activity status, and "
            "busy flag. Use this to decide who to delegate to, answer "
            "'who else is here?', or orient yourself in the shared world. "
            "Optional self_agent_id marks the calling agent's row with "
            "is_self=true. Read-only; no side effects."
        ),
        input_schema=_INPUT_SCHEMA,
        handler=_make_world_snapshot_handler(),
        tier="auto_approve",  # read-only; no user gate needed
        timeout_s=10.0,
    )
