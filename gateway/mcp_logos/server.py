"""
InProcessMCPServer — the primitive behind the Logos capabilities MCP server.

External MCP servers are stdio subprocesses with a ClientSession. This class
wears the same duck-typed surface (``name``, ``description``, ``session``,
``_registered_tool_names``, ``tool_timeout``) but dispatches tool calls to
in-process async callables instead of forwarding JSON-RPC to a subprocess.

``MCPGatewayService`` treats the server as a first-class entry in its
``_servers`` dict. The HTTP handler at ``/mcp/{name}/*`` branches on the
``_logos_in_process`` marker and calls :meth:`dispatch_method` here instead
of ``_dispatch_to_session``.

Architecture notes:

- Tools receive a ``calling_agent`` dict as their first argument. That value
  is resolved **server-side** from the caller's WebSocket / session context
  and never accepted from the tool args — this prevents an agent from
  spoofing another agent's identity by lying in the JSON-RPC payload.
  (Phase L.1 leaves ``calling_agent`` unresolved — callers pass ``None``;
  Phase 5.3 wires real worker_id → agent resolution when the platforms
  migration begins dispatching inbound messages through this path.)

- Every tool call returns a structured envelope
  ``{ok, data, error, tool, duration_ms}``. This mirrors the shape
  described in section 4.5 of the design doc, so sandbox MCP clients see
  the same response shape whether they're talking to the logos server or
  a third-party stdio MCP server.

- Arg validation runs *before* the tool handler via Pydantic models. Invalid
  args short-circuit with a structured error; the tool never sees a bad
  payload. (Phase L.1 has no tools, so this is a no-op for now.)

- Per-call timeout defaults to 30s, overridable per tool. The dispatcher
  wraps the tool coroutine in ``asyncio.wait_for`` and returns a structured
  ``TimeoutError`` envelope on breach.

Phase L.1 intentionally ships **zero tools** — only the plumbing. Tools
land in L.2 (platform), L.3 (session/memory), L.4 (cron/workflow), L.5
(agent roster). Each phase just calls :meth:`InProcessMCPServer.register_tool`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_S = 30.0

# Sentinel marker that mcp_service.handle_jsonrpc checks to pick the
# in-process branch instead of forwarding to a stdio ClientSession.
IN_PROCESS_MARKER = "_logos_in_process"


# ---------------------------------------------------------------------------
# Tool registration record
# ---------------------------------------------------------------------------


@dataclass
class RegisteredTool:
    """Metadata + handler for a single in-process tool."""

    name: str
    description: str
    # JSON Schema for the tool's input. Can be generated from a Pydantic
    # model via ``model.model_json_schema()``. L.1 accepts a raw dict so
    # tools without Pydantic models (e.g. trivial no-arg tools) can still
    # register cleanly.
    input_schema: Dict[str, Any]
    # Async handler: ``async def fn(calling_agent, args) -> dict``
    handler: Callable[[Optional[dict], dict], Awaitable[Any]]
    # Approval tier: "auto_approve" / "user_approve" / "admin_approve" / "deny".
    # Mirrors the category tiers used for external MCP servers.
    tier: str = "user_approve"
    # Per-tool timeout override (seconds). None → use DEFAULT_TOOL_TIMEOUT_S.
    timeout_s: Optional[float] = None
    # Call counter for the catalogue/dashboard surface.
    call_count: int = 0
    error_count: int = 0


# ---------------------------------------------------------------------------
# InProcessMCPServer
# ---------------------------------------------------------------------------


class InProcessMCPServer:
    """In-process MCP server providing gateway-held capabilities to sandboxes.

    Duck-types ``MCPServerTask`` (from ``tools/mcp_tool.py``) so
    ``MCPGatewayService`` can store it in the same ``_servers`` dict without
    a special case for catalogue queries. The HTTP JSON-RPC path picks the
    in-process branch by checking the :data:`IN_PROCESS_MARKER` attribute.
    """

    def __init__(
        self,
        name: str,
        description: str,
        runner: Any = None,
    ) -> None:
        self.name = name
        self.description = description
        # Runner reference — tools use this to reach adapters, session_store,
        # cron_manager, memory_manager, etc. For L.1 this is held but unused
        # because there are no tools yet.
        self.runner = runner

        # ------------------------------------------------------------------
        # Duck-typed surface matching tools.mcp_tool.MCPServerTask
        # ------------------------------------------------------------------
        # `session` is truthy so mcp_service.is_connected() returns True. The
        # value doesn't matter — the in-process dispatcher never touches it.
        self.session: Any = self
        self._registered_tool_names: List[str] = []
        self.tool_timeout = int(DEFAULT_TOOL_TIMEOUT_S)

        # Marker for the mcp_service branch.
        setattr(self, IN_PROCESS_MARKER, True)

        # ------------------------------------------------------------------
        # Tool registry
        # ------------------------------------------------------------------
        self._tools: Dict[str, RegisteredTool] = {}

    # ------------------------------------------------------------------
    # Registration (called from L.2+ tool modules)
    # ------------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Optional[dict], dict], Awaitable[Any]],
        tier: str = "user_approve",
        timeout_s: Optional[float] = None,
    ) -> None:
        """Register a tool. Later phases call this for each capability.

        ``handler`` must be an ``async`` callable with signature
        ``async def fn(calling_agent: dict | None, args: dict) -> Any``.
        The return value is wrapped in the standard envelope by
        :meth:`call_tool`.
        """
        if name in self._tools:
            logger.warning("mcp_logos: tool '%s' already registered — overwriting", name)
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            tier=tier,
            timeout_s=timeout_s,
        )
        if name not in self._registered_tool_names:
            self._registered_tool_names.append(name)
        logger.info("mcp_logos: registered tool '%s' (tier=%s)", name, tier)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the MCP tools/list payload (plain dicts for JSON)."""
        return [
            {
                "name":        t.name,
                "description": t.description,
                "inputSchema": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in self._tools.values()
        ]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        calling_agent: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Dispatch a tool call and return the standard envelope.

        Envelope shape (on success)::

            {
                "ok": True,
                "data": <tool return value>,
                "error": None,
                "tool": <tool name>,
                "duration_ms": <elapsed ms>,
            }

        On failure::

            {
                "ok": False,
                "data": None,
                "error": {"type": ..., "message": ..., "recoverable": ...},
                "tool": <tool name>,
                "duration_ms": <elapsed ms>,
            }
        """
        t0 = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            return self._error_envelope(
                name,
                "ToolNotFound",
                f"Tool '{name}' is not registered on MCP server '{self.name}'",
                recoverable=False,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        try:
            timeout = tool.timeout_s or DEFAULT_TOOL_TIMEOUT_S
            result = await asyncio.wait_for(
                tool.handler(calling_agent, arguments),
                timeout=timeout,
            )
            tool.call_count += 1
            return {
                "ok":          True,
                "data":        result,
                "error":       None,
                "tool":        name,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        except asyncio.TimeoutError:
            tool.error_count += 1
            return self._error_envelope(
                name,
                "TimeoutError",
                f"Tool '{name}' exceeded timeout of {tool.timeout_s or DEFAULT_TOOL_TIMEOUT_S}s",
                recoverable=True,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            tool.error_count += 1
            # _AdapterUnavailable is a signal from platform tools that the
            # requested platform has no adapter bound. Surface it as a
            # structured, recoverable error — Phase 5.4 will unblock this
            # automatically by re-enabling adapters, so the sandbox should
            # treat it as "try again later" rather than a fatal bug.
            err_type = type(exc).__name__
            recoverable = err_type in ("_AdapterUnavailable", "AdapterUnavailable")
            if recoverable:
                err_type = "AdapterUnavailable"
            else:
                logger.warning("mcp_logos: tool '%s' raised: %s", name, exc)
            return self._error_envelope(
                name,
                err_type,
                str(exc) or "adapter not available",
                recoverable=recoverable,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    async def dispatch_method(
        self,
        method: str,
        params: Dict[str, Any],
        calling_agent: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Handle a single JSON-RPC method for this in-process server.

        ``mcp_service.handle_jsonrpc`` routes here after detecting the
        ``_logos_in_process`` marker, passing the still-unresolved
        ``calling_agent`` placeholder (None in L.1 — wired in L.2+).
        """
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo":      {"name": f"logos-mcp-{self.name}", "version": "0.1"},
            }

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": self.list_tools()}

        if method == "tools/call":
            tool_name = params.get("name", "")
            args      = params.get("arguments") or {}
            envelope  = await self.call_tool(tool_name, args, calling_agent=calling_agent)
            # MCP tools/call contract: return {content: [...], isError: bool}
            # We serialise the envelope as a single JSON text block so
            # sandbox-side code can parse it uniformly regardless of whether
            # the tool came from logos or a stdio server.
            import json as _json
            return {
                "content": [{"type": "text", "text": _json.dumps(envelope)}],
                "isError": not envelope["ok"],
            }

        # Resources / prompts are not modelled for in-process tools.
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return {key: []}

        raise ValueError(f"Unsupported MCP method for in-process server: {method}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _error_envelope(
        self,
        tool_name: str,
        err_type: str,
        message: str,
        recoverable: bool,
        duration_ms: int,
    ) -> Dict[str, Any]:
        return {
            "ok":          False,
            "data":        None,
            "error":       {
                "type":        err_type,
                "message":     message,
                "recoverable": recoverable,
            },
            "tool":        tool_name,
            "duration_ms": duration_ms,
        }


