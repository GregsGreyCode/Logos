"""
gateway.mcp_logos — in-process MCP server exposing Logos gateway capabilities.

External MCP servers (filesystem, github, ...) run as stdio subprocesses and
are managed by `tools.mcp_tool`. This package adds a *parallel* server type
that runs inside the gateway process itself so sandboxed agents can call
back into runner-held state (platform adapters, session store, cron,
memory, agent roster) via the same `/mcp/{name}/*` HTTP interface they
already use for external tools.

See docs/migration/logos-capabilities-mcp-server.md for the full design.

Public entry point:

    from gateway.mcp_logos import register_logos_server
    register_logos_server(runner, mcp_service)

That injects an ``InProcessMCPServer`` named ``logos`` into the
MCPGatewayService's server table and its config, so it shows up in the
catalogue and is reachable via `/mcp/logos/*`.

Phase L.1 ships the primitive with **zero tools** — tools are added in
subsequent phases (L.2 platform, L.3 session+memory, L.4 cron+workflow,
L.5 agent roster). This lets us land the infrastructure safely and iterate
on tools without touching the transport.
"""

from .server import InProcessMCPServer, register_logos_server

__all__ = ["InProcessMCPServer", "register_logos_server"]
