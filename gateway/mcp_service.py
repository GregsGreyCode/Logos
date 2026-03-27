"""
MCPGatewayService — centralized MCP server lifecycle manager.

Reads ``mcp_servers`` and ``mcp_policy`` from ~/.logos/config.yaml, boots each
configured server once at gateway startup, and exposes them as a JSON-RPC proxy
over HTTP at /mcp/{server-name}.

Agents running in any executor mode (local process, OpenShell sandbox, Kubernetes
pod) connect to the gateway's HTTP endpoint rather than spawning their own MCP
server subprocesses.

URL resolution per execution mode:
  local process:     http://127.0.0.1:{mcp_port}/mcp/{name}
  OpenShell sandbox: http://host.docker.internal:{mcp_port}/mcp/{name}
  Kubernetes pod:    http://logos-gateway.{ns}.svc.cluster.local:{mcp_port}/mcp/{name}

The port defaults to 8081 and can be overridden with the HERMES_MCP_PORT env var.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy tier constants
# ---------------------------------------------------------------------------

TIER_AUTO   = "auto_approve"    # granted immediately, no prompt
TIER_USER   = "user_approve"    # user prompted via Telegram / web UI
TIER_ADMIN  = "admin_approve"   # requires admin account to approve
TIER_DENY   = "deny"            # always denied

_DEFAULT_TIER = TIER_USER

# ---------------------------------------------------------------------------
# MCPGatewayService
# ---------------------------------------------------------------------------


class MCPGatewayService:
    """Manages all configured MCP servers and exposes them over HTTP.

    Call ``await service.start()`` during gateway startup and
    ``await service.stop()`` during shutdown.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: The full ~/.logos/config.yaml dict (or the relevant sub-keys).
        """
        self._servers_cfg: Dict[str, dict] = config.get("mcp_servers") or {}
        self._policy_cfg:  Dict[str, list] = config.get("mcp_policy")  or {}
        # name → MCPServerTask (from tools/mcp_tool.py)
        self._servers: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._started = False

        # Build category → tier mapping from policy config
        self._auto_categories:  frozenset = frozenset(self._policy_cfg.get("auto_approve",  []))
        self._user_categories:  frozenset = frozenset(self._policy_cfg.get("user_approve",  []))
        self._admin_categories: frozenset = frozenset(self._policy_cfg.get("admin_approve", []))
        self._deny_categories:  frozenset = frozenset(self._policy_cfg.get("deny",          []))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Boot all configured MCP servers in parallel."""
        if not self._servers_cfg:
            logger.debug("mcp_service: no mcp_servers configured — service idle")
            return

        try:
            from tools.mcp_tool import _ensure_mcp_loop, _discover_and_register_server, _run_on_mcp_loop
        except ImportError:
            logger.warning("mcp_service: mcp package not installed — MCP gateway service disabled")
            return

        _ensure_mcp_loop()

        async def _start_all():
            results = await asyncio.gather(
                *(_discover_and_register_server(name, cfg)
                  for name, cfg in self._servers_cfg.items()
                  if cfg.get("enabled", True) is not False),
                return_exceptions=True,
            )
            for name, result in zip(self._servers_cfg.keys(), results):
                if isinstance(result, Exception):
                    logger.warning("mcp_service: failed to start '%s': %s", name, result)
                else:
                    logger.info("mcp_service: started '%s' (%d tools)", name, len(result))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _run_on_mcp_loop(_start_all(), timeout=120))

        # Snapshot connected servers from the mcp_tool module's _servers dict
        try:
            from tools.mcp_tool import _servers as _mcp_servers, _lock as _mcp_lock
            with _mcp_lock:
                with self._lock:
                    self._servers = dict(_mcp_servers)
        except Exception as exc:
            logger.warning("mcp_service: could not snapshot server map: %s", exc)

        self._started = True
        logger.info("mcp_service: started with %d server(s)", len(self._servers))

    async def stop(self) -> None:
        """Shut down all managed MCP servers."""
        if not self._started:
            return
        try:
            from tools.mcp_tool import shutdown_mcp_servers
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, shutdown_mcp_servers)
        except Exception as exc:
            logger.warning("mcp_service: error during shutdown: %s", exc)
        self._started = False

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------

    def get_catalogue(self) -> List[dict]:
        """Return the list of configured servers with metadata (no tool details)."""
        catalogue = []
        for name, cfg in self._servers_cfg.items():
            if cfg.get("enabled", True) is False:
                continue
            with self._lock:
                server = self._servers.get(name)
            connected = server is not None and getattr(server, "session", None) is not None
            catalogue.append({
                "name":        name,
                "description": cfg.get("description", ""),
                "category":    cfg.get("category", "general"),
                "transport":   "http" if "url" in cfg else "stdio",
                "connected":   connected,
                "tool_count":  len(getattr(server, "_registered_tool_names", [])) if connected else 0,
                "approval_tier": self.get_policy_tier(cfg.get("category", "general")),
            })
        return catalogue

    def get_server_cfg(self, name: str) -> Optional[dict]:
        """Return the raw config for a named server, or None."""
        return self._servers_cfg.get(name)

    def is_connected(self, name: str) -> bool:
        """Return True if the named server is connected and has a live session."""
        with self._lock:
            server = self._servers.get(name)
        return server is not None and getattr(server, "session", None) is not None

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def get_policy_tier(self, category: str) -> str:
        """Return the approval tier for a given category name."""
        if category in self._deny_categories:
            return TIER_DENY
        if category in self._auto_categories:
            return TIER_AUTO
        if category in self._admin_categories:
            return TIER_ADMIN
        if category in self._user_categories:
            return TIER_USER
        return _DEFAULT_TIER

    def get_server_policy_tier(self, server_name: str) -> str:
        """Return the approval tier for a configured server by name."""
        cfg = self._servers_cfg.get(server_name, {})
        return self.get_policy_tier(cfg.get("category", "general"))

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------

    def get_server_url(self, name: str, platform: str = "local") -> str:
        """Return the HTTP URL an agent should use to connect to this server.

        Args:
            name:     Server name as configured.
            platform: "local", "openshell", or "kubernetes".
        """
        port = int(os.getenv("HERMES_MCP_PORT", "8081"))
        if platform == "kubernetes":
            ns = os.getenv("HERMES_K8S_NAMESPACE", "hermes")
            base = f"http://logos-gateway.{ns}.svc.cluster.local:{port}"
        elif platform == "openshell":
            base = f"http://host.docker.internal:{port}"
        else:
            base = f"http://127.0.0.1:{port}"
        return f"{base}/mcp/{name}"

    # ------------------------------------------------------------------
    # JSON-RPC proxy helpers (used by mcp_handlers.py)
    # ------------------------------------------------------------------

    async def handle_jsonrpc(self, server_name: str, body: dict) -> dict:
        """Dispatch a JSON-RPC request to the upstream MCP server session.

        Supports: initialize, tools/list, tools/call, resources/list,
                  resources/read, prompts/list, prompts/get, ping.

        Returns a JSON-RPC response dict.
        """
        request_id = body.get("id")
        method     = body.get("method", "")
        params     = body.get("params") or {}

        def _err(code: int, message: str) -> dict:
            return {
                "jsonrpc": "2.0",
                "id":      request_id,
                "error":   {"code": code, "message": message},
            }

        with self._lock:
            server = self._servers.get(server_name)

        if server is None or getattr(server, "session", None) is None:
            return _err(-32000, f"MCP server '{server_name}' is not connected")

        try:
            from tools.mcp_tool import _run_on_mcp_loop
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _run_on_mcp_loop(
                    self._dispatch_to_session(server, method, params),
                    timeout=server.tool_timeout if hasattr(server, "tool_timeout") else 120,
                ),
            )
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            logger.warning("mcp_service: jsonrpc error for %s/%s: %s", server_name, method, exc)
            return _err(-32603, str(exc))

    async def _dispatch_to_session(self, server, method: str, params: dict):
        """Translate a JSON-RPC method call into an MCP ClientSession call."""
        session = server.session

        if method == "initialize":
            # Return gateway-side capabilities
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "logos-mcp-gateway", "version": "0.1"},
            }

        if method == "ping":
            return {}

        if method == "tools/list":
            result = await session.list_tools()
            tools = []
            for t in (result.tools if hasattr(result, "tools") else []):
                tools.append({
                    "name":        t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                })
            return {"tools": tools}

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            result = await session.call_tool(name, arguments=arguments)
            content = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    content.append({"type": "text", "text": block.text})
                elif hasattr(block, "data"):
                    content.append({"type": "image", "data": block.data, "mimeType": getattr(block, "mimeType", "image/png")})
            return {"content": content, "isError": bool(result.isError)}

        if method == "resources/list":
            result = await session.list_resources()
            resources = []
            for r in (result.resources if hasattr(result, "resources") else []):
                resources.append({
                    "uri":         str(r.uri),
                    "name":        r.name,
                    "description": getattr(r, "description", ""),
                    "mimeType":    getattr(r, "mimeType", ""),
                })
            return {"resources": resources}

        if method == "resources/read":
            uri = params.get("uri", "")
            result = await session.read_resource(uri)
            contents = []
            for block in (result.contents if hasattr(result, "contents") else []):
                if hasattr(block, "text"):
                    contents.append({"uri": uri, "mimeType": "text/plain", "text": block.text})
                elif hasattr(block, "blob"):
                    contents.append({"uri": uri, "mimeType": getattr(block, "mimeType", "application/octet-stream"), "blob": block.blob})
            return {"contents": contents}

        if method == "prompts/list":
            result = await session.list_prompts()
            prompts = []
            for p in (result.prompts if hasattr(result, "prompts") else []):
                prompts.append({
                    "name":        p.name,
                    "description": getattr(p, "description", ""),
                    "arguments":   [{"name": a.name, "description": getattr(a, "description", ""), "required": getattr(a, "required", False)} for a in (getattr(p, "arguments", None) or [])],
                })
            return {"prompts": prompts}

        if method == "prompts/get":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            result = await session.get_prompt(name, arguments=arguments)
            messages = []
            for m in (result.messages if hasattr(result, "messages") else []):
                messages.append({
                    "role":    m.role,
                    "content": {"type": "text", "text": m.content.text if hasattr(m.content, "text") else str(m.content)},
                })
            return {"description": getattr(result, "description", ""), "messages": messages}

        raise ValueError(f"Unsupported MCP method: {method}")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_mcp_gateway_config() -> dict:
    """Load mcp_servers + mcp_policy from ~/.logos/config.yaml."""
    try:
        from hermes_cli.config import load_config
        return load_config()
    except Exception as exc:
        logger.debug("mcp_service: could not load config: %s", exc)
        return {}