# ---------------------------------------------------------------------------
# Public registration entry point
# ---------------------------------------------------------------------------


def register_logos_server(runner: Any, service: Any) -> InProcessMCPServer:
    """Install an ``InProcessMCPServer`` named ``logos`` on ``service``.

    Called from the HTTP startup sequence in ``http_api.py`` right after
    ``MCPGatewayService.start()`` finishes booting external servers. This
    injects the logos server into both the service's config table and its
    live server map so catalogue + JSON-RPC routing find it.

    Every tool module under ``gateway.mcp_logos.tools`` that has landed in
    the current phase is auto-registered on the returned server. New tool
    modules added in later phases just need to be imported here — nothing
    in http_api.py has to change.

    Returns the server instance so callers can reach it via
    ``service._logos_server`` (set in http_api.py boot sequence).
    """
    server = InProcessMCPServer(
        name="logos",
        # Keep the description honest to what's actually registered.
        # Platform tools (platform_send / home_message) were dropped
        # 2026-04-21 — adapter ownership moved into each sandbox's
        # hermes process, so gateway-mediated messaging no longer has
        # a home here. Current surface: world roster + clock.
        description="Logos gateway capabilities — world awareness + time",
        runner=runner,
    )

    # Inject into the service so get_catalogue() and handle_jsonrpc() find
    # it. The config entry has to be present because get_catalogue iterates
    # _servers_cfg rather than _servers directly.
    try:
        service._servers_cfg["logos"] = {
            "description":  server.description,
            "category":     "gateway",
            "enabled":      True,
            "_in_process":  True,   # hint for any catalogue code that cares
        }
        with service._lock:
            service._servers["logos"] = server
        logger.info("mcp_logos: registered in-process 'logos' server on MCPGatewayService")
    except Exception as exc:
        logger.warning("mcp_logos: could not install logos server on service: %s", exc)
        raise

    # Register tool modules landed in the current phase. Each module
    # exports a ``register(server)`` function that calls
    # ``server.register_tool(...)`` for each tool it owns.
    _register_current_phase_tools(server)
    return server


