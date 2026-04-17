"""
Time tool for the Logos capabilities MCP server (LOG-41).

Exposes one auto-approve tool to sandboxed agents:

- ``get_current_time`` — returns the current wall-clock time the gateway
  host sees, with optional IANA timezone conversion. Useful for
  scheduling ("remind me in 3 hours"), relative-date reasoning ("is
  that event this week?"), and anything else where the agent needs to
  know what "now" is without inferring it from conversation context.

The passive "what time is it?" case was previously handled by a
prompt injection that stamped ``Current time`` onto the session
context (commit 24e3ad8). That's still there — this tool is for
*active* queries where the agent decides it needs the current time
mid-task, e.g. to compute a target timestamp or check if a deadline
has passed.

Return shape::

    {
      "iso":      "2026-04-17T21:45:13+01:00",
      "epoch_s":  1776488713,
      "timezone": "Europe/London",
      "display":  "Thursday, 17 Apr 2026, 21:45 BST"
    }

Fallback on unknown/invalid timezone: returns the server's local time
(same as passing no ``timezone`` argument) and sets ``fallback`` in
the envelope so the agent can mention it if it matters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as _tz
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _make_get_current_time_handler():
    async def get_current_time(
        arguments: dict, calling_agent: Optional[dict] = None
    ) -> dict:
        tz_name = (arguments or {}).get("timezone") or ""
        fallback = False

        tzinfo = None
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                tzinfo = ZoneInfo(tz_name)
            except Exception:
                # Unknown tz — fall back to server local and flag it so
                # the agent can say "I couldn't find that timezone" if
                # it matters. Don't raise: a time lookup should never
                # fail outright.
                tzinfo = None
                fallback = True

        now = datetime.now(tzinfo) if tzinfo else datetime.now().astimezone()
        resolved_tz = str(now.tzinfo) if now.tzinfo else "UTC"

        try:
            display = now.strftime("%A, %d %b %Y, %H:%M %Z").strip()
        except Exception:
            display = now.isoformat()

        return {
            "iso":      now.isoformat(),
            "epoch_s":  int(now.timestamp()),
            "timezone": resolved_tz,
            "display":  display,
            "fallback": fallback,
        }

    return get_current_time


_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": (
                "Optional IANA timezone (e.g. 'America/Los_Angeles', "
                "'Europe/London'). Omit for the gateway host's local "
                "timezone. Invalid values fall back to local and set "
                "`fallback: true` in the response."
            ),
        },
    },
    "additionalProperties": False,
}


def register(server) -> None:
    """Register the time tool on the given InProcessMCPServer."""
    server.register_tool(
        name="get_current_time",
        description=(
            "Return the current wall-clock time on the gateway host, "
            "optionally in a specified IANA timezone. Useful for "
            "scheduling, relative dates ('in 3 hours'), and deadline "
            "checks. Read-only; no side effects."
        ),
        input_schema=_INPUT_SCHEMA,
        handler=_make_get_current_time_handler(),
        tier="auto_approve",   # read-only; no user prompt needed
        timeout_s=5.0,
    )
