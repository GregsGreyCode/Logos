"""
Placeholder for the former platform tools (``platform_send``, ``home_message``).

Both tools were removed from the Logos MCP server on 2026-04-21 because the
gateway-held platform adapters they depended on had been disabled much
earlier (Phase 5.1 of the platforms migration) and were never re-enabled —
the messaging architecture shifted instead, with platform adapters now
living inside each sandbox's hermes process and the bot token in the
sandbox's ``/tmp/hermes-srv-home/.env``. An agent calling "send a message"
reaches its in-sandbox hermes tool, which talks to the in-sandbox adapter
directly; the Logos gateway never touches the token.

Leaving ``platform_send`` / ``home_message`` registered here was actively
misleading — the Logos MCP catalogue advertised them as available tools
but every call returned ``AdapterUnavailable``.

If gateway-mediated messaging ever comes back (e.g. a central-broadcast
pattern that can't live in individual sandboxes), re-register the tools
here and restart adapter startup in ``GatewayRunner.start()``. Git
history has the prior handler + schema implementations.

Schema definitions in :mod:`gateway.mcp_logos.schemas` are kept for
reference but are no longer consumed by anything in-tree.
"""

from __future__ import annotations


def register(server) -> None:  # noqa: ARG001 — kept for import-compat
    """No-op: platform tools are intentionally unregistered.

    ``register_logos_server`` in ``server.py`` no longer imports this
    module, so this stub only exists to avoid breakage for any external
    code that still imports ``platform.register`` directly.
    """
    return
