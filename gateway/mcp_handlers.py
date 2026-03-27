"""
aiohttp route handlers for the gateway MCP service.

Routes registered in http_api.py:
    GET  /api/mcp/catalogue          → handle_catalogue
    GET  /api/mcp/status             → handle_mcp_status
    POST /api/mcp/grants/{sid}/{srv} → handle_grant
    DELETE /api/mcp/grants/{sid}/{srv} → handle_revoke
    POST|GET /mcp/{server_name}      → handle_mcp_proxy   (JSON-RPC proxy)
    POST|GET /mcp/{server_name}/{tail:.*} → handle_mcp_proxy

The /mcp/{name} proxy implements a lightweight StreamableHTTP-compatible
JSON-RPC forwarder: POST for requests, GET for SSE (notification streams).
Agents authenticate with a bearer token (HERMES_INTERNAL_TOKEN or a valid
session JWT). Access is gated on mcp_access.has_access(session_id, server).
"""

import json
import logging

from aiohttp import web

from gateway.mcp_access import (
    grant_access,
    has_access,
    revoke_access,
    get_grants,
    all_grants,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal token for machine-to-machine calls (same-host agents)
# ---------------------------------------------------------------------------

def _internal_token() -> str:
    import os
    return os.getenv("HERMES_INTERNAL_TOKEN", "")


def _get_bearer_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _get_session_id_from_request(request: web.Request) -> str:
    """Extract session_id from header, query param, or body."""
    # Prefer explicit header
    sid = request.headers.get("X-Session-Id", "")
    if sid:
        return sid
    # Query param
    sid = request.rel_url.query.get("session_id", "")
    if sid:
        return sid
    return ""


async def _require_mcp_auth(request: web.Request, server_name: str) -> tuple[bool, str]:
    """Return (ok, session_id).  Checks internal token OR session grant."""
    token = _get_bearer_token(request)
    internal = _internal_token()

    # Machine-to-machine internal token bypasses grant check (gateway calls itself)
    if internal and token == internal:
        sid = _get_session_id_from_request(request)
        return True, sid

    # Normal agent call: must have a valid session grant
    sid = _get_session_id_from_request(request)
    if not sid:
        return False, ""
    if not has_access(sid, server_name):
        return False, sid
    return True, sid


# ---------------------------------------------------------------------------
# Catalogue & status
# ---------------------------------------------------------------------------

async def handle_catalogue(request: web.Request) -> web.Response:
    """GET /api/mcp/catalogue — list available MCP servers with metadata."""
    svc = request.app.get("mcp_service")
    if not svc:
        return web.json_response({"servers": []})
    return web.json_response({"servers": svc.get_catalogue()})


async def handle_mcp_status(request: web.Request) -> web.Response:
    """GET /api/mcp/status — connection status + active grants summary."""
    svc = request.app.get("mcp_service")
    catalogue = svc.get_catalogue() if svc else []
    grants_snapshot = {sid: list(servers) for sid, servers in all_grants().items()}
    return web.json_response({
        "servers":        catalogue,
        "active_grants":  grants_snapshot,
        "total_sessions": len(grants_snapshot),
    })


# ---------------------------------------------------------------------------
# Manual grant management (admin/operator use)
# ---------------------------------------------------------------------------

async def handle_grant(request: web.Request) -> web.Response:
    """POST /api/mcp/grants/{session_id}/{server} — manually grant access."""
    sid    = request.match_info["session_id"]
    server = request.match_info["server"]

    svc = request.app.get("mcp_service")
    if svc and not svc.is_connected(server):
        return web.json_response({"ok": False, "error": f"Server '{server}' is not connected"}, status=404)

    grant_access(sid, server)
    logger.info("mcp_handlers: manual grant session=%s server=%s", sid, server)
    return web.json_response({"ok": True, "session_id": sid, "server": server})


async def handle_revoke(request: web.Request) -> web.Response:
    """DELETE /api/mcp/grants/{session_id}/{server} — revoke access."""
    sid    = request.match_info["session_id"]
    server = request.match_info["server"]
    revoke_access(sid, server)
    logger.info("mcp_handlers: revoked session=%s server=%s", sid, server)
    return web.json_response({"ok": True, "session_id": sid, "server": server})


# ---------------------------------------------------------------------------
# JSON-RPC proxy
# ---------------------------------------------------------------------------

async def handle_mcp_proxy(request: web.Request) -> web.Response:
    """POST /mcp/{server_name} — JSON-RPC proxy to the gateway-managed MCP server.

    Accepts both direct JSON-RPC POSTs and MCP StreamableHTTP POSTs.
    GET requests return an empty SSE stream (notification path — agents that
    use streamablehttp_client will open a GET for server-sent events; we
    acknowledge with an empty stream so the handshake completes).
    """
    server_name = request.match_info.get("server_name", "")
    svc = request.app.get("mcp_service")

    if not svc:
        raise web.HTTPServiceUnavailable(reason="MCP gateway service not running")

    if not svc.is_connected(server_name):
        raise web.HTTPNotFound(reason=f"MCP server '{server_name}' not found or not connected")

    # Auth check
    ok, session_id = await _require_mcp_auth(request, server_name)
    if not ok:
        if not session_id:
            raise web.HTTPUnauthorized(reason="Missing X-Session-Id header or session_id query param")
        raise web.HTTPForbidden(reason=f"MCP access to '{server_name}' not granted for this session")

    # GET → SSE stream stub (keepalive for streamablehttp_client notification channel)
    if request.method == "GET":
        resp = web.StreamResponse(headers={
            "Content-Type":  "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        # Send a comment to confirm the stream is open, then keep alive
        await resp.write(b": mcp-gateway-sse-ready\n\n")
        # Hold open until client disconnects
        try:
            while True:
                import asyncio as _asyncio
                await _asyncio.sleep(15)
                await resp.write(b": ping\n\n")
        except (ConnectionResetError, Exception):
            pass
        return resp

    # POST → JSON-RPC dispatch
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    # Handle batch requests (JSON-RPC 2.0 batch)
    if isinstance(body, list):
        responses = []
        for item in body:
            resp_item = await svc.handle_jsonrpc(server_name, item)
            responses.append(resp_item)
        return web.json_response(responses)

    result = await svc.handle_jsonrpc(server_name, body)
    return web.json_response(result)
