"""API handlers for the Tools tab — MCP server management.

Endpoints under /api/tools/ for listing, deploying, and managing MCP
tool servers from the dashboard.
"""

import asyncio
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
                # Forward tool_names so the dashboard can show the actual
                # tool list when the row is expanded, instead of the user
                # seeing "2 tools" with no way to find out which two.
                "tool_names": entry.get("tool_names", []),
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
    """POST /api/tools/servers — create a new managed server (external only).

    Container-based deploy is being rewritten (Docker-based) — until that
    lands, ``deploy_mode`` must be ``external``. The previous k8s path has
    been removed along with the rest of the legacy sandbox runtimes.
    """
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
    if deploy_mode == "k8s":
        return _json(
            {"error": "k8s_removed",
             "detail": "k8s sandbox deploy was removed; use deploy_mode=external "
                       "(or deploy_mode=docker, coming next)"},
            status=400,
        )
    if deploy_mode not in ("external", "docker"):
        return _json({"error": f"unsupported deploy_mode: {deploy_mode}"}, 400)

    catalogue_id = body.get("catalogue_id")
    config_values = body.get("config", {})
    url = (body.get("url") or "").strip()
    token = (body.get("token") or "").strip()

    # Look up catalogue entry for defaults + docker deploy parameters
    cat_entry = None
    if catalogue_id:
        from gateway.mcp_catalogue import get_catalogue_entry
        cat_entry = get_catalogue_entry(catalogue_id)

    # Validate docker deploy has an image to pull — either the user
    # provided it in ``body.image`` or it came from the catalogue.
    if deploy_mode == "docker":
        image = (body.get("image") or (cat_entry or {}).get("image") or "").strip()
        if not image:
            return _json(
                {"error": "missing_image",
                 "detail": "deploy_mode=docker requires either body.image or a catalogue entry with 'image' set"},
                400,
            )

    server = db.create_mcp_server(
        name=name,
        catalogue_id=catalogue_id,
        source="external" if deploy_mode == "external" else "ui",
        deploy_mode=deploy_mode,
        url=url if deploy_mode == "external" else None,
        token=token if deploy_mode == "external" else None,
        config_json=json.dumps(config_values),
        tools_filter=json.dumps(body.get("tools_filter", {})),
        category=cat_entry["category"] if cat_entry else body.get("category", "general"),
        description=cat_entry["description"] if cat_entry else body.get("description"),
    )

    # External: just auto-wire to the user-provided URL.
    if deploy_mode == "external" and url:
        db.update_mcp_server(server["id"], status="external")
        server = db.get_mcp_server(server["id"])
        try:
            await _auto_wire_server(request.app, name, url, token)
        except Exception as exc:
            logger.warning("Auto-wire failed for external server %s: %s", name, exc)

    # Docker: pull the image, start the container, then auto-wire the
    # local URL ``docker run`` assigned. Failure at any step flips the
    # row to status='error' so the UI surfaces it; the row still exists
    # so the user can retry via the restart button without recreating.
    if deploy_mode == "docker":
        from gateway.mcp_docker_deploy import deploy_container
        try:
            port        = int((cat_entry or {}).get("port") or body.get("port") or 8000)
            mcp_path    = (cat_entry or {}).get("mcp_path") or body.get("mcp_path") or "/mcp"
            env_vars    = dict(config_values)  # catalogue config_schema fills env_vars directly
            result = await asyncio.to_thread(
                deploy_container,
                name=name, image=image, port=port,
                env_vars=env_vars, mcp_path=mcp_path,
            )
            db.update_mcp_server(
                server["id"],
                status="running",
                url=result["url"],
                last_error=None,
            )
            server = db.get_mcp_server(server["id"])
            try:
                await _auto_wire_server(request.app, name, result["url"], token="")
            except Exception as exc:
                logger.warning("Auto-wire failed for docker server %s: %s", name, exc)
        except Exception as exc:
            logger.exception("Failed to deploy MCP container %s", name)
            db.update_mcp_server(
                server["id"], status="error", last_error=str(exc)[:300],
            )
            server = db.get_mcp_server(server["id"])

    return _json({"server": server}, 201)


# ── Delete server ──────��─────────────────────────────────────────────────────

async def handle_server_delete(request: web.Request) -> web.Response:
    """DELETE /api/tools/servers/{id} — unwire + delete.

    For docker-deployed servers the container is also stopped and
    removed so ``docker ps`` doesn't keep the now-orphaned container
    running. Errors during container removal are logged and the
    gateway DB row is still deleted — the user expects a clean slate
    after they click delete.
    """
    db = _get_auth_db()
    server_id = request.match_info["id"]
    server = db.get_mcp_server(server_id)
    if not server:
        return _json({"error": "not_found"}, 404)

    # Un-wire from gateway
    try:
        await _auto_unwire_server(request.app, server["name"])
    except Exception as exc:
        logger.warning("Auto-unwire failed for %s: %s", server["name"], exc)

    # Stop + remove the Docker container for docker-deployed servers.
    if server.get("deploy_mode") == "docker":
        from gateway.mcp_docker_deploy import undeploy_container
        try:
            await asyncio.to_thread(undeploy_container, server["name"])
        except Exception as exc:
            logger.warning("Undeploy container %s failed: %s", server["name"], exc)

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

    # External: check if MCP gateway has it connected
    svc = request.app.get("mcp_service")
    if svc and svc.is_connected(server["name"]):
        return _json({"status": "running", "connected": True})
    return _json({"status": "disconnected", "connected": False})


# ── Internal: wire helpers ────────────────────────────���──────────────────────

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
