"""API handlers for the Tools tab — MCP server management.

Endpoints under /api/tools/ for listing, deploying, and managing MCP
tool servers from the dashboard.
"""

import json
import logging
from aiohttp import web

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────��─────────────────────────────────────

def _get_auth_db():
    import gateway.auth.db as auth_db
    return auth_db


def _json(data, status=200):
    return web.json_response(data, status=status)


# ── Catalogue ───────��────────────────────────────────────��───────────────────

async def handle_catalogue(request: web.Request) -> web.Response:
    """GET /api/tools/catalogue — merged built-in + remote catalogue."""
    from gateway.mcp_catalogue import get_catalogue
    db = _get_auth_db()
    flags = db.get_platform_feature_flags()
    remote_url = flags.get("mcp_catalogue_url")
    entries = get_catalogue(remote_url=remote_url)
    return _json({"catalogue": entries})


# ── Server list (DB managed + config-file read-only) ─��───────────────────────

async def handle_servers_list(request: web.Request) -> web.Response:
    """GET /api/tools/servers — all managed + config-file servers."""
    db = _get_auth_db()
    db_servers = db.list_mcp_servers()

    # Config-file servers from MCPGatewayService
    config_servers = []
    svc = request.app.get("mcp_service")
    if svc:
        for entry in svc.get_catalogue():
            config_servers.append({
                "id": f"config_{entry['name']}",
                "name": entry["name"],
                "source": "config",
                "status": "running" if entry.get("connected") else "disconnected",
                "deploy_mode": "config",
                "url": entry.get("url", ""),
                "description": entry.get("description") or entry.get("category", ""),
                "tool_count": entry.get("tool_count", 0),
                "category": entry.get("category", "general"),
                "readonly": True,
            })

    # Merge: DB servers first, then config servers not already in DB
    db_names = {s["name"] for s in db_servers}
    merged = list(db_servers)
    for cs in config_servers:
        if cs["name"] not in db_names:
            merged.append(cs)

    return _json({"servers": merged})


# ── Create server ─────────────────��──────────────────────────────────────────

async def handle_server_create(request: web.Request) -> web.Response:
    """POST /api/tools/servers — create a new managed server (deploy or external)."""
    db = _get_auth_db()
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, 400)

    name = (body.get("name") or "").strip()
    if not name:
        return _json({"error": "name is required"}, 400)

    if db.get_mcp_server_by_name(name):
        return _json({"error": f"Server '{name}' already exists"}, 409)

    deploy_mode = body.get("deploy_mode", "external")
    catalogue_id = body.get("catalogue_id")
    config_values = body.get("config", {})
    url = (body.get("url") or "").strip()
    token = (body.get("token") or "").strip()

    # Look up catalogue entry for defaults
    cat_entry = None
    if catalogue_id:
        from gateway.mcp_catalogue import get_catalogue_entry
        cat_entry = get_catalogue_entry(catalogue_id)

    server = db.create_mcp_server(
        name=name,
        catalogue_id=catalogue_id,
        source="ui" if deploy_mode == "k8s" else "external",
        deploy_mode=deploy_mode,
        url=url if deploy_mode == "external" else None,
        token=token if deploy_mode == "external" else None,
        k8s_image=cat_entry["image"] if cat_entry else body.get("image"),
        config_json=json.dumps(config_values),
        tools_filter=json.dumps(body.get("tools_filter", {})),
        category=cat_entry["category"] if cat_entry else body.get("category", "general"),
        description=cat_entry["description"] if cat_entry else body.get("description"),
    )

    # If k8s deploy, trigger it
    if deploy_mode == "k8s":
        try:
            result = await _deploy_server(request.app, server, cat_entry, config_values)
            server = db.update_mcp_server(server["id"], **result)
        except Exception as exc:
            logger.exception("Failed to deploy MCP server %s", name)
            db.update_mcp_server(server["id"], status="error", last_error=str(exc))
            server = db.get_mcp_server(server["id"])

    # If external, auto-wire immediately
    if deploy_mode == "external" and url:
        db.update_mcp_server(server["id"], status="external")
        server = db.get_mcp_server(server["id"])
        try:
            await _auto_wire_server(request.app, name, url, token)
        except Exception as exc:
            logger.warning("Auto-wire failed for external server %s: %s", name, exc)

    return _json({"server": server}, 201)


# ── Delete server ──────��─────────────────────────────────────────────────────

async def handle_server_delete(request: web.Request) -> web.Response:
    """DELETE /api/tools/servers/{id} — undeploy + delete."""
    db = _get_auth_db()
    server_id = request.match_info["id"]
    server = db.get_mcp_server(server_id)
    if not server:
        return _json({"error": "not_found"}, 404)

    # Undeploy from k8s if applicable
    if server["deploy_mode"] == "k8s" and server["status"] not in ("pending", "error"):
        try:
            from gateway.mcp_deploy import undeploy_mcp_server
            undeploy_mcp_server(server["name"], namespace=server.get("k8s_namespace") or "hermes")
        except Exception as exc:
            logger.warning("Failed to undeploy %s: %s", server["name"], exc)

    # Un-wire from gateway
    try:
        await _auto_unwire_server(request.app, server["name"])
    except Exception as exc:
        logger.warning("Auto-unwire failed for %s: %s", server["name"], exc)

    db.delete_mcp_server(server_id)
    return _json({"ok": True})


# ── Update server ─────────────────────────────���──────────────────────────────