def _register_current_phase_tools(server: "InProcessMCPServer") -> None:
    """Import and register all tool modules that are currently in-phase.

    Split out so tests can register a server without implicit tool side
    effects by constructing ``InProcessMCPServer`` directly.
    """
    # Phase L.2 platform tools (platform_send / home_message) were
    # removed 2026-04-21 — adapters moved into per-sandbox hermes, so
    # gateway-mediated messaging isn't a thing here any more. The
    # stub module ``tools/platform.py`` still exists (with a no-op
    # register()) to avoid breaking any external importers, but we
    # don't call it. If gateway-held adapters come back one day,
    # re-add the import here.

    # LOG-41: current-time tool. Lives outside the phase taxonomy
    # because it's a trivial read-only utility — no phase gating.
    try:
        from gateway.mcp_logos.tools import time as _time_tools
        _time_tools.register(server)
    except Exception as exc:
        logger.warning("mcp_logos: failed to register time tools: %s", exc)

    # World awareness — roster of every named agent on the system.
    # Read-only; uses build_world_snapshot() from gateway.world_awareness.
    try:
        from gateway.mcp_logos.tools import world as _world_tools
        _world_tools.register(server)
    except Exception as exc:
        logger.warning("mcp_logos: failed to register world tools: %s", exc)

    # L.3, L.4, L.5 tool modules get imported here as they land.
