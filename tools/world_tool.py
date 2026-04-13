#!/usr/bin/env python3
"""
World Tool — look up who else is in the Agent World.

Returns a snapshot of every named agent on this Logos install: name,
soul, model, visual appearance, running/offline status, and whether
they're currently busy with a task. Intended for agents to:

- Answer "who else is on the system?" questions about their peers.
- Decide when to suggest delegating to an agent with a more specific
  soul (e.g. hand off a coding task to an agent with the
  ``app-development`` soul).
- Later — when agent-to-agent messaging lands — choose who to
  address.

The tool hits the local gateway's ``/api/world/state`` endpoint using
the same internal-token pattern as workflow_tool / routing_tool, so it
works inside OpenShell sandboxes without a user session.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_GATEWAY_BASE = (
    os.environ.get("LOGOS_GATEWAY_URL")
    or os.environ.get("HERMES_GATEWAY_URL")
    or "http://localhost:8080"
)
_INTERNAL_TOKEN = (
    os.environ.get("LOGOS_INTERNAL_TOKEN")
    or os.environ.get("HERMES_INTERNAL_TOKEN")
    or ""
)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _INTERNAL_TOKEN:
        h["Authorization"] = f"Bearer {_INTERNAL_TOKEN}"
    return h


def get_agent_world(include_self: bool = True) -> str:
    """Return a JSON snapshot of all named agents in the Agent World.

    Args:
        include_self: If True (default), the calling agent appears in
            the list with ``is_self: true``. If False, the caller is
            filtered out — useful when asking "who else is around?".

    Returns:
        JSON string with ``agents`` (list) and ``total_agents`` (int).
        Each agent row has: ``name``, ``soul``, ``model``, ``appearance``,
        ``status`` (running / offline / unknown), ``busy``, ``is_self``.
    """
    # The caller's own agent_id is surfaced by the worker via this env
    # var — see gateway/executors/openshell.py where it's populated
    # from instance_config before the sandbox process launches.
    self_id = os.environ.get("LOGOS_AGENT_ID") or os.environ.get("HERMES_AGENT_ID") or ""

    url = f"{_GATEWAY_BASE}/api/world/state"
    if self_id:
        url = f"{url}?agent_id={urllib.request.quote(self_id)}"

    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": f"HTTP {exc.code}: {exc.reason}"})
    except Exception as exc:
        return json.dumps({"error": f"Gateway unreachable: {exc}"})

    if not include_self:
        payload["agents"] = [a for a in payload.get("agents", []) if not a.get("is_self")]
        payload["total_agents"] = len(payload["agents"])

    return json.dumps(payload, indent=2)


WORLD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_agent_world",
        "description": (
            "Look up every named agent currently in the Agent World on this "
            "Logos install — their names, souls, models, visual appearances, "
            "running/offline status, and whether they are busy with a task. "
            "Use this to reason about peers you might delegate to, to refer "
            "to other agents by name, or to answer questions about who else "
            "is on the system."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_self": {
                    "type": "boolean",
                    "description": "Include yourself in the list (true, default) or filter yourself out (false).",
                    "default": True,
                },
            },
            "required": [],
        },
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="get_agent_world",
    toolset="world",
    schema=WORLD_TOOL_SCHEMA,
    handler=lambda args, **kw: get_agent_world(
        include_self=bool(args.get("include_self", True)),
    ),
    check_fn=lambda: True,
    description="Snapshot of all named agents in the Agent World (name, soul, appearance, status).",
)