async def handle_server_update(request: web.Request) -> web.Response:
    """PATCH /api/tools/servers/{id} — update config/settings."""
    db = _get_auth_db()
    server_id = request.match_info["id"]
    server = db.get_mcp_server(server_id)
    if not server:
        return _json({"error": "not_found"}, 404)

    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, 400)

    updates = {}
    for field in ("url", "token", "description", "category", "enabled", "auto_wire", "config_json", "tools_filter"):
        if field in body:
            updates[field] = body[field]

    server = db.update_mcp_server(server_id, **updates)
    return _json({"server": server})


# ── Restart server connection ───────────��────────────────────────────────────

async def handle_server_restart(request: web.Request) -> web.Response:
    """POST /api/tools/servers/{id}/restart — restart MCP connection."""
    db = _get_auth_db()
    server_id = request.match_info["id"]
    server = db.get_mcp_server(server_id)
    if not server:
        return _json({"error": "not_found"}, 404)

    url = server.get("url")
    token = server.get("token")
    if not url:
        return _json({"error": "Server has no URL configured"}, 400)

    try:
        await _auto_wire_server(request.app, server["name"], url, token)
        db.update_mcp_server(server_id, status="running", last_error=None)
        return _json({"ok": True})
    except Exception as exc:
        db.update_mcp_server(server_id, status="error", last_error=str(exc))
        return _json({"ok": False, "error": str(exc)})


# ── Health check ──────────���─────────────────────────��────────────────────────

async def handle_server_health(request: web.Request) -> web.Response:
    """GET /api/tools/servers/{id}/health — live health check."""
    db = _get_auth_db()
    server_id = request.match_info["id"]
    server = db.get_mcp_server(server_id)
    if not server:
        return _json({"error": "not_found"}, 404)

    if server["deploy_mode"] == "k8s":
        from gateway.mcp_deploy import get_mcp_deploy_status
        status = get_mcp_deploy_status(server["name"], namespace=server.get("k8s_namespace") or "hermes")
        return _json(status)

    # External: check if MCP gateway has it connected
    svc = request.app.get("mcp_service")
    if svc and svc.is_connected(server["name"]):
        return _json({"status": "running", "connected": True})
    return _json({"status": "disconnected", "connected": False})


# ── Internal: deploy + wire helpers ───────────���──────────────────────────────

async def _deploy_server(app, server: dict, cat_entry: dict | None, config_values: dict) -> dict:
    """Deploy an MCP server to k8s and return DB update fields."""
    import asyncio
    from gateway.mcp_deploy import deploy_mcp_server

    image = server.get("k8s_image") or (cat_entry or {}).get("image", "")
    port = (cat_entry or {}).get("port", 8000)
    mcp_path = (cat_entry or {}).get("mcp_path", "/mcp")
    resources = (cat_entry or {}).get("resources")

    # Split config into secret vs plain env vars
    secret_keys = set()
    if cat_entry:
        for field in cat_entry.get("config_schema", []):
            if field.get("type") == "secret":
                secret_keys.add(field["key"])

    env_vars = {}
    secret_vars = {}
    # Always set transport to streamable-http for k8s deployments
    env_vars["MCP_TRANSPORT"] = "streamable-http"
    for k, v in config_values.items():
        if k in secret_keys:
            secret_vars[k] = str(v)
        else:
            env_vars[k] = str(v)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: deploy_mcp_server(
        name=server["name"],
        image=image,
        port=port,
        env_vars=env_vars,
        secret_vars=secret_vars,
        resources=resources,
        mcp_path=mcp_path,
    ))

    # Auto-wire after deploy
    url = result["url"]
    token = config_values.get("MCP_CLIENT_TOKEN", "")
    try:
        await _auto_wire_server(app, server["name"], url, token)
    except Exception as exc:
        logger.warning("Auto-wire after deploy failed for %s (server may still be starting): %s", server["name"], exc)

    return {
        "url": url,
        "k8s_namespace": result["namespace"],
        "status": "deploying",
    }


async def _auto_wire_server(app, name: str, url: str, token: str = ""):
    """Wire a server into the running MCPGatewayService."""
    svc = app.get("mcp_service")
    if not svc:
        logger.debug("No MCP gateway service — skipping auto-wire for %s", name)
        return

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    server_cfg = {
        "url": url,
        "transport": "streamable-http",
        "headers": headers,
        "tools": {"resources": False, "prompts": False},
    }

    # Add to gateway config and trigger discovery
    svc._servers_cfg[name] = server_cfg
    try:
        await svc.restart_server(name)
    except Exception:
        # Server might not be ready yet (k8s pod still starting)
        logger.debug("restart_server failed for %s (may still be starting)", name)


async def _auto_unwire_server(app, name: str):
    """Remove a server from the running MCPGatewayService."""
    svc = app.get("mcp_service")
    if not svc:
        return

    svc._servers_cfg.pop(name, None)
    with svc._lock:
        server = svc._servers.pop(name, None)
    if server and hasattr(server, "shutdown"):
        try:
            await server.shutdown()
        except Exception:
            pass


# ── Route registration ─────────────────���─────────────────────────────────────

def register_routes(app: web.Application):
    """Register /api/tools/* routes on the aiohttp app."""
    app.router.add_get("/api/tools/catalogue", handle_catalogue)
    app.router.add_get("/api/tools/servers", handle_servers_list)
    app.router.add_post("/api/tools/servers", handle_server_create)
    app.router.add_patch("/api/tools/servers/{id}", handle_server_update)
    app.router.add_delete("/api/tools/servers/{id}", handle_server_delete)
    app.router.add_post("/api/tools/servers/{id}/restart", handle_server_restart)
    app.router.add_get("/api/tools/servers/{id}/health", handle_server_health)
