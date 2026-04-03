"""
HTTP API server for the Hermes gateway.

Provides:
  GET  /          — unified admin dashboard (no auth)
  GET  /health    — health check (no auth)
  GET  /status    — agent execution status JSON (no auth)
  GET  /sessions  — list active sessions (Bearer auth)
  POST /chat      — send a message, SSE stream (no auth — same-origin dashboard)
  GET  /canary/status                     — probe hermes-canary in-cluster health (active: bool)
  GET  /proxy/state                       — proxy → ai-router /admin/state
  POST /proxy/providers/{key}/toggle      — proxy → ai-router /admin/providers/{key}/toggle
"""

import asyncio
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import yaml

import aiohttp
from aiohttp import web

from gateway.auth import db as auth_db
from gateway.auth.handlers import (
    handle_audit_logs,
    handle_login,
    handle_logout,
    handle_me,
    handle_refresh,
    handle_users_list,
    handle_users_me_patch,
    handle_users_patch,
    handle_users_post,
)
from gateway import admin_handlers
from gateway.auth.middleware import auth_middleware, check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password
from gateway.auth.rbac import can_spawn
from gateway.config import Platform
from gateway.session import SessionSource, build_session_context, build_session_context_prompt

logger = logging.getLogger(__name__)

_start_time: float = 0.0
_hermes_home: Path = Path(
    os.environ.get("LOGOS_HOME")
    or os.environ.get("HERMES_HOME")
    or str(Path.home() / ".logos")
)
_AI_ROUTER_BASE = os.environ.get(
    "AI_ROUTER_BASE",
    "http://ai-router.hermes.svc.cluster.local:9001",
)
_CANARY_HEALTH_URL = "http://hermes-canary.hermes.svc.cluster.local/health"
_INSTANCE_NAME = os.environ.get("HERMES_INSTANCE_NAME", "Hermes")
_IS_CANARY = os.environ.get("HERMES_IS_CANARY", "").lower() in ("1", "true", "yes")
_RUNTIME_MODE = os.environ.get("HERMES_RUNTIME_MODE", "kubernetes")  # "local" | "kubernetes"

try:
    # Read directly from pyproject.toml — immune to stale installed metadata
    import tomllib as _tomllib
    with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as _f:
        _APP_VERSION = _tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        _APP_VERSION = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        _APP_VERSION = "dev"
_BUILD_SHA = os.environ.get("BUILD_SHA", "local")[:7]
_VERSION_LABEL = f"v{_APP_VERSION} · {_BUILD_SHA}{' · canary' if _IS_CANARY else ''}"
_SERVER_START_TS = str(int(__import__("time").time()))  # unique per pod start; used to invalidate setup localStorage
# K8s constants and helpers — extracted to gateway/executors/k8s_helpers.py
from gateway.executors.k8s_helpers import (
    HERMES_NAMESPACE as _HERMES_NAMESPACE,
    INSTANCE_CPU_REQUEST as _INSTANCE_CPU_REQUEST,
    INSTANCE_MEM_REQUEST as _INSTANCE_MEM_REQUEST,
    INSTANCE_CPU_LIMIT as _INSTANCE_CPU_LIMIT,
    INSTANCE_MEM_LIMIT as _INSTANCE_MEM_LIMIT,
    SPAWN_CPU_THRESHOLD as _SPAWN_CPU_THRESHOLD,
    SPAWN_MEM_THRESHOLD as _SPAWN_MEM_THRESHOLD,
    k8s_clients as _k8s_clients,
    safe_k8s_name as _safe_k8s_name,
    cluster_resources as _cluster_resources,
    list_hermes_instances as _list_hermes_instances,
    delete_hermes_instance as _delete_instance,
)

# In-memory request queue for instances that couldn't spawn due to resource constraints
_instance_queue: list[dict] = []

# ── Soul Registry — re-exported from gateway.souls ────────────────────────────
from gateway.souls import (  # noqa: E402
    SoulManifest as SoulManifest,
    load_souls as _load_souls,
    get_soul_registry as _get_soul_registry,
    validate_soul_overrides as _validate_soul_overrides,
    compute_effective_toolsets as _compute_effective_toolsets,
)
# _SOUL_REGISTRY alias for the one place that accesses it directly (startup + admin page)
import gateway.souls as _souls_module

_SOULS_DIR = pathlib.Path(__file__).parent.parent / "souls"



# Stable epoch for hue-cycle phase-locking across all browser tabs and the tray icon.
_HUE_EPOCH_MS: int = int(time.time() * 1000)


# When running as a PyInstaller frozen executable, __file__ points inside the
# zip archive and "html/" must be resolved via sys._MEIPASS instead.
import sys as _sys
_html_dir = (
    Path(_sys._MEIPASS) / "gateway" / "html"
    if getattr(_sys, "frozen", False)
    else Path(__file__).parent / "html"
)
_ADMIN_HTML  = (_html_dir / "main_app.html").read_text(encoding="utf-8")
_LOGIN_HTML  = (_html_dir / "login.html").read_text(encoding="utf-8")
_SETUP_HTML  = (_html_dir / "setup.html").read_text(encoding="utf-8")


def _check_auth(request: web.Request) -> bool:
    """Legacy internal-token check — still used by /sessions endpoint."""
    token = os.environ.get("HERMES_INTERNAL_TOKEN", "")
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {token}"


def _ensure_admin_exists() -> None:
    """Seed the first admin account from env vars if the users table is empty."""
    admin_email = os.environ.get("HERMES_ADMIN_EMAIL", "").strip()
    admin_pass  = os.environ.get("HERMES_ADMIN_PASSWORD", "").strip()
    if not admin_email or not admin_pass:
        return
    try:
        # Skip if any admin user already exists — the admin may have changed their
        # email during setup, so checking by email alone would re-create a duplicate.
        _, total = auth_db.list_users(page=1, limit=1, role="admin")
        if total > 0:
            return
        auth_db.create_user(
            email=admin_email,
            username="admin",
            password_hash=hash_password(admin_pass),
            role="admin",
            display_name=os.environ.get("HERMES_ADMIN_NAME", "Admin"),
        )
        logger.info("Seeded admin account: %s", admin_email)
    except Exception as exc:
        logger.warning("Failed to seed admin account: %s", exc)


# ── Unified Services (tool credentials) ──────────────────────────────────

async def _handle_services_catalogue(request: web.Request) -> web.Response:
    """GET /api/services — unified catalogue of MCP servers + tool integrations."""
    from gateway.services import get_tool_integrations
    mcp_servers = []
    try:
        svc = request.app.get("mcp_service")
        if svc:
            mcp_servers = svc.get_catalogue()
    except Exception:
        pass
    # Read inference settings from config
    inference_cfg = {}
    try:
        import yaml as _yaml
        _hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
        _cfg_path = _hermes_home / "config.yaml"
        if _cfg_path.exists():
            _cfg = _yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
            _lms = _cfg.get("lmstudio") or {}
            inference_cfg = {
                "n_parallel": _lms.get("n_parallel", 2),
                "server_type": os.environ.get("HERMES_SERVER_TYPE", ""),
                "model": os.environ.get("HERMES_MODEL", ""),
                "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            }
    except Exception:
        pass

    return web.json_response({
        "mcp_servers": mcp_servers,
        "tool_integrations": get_tool_integrations(),
        "inference": inference_cfg,
    })


async def _handle_services_set_key(request: web.Request) -> web.Response:
    """POST /api/services/keys — set a tool credential (admin only)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "error": "env_var and value required"}, status=400)
    from gateway.services import set_credential, get_tool_integrations
    set_credential(env_var, value)
    return web.json_response({"ok": True, "integrations": get_tool_integrations()})


async def _handle_services_delete_key(request: web.Request) -> web.Response:
    """DELETE /api/services/keys — remove a tool credential (admin only)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    if not env_var:
        return web.json_response({"ok": False, "error": "env_var required"}, status=400)
    from gateway.services import delete_credential, get_tool_integrations
    delete_credential(env_var)
    return web.json_response({"ok": True, "integrations": get_tool_integrations()})


async def _handle_services_inference(request: web.Request) -> web.Response:
    """POST /api/services/inference — save inference server settings (n_parallel, etc.)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    n_parallel = body.get("n_parallel")
    if not isinstance(n_parallel, int) or n_parallel < 1 or n_parallel > 16:
        return web.json_response({"ok": False, "error": "n_parallel must be 1-16"}, status=400)
    # Save to config.yaml
    try:
        import yaml as _yaml
        _hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
        _cfg_path = _hermes_home / "config.yaml"
        _cfg = {}
        if _cfg_path.exists():
            _cfg = _yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
        _cfg.setdefault("lmstudio", {})["n_parallel"] = n_parallel
        _cfg_path.write_text(_yaml.dump(_cfg, default_flow_style=False, allow_unicode=True))
        logger.info("services: n_parallel set to %d", n_parallel)
        return web.json_response({"ok": True, "n_parallel": n_parallel})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _handle_services_validate_key(request: web.Request) -> web.Response:
    """POST /api/services/validate — test a credential with a real API call."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "message": "env_var and value required"}, status=400)
    from gateway.services import validate_credential
    result = await validate_credential(env_var, value)
    return web.json_response(result)


async def _handle_setup_page(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if is_setup_completed():
        raise web.HTTPFound("/")
    html = _SETUP_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL).replace("__SETUP_TS__", _SERVER_START_TS)
    return web.Response(text=html, content_type="text/html")


async def _handle_setup_status(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    return web.json_response({"completed": is_setup_completed()})


async def _handle_setup_reset(request: web.Request) -> web.Response:
    from gateway.auth.db import reset_setup_completed, write_audit_log
    user_id = request["current_user"]["sub"]
    reset_setup_completed()
    write_audit_log(user_id, "setup_reset", ip_address=request.remote)
    return web.json_response({"ok": True})


async def _handle_index(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if not is_setup_completed():
        raise web.HTTPFound("/login")
    inject = f'<script>window.__LOGOS__={{isCanary:{str(_IS_CANARY).lower()},runtimeMode:"{_RUNTIME_MODE}",version:"{_VERSION_LABEL}"}};window._hueEpochMs={_HUE_EPOCH_MS};</script>'
    html = _ADMIN_HTML.replace("</head>", inject + "</head>", 1)
    return web.Response(text=html, content_type="text/html")


async def _handle_login_page(request: web.Request) -> web.Response:
    html = _LOGIN_HTML.replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html")


async def _handle_log_tail(request: web.Request) -> web.Response:
    """Return the last N lines of the gateway log file.

    GET /api/logs?n=200&file=gateway   (file: gateway|errors)
    Requires view_audit_logs permission (admin/operator).
    """
    n = min(int(request.query.get("n", 200)), 2000)
    fname = request.query.get("file", "gateway")
    if fname not in ("gateway", "errors"):
        return web.json_response({"error": "invalid file"}, status=400)
    log_path = _hermes_home / "logs" / f"{fname}.log"
    try:
        if not log_path.exists():
            return web.json_response({"lines": [], "path": str(log_path), "exists": False})
        # Read last N lines efficiently without loading the whole file
        lines: list[str] = []
        with open(log_path, "rb") as fh:
            # Seek backwards in chunks to find the last N newlines
            chunk = 1024 * 32
            fh.seek(0, 2)  # end
            size = fh.tell()
            buf = b""
            pos = size
            while len(lines) < n + 1 and pos > 0:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                lines = buf.split(b"\n")
        lines = [l.decode("utf-8", errors="replace") for l in lines[-n:] if l]
        return web.json_response({"lines": lines, "path": str(log_path), "exists": True, "total_bytes": log_path.stat().st_size})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _handle_status(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    uptime = int(time.time() - _start_time)
    now = time.time()

    active = []
    for session_key, s in list(runner._session_status.items()):
        tool_started = s.get("tool_started_at") or now
        session_started = s.get("session_started_at") or now

        # Pull live token counts from the running agent if available
        agent = runner._running_agents.get(session_key)
        prompt_tokens = 0
        completion_tokens = 0
        api_calls = 0
        if agent is not None:
            prompt_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
            completion_tokens = getattr(agent, "session_completion_tokens", 0) or 0
            api_calls = getattr(agent, "session_api_calls", 0) or 0

        active.append({
            "session_key": session_key,
            "platform": s.get("platform", "unknown"),
            "current_tool": s.get("current_tool", "unknown"),
            "elapsed_s": int(now - tool_started),
            "tool_started_at": tool_started,
            "tool_count": s.get("tool_count", 0),
            "error_count": s.get("error_count", 0),
            "recent_tools": s.get("recent_tools", []),
            "stuck": s.get("stuck", False),
            "session_started_at": session_started,
            "elapsed_session_s": int(now - session_started),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "api_calls": api_calls,
        })

    # Recent completed sessions (ring buffer, newest last)
    recent = list(getattr(runner, "_recent_sessions", []))

    cpu_percent = None
    mem_mb = None
    try:
        import psutil as _psutil
        _proc = _psutil.Process()
        cpu_percent = round(_proc.cpu_percent(interval=None), 1)
        mem_mb = int(_proc.memory_info().rss / 1024 / 1024)
    except Exception:
        pass

    current_model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or ""

    return web.json_response({
        "status": "ok",
        "uptime_s": uptime,
        "instance_name": _INSTANCE_NAME,
        "active_sessions": active,
        "recent_sessions": recent,
        "current_model": current_model,
        "cpu_percent": cpu_percent,
        "mem_mb": mem_mb,
    })


async def _handle_model_patch(request: web.Request) -> web.Response:
    """PATCH /api/model — change the active model at runtime and persist to config.yaml."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    new_model = (body.get("model") or "").strip()
    if not new_model:
        return web.json_response({"error": "model required"}, status=400)
    _hermes_home = pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(pathlib.Path.home() / ".logos"))
    _config_path = _hermes_home / "config.yaml"
    try:
        import yaml as _yaml
        _cfg: dict = {}
        if _config_path.exists():
            with open(_config_path, encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
        _cfg["HERMES_MODEL"] = new_model
        os.environ["HERMES_MODEL"] = new_model
        with open(_config_path, "w", encoding="utf-8") as _f:
            _yaml.dump(_cfg, _f, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "model": new_model})


async def _handle_toolsets(request: web.Request) -> web.Response:
    """Return available toolsets and per-tool availability for the current install."""
    try:
        from core.model_tools import check_tool_availability
        from tools.registry import registry
        available_ts, unavailable_info = check_tool_availability(quiet=True)
        ts_meta = registry.get_available_toolsets()
        # Enrich with description from core/toolsets.py TOOLSET_REGISTRY
        try:
            from core.toolsets import TOOLSET_REGISTRY
            for name, meta in ts_meta.items():
                reg_entry = TOOLSET_REGISTRY.get(name, {})
                meta["description"] = reg_entry.get("description", "")
                meta["tools"] = reg_entry.get("tools", meta.get("tools", []))
        except Exception:
            pass
        # Include which toolsets are currently enabled in config
        try:
            from logos_cli.config import load_config
            cfg = load_config()
            enabled = cfg.get("toolsets", ["hermes-cli"])
            # Resolve the enabled toolset(s) to individual toolset names
            from core.toolsets import resolve_toolset
            enabled_tools = set()
            for ts_name in (enabled if isinstance(enabled, list) else [enabled]):
                try:
                    enabled_tools.update(resolve_toolset(ts_name))
                except Exception:
                    pass
        except Exception:
            enabled = ["hermes-cli"]
            enabled_tools = set()
        return web.json_response({
            "available": sorted(available_ts),
            "toolsets": ts_meta,
            "unavailable": unavailable_info,
            "enabled_toolsets": enabled if isinstance(enabled, list) else [enabled],
            "enabled_tools": sorted(enabled_tools),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@require_csrf
async def _handle_toolsets_toggle(request: web.Request) -> web.Response:
    """POST /api/toolsets/toggle — enable or disable a toolset in the active config.

    Body: { "toolset": "knowledge", "enabled": true }

    Updates the config.yaml toolsets list. The change takes effect on the next
    agent session (existing sessions keep their current toolset).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    toolset_name = (body.get("toolset") or "").strip()
    enabled = body.get("enabled", True)

    if not toolset_name:
        return web.json_response({"error": "toolset is required"}, status=400)

    import yaml as _yaml
    _home = pathlib.Path(
        os.environ.get("LOGOS_HOME")
        or os.environ.get("HERMES_HOME")
        or str(pathlib.Path.home() / ".logos")
    )
    _config_path = _home / "config.yaml"

    try:
        _cfg: dict = {}
        if _config_path.exists():
            with open(_config_path, encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}

        current = _cfg.get("toolsets", ["hermes-cli"])
        if not isinstance(current, list):
            current = [current]

        if enabled and toolset_name not in current:
            current.append(toolset_name)
        elif not enabled and toolset_name in current:
            current.remove(toolset_name)

        _cfg["toolsets"] = current

        with open(_config_path, "w", encoding="utf-8") as _f:
            _yaml.dump(_cfg, _f, default_flow_style=False, sort_keys=False)

    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({"ok": True, "toolsets": current})


async def _handle_canary_status(request: web.Request) -> web.Response:
    """Check if the canary pod is alive by probing its in-cluster health endpoint."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _CANARY_HEALTH_URL,
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                return web.json_response({"active": r.status < 400})
    except Exception:
        return web.json_response({"active": False})


async def _handle_proxy_state(request: web.Request) -> web.Response:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/state",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                from gateway import admin_handlers
                routes = data.get("routes", {})
                data["route_model_classes"] = {
                    alias: admin_handlers.ALIAS_TO_CLASS.get(alias, "general")
                    for alias in routes
                }
                return web.json_response(data)
    except Exception as e:
        return web.json_response({
            "providers": {},
            "routes": {},
            "route_model_classes": {},
            "grafana_url": "http://192.168.1.253:3200",
            "_error": str(e),
        })


@require_permission("manage_platform")
@require_csrf
async def _handle_proxy_toggle(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/providers/{key}/toggle",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                return web.json_response(data)
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_proxy_models_live(request: web.Request) -> web.Response:
    """GET /proxy/models-live — proxy to ai-router /admin/models-live."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_AI_ROUTER_BASE}/admin/models-live",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        return web.json_response({"providers": {}, "_error": str(e)})


@require_permission("manage_machines")
@require_csrf
async def _handle_proxy_benchmark(request: web.Request) -> web.Response:
    """POST /proxy/benchmark — proxy to ai-router /admin/benchmark."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_ROUTER_BASE}/admin/benchmark",
                json=body,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                return web.json_response(await r.json())
    except Exception as e:
        raise web.HTTPBadGateway(reason=str(e))


async def _handle_routing_claims(request: web.Request) -> web.Response:
    """GET /internal/routing/claims — full machine→user claim map for the MCP routing tool."""
    claims = auth_db.list_all_claims()
    machines = auth_db.list_machines()
    users = auth_db.list_users(limit=500)
    return web.json_response({
        "claims": claims,
        "machines": machines,
        "users": [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                   "email": u["email"], "policy_id": u.get("policy_id")} for u in users],
    })


async def _handle_routing_apply(request: web.Request) -> web.Response:
    """POST /internal/routing/apply — Hermes MCP tool applies a suggested profile.
    Body: {"user_id": str, "policy_name": str, "description": str, "rules": [...], "fallback": str}
    """
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")

    user_id = body.get("user_id")
    policy_name = body.get("policy_name")
    rules = body.get("rules", [])
    description = body.get("description", "Auto-configured by Hermes")
    fallback = body.get("fallback", "any_available")

    if not user_id or not policy_name:
        raise web.HTTPBadRequest(reason="user_id and policy_name required")

    user = auth_db.get_user_by_id(user_id)
    if not user:
        raise web.HTTPNotFound(reason="user_not_found")

    # Create or reuse policy with this name
    existing = next((p for p in auth_db.list_policies() if p["name"] == policy_name), None)
    if existing:
        policy = auth_db.update_policy(existing["id"], description=description, fallback=fallback)
        pid = existing["id"]
    else:
        policy = auth_db.create_policy(policy_name, description=description, fallback=fallback)
        pid = policy["id"]

    auth_db.set_policy_rules(pid, rules)
    auth_db.assign_user_policy(user_id, pid)

    return web.json_response({"ok": True, "policy": auth_db.get_policy(pid),
                              "rules": auth_db.get_policy_rules(pid)})


async def _handle_souls_get(request: web.Request) -> web.Response:
    registry = _get_soul_registry()
    return web.json_response({"souls": [s.to_dict() for s in registry.values()]})


async def _handle_soul_detail(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    registry = _get_soul_registry()
    soul = registry.get(slug)
    if not soul:
        raise web.HTTPNotFound(reason=f"soul not found: {slug}")
    return web.json_response(soul.to_dict(include_soul_md=True))


async def _handle_instances_get(request: web.Request) -> web.Response:
    executor = request.app["executor"]
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, executor.get_resources)
    except Exception as e:
        res = {"_error": str(e)}
    try:
        inst = await loop.run_in_executor(None, executor.list_instances)
    except Exception as e:
        inst = []
        if "_error" not in res:
            res = {"_error": str(e)}
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")
    caller_name = (caller.get("display_name") or caller.get("username") or "").lower()
    # Non-admins only see instances spawned for themselves
    if caller_role not in ("admin", "operator"):
        inst = [i for i in inst if i.get("requester", "").lower() == caller_name]
    # Normalise local-executor instances to the same shape the k8s executor returns
    # so the frontend can use a single template for both modes.
    if _RUNTIME_MODE == "local":
        registry = _get_soul_registry()
        normalized = []
        for i in inst:
            slug = i.get("soul_name", "")
            soul_obj = registry.get(slug)
            _label = i.get("instance_label", "")
        _req = i.get("requester") or i.get("name", "")
        normalized.append({
                "name":          i.get("name", ""),
                "instance_name": f"{_req} · {_label}" if _label else f"Hermes for {_req}",
                "requester":     _req,
                "instance_label": _label,
                "soul":          {"name": soul_obj.name, "slug": slug, "status": soul_obj.status}
                                 if soul_obj else {"name": slug or "default", "slug": slug, "status": "stable"},
                "model_alias":   i.get("model", ""),
                "machine_name":  None,
                "k8s_status":    "running" if i.get("healthy") else "starting",
                "status":        "running" if i.get("healthy") else "starting",
                "ready":         1 if i.get("healthy") else 0,
                "desired":       1,
                "node_port":     i.get("port"),
                "url":           i.get("url"),
                "pid":           i.get("pid"),
                "source":        "local",
                "cpu_percent":   i.get("cpu_percent"),
                "mem_mb":        i.get("mem_mb"),
            })
        inst = normalized
    return web.json_response({
        "instances": inst,
        "resources": res,
        "queue": _instance_queue,
    })


@require_csrf
async def _handle_instances_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    caller_role = caller.get("role", "viewer")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    requester = (body.get("requester") or "").strip()
    if not requester:
        return web.json_response({"error": "requester is required"}, status=400)

    soul_slug = (body.get("soul_slug") or "general").strip()
    tool_overrides = body.get("tool_overrides") or {}
    model_alias = (body.get("model_alias") or "balanced").strip()
    machine_id_override = body.get("machine_id") or None
    instance_label = (body.get("instance_label") or "").strip()

    # Validate instance_label: lowercase alphanumeric + hyphens, max 32 chars
    if instance_label:
        import re as _re
        sanitised = _re.sub(r"[^a-z0-9-]", "", instance_label.lower())
        if sanitised != instance_label or len(instance_label) > 32:
            return web.json_response(
                {"error": "invalid_label",
                 "message": "Instance label must be lowercase alphanumeric with hyphens, max 32 chars"},
                status=400,
            )

    # Validate soul and overrides before checking resources
    registry = _get_soul_registry()
    soul = registry.get(soul_slug)
    if not soul:
        return web.json_response(
            {"error": "soul_not_found", "soul_slug": soul_slug},
            status=400,
        )

    # RBAC: check if caller can spawn this soul
    if not can_spawn(caller_role, soul.to_dict()):
        return web.json_response(
            {"error": "forbidden", "message": "You don't have permission to spawn this soul"},
            status=403,
        )

    from gateway.auth.rbac import has_permission as _has_perm

    # RBAC: machine routing override requires override_routing permission
    if machine_id_override and not _has_perm(caller_role, "override_routing"):
        return web.json_response(
            {"error": "forbidden", "message": "Machine routing override requires operator or admin role"},
            status=403,
        )

    # RBAC: toolset overrides require override_toolsets permission
    if tool_overrides and not _has_perm(caller_role, "override_toolsets"):
        return web.json_response(
            {"error": "forbidden", "message": "Toolset overrides require operator or admin role"},
            status=403,
        )

    try:
        _validate_soul_overrides(soul, tool_overrides)
    except ValueError as exc:
        code, _, detail = str(exc).partition(":")
        messages = {
            "cannot_remove_enforced": f"toolset '{detail}' is enforced by soul '{soul_slug}' and cannot be removed",
            "toolset_not_available": f"toolset '{detail}' is forbidden by soul '{soul_slug}'",
            "toolset_not_in_soul": f"toolset '{detail}' is not in the optional list for soul '{soul_slug}'",
        }
        return web.json_response(
            {"error": code, "message": messages.get(code, str(exc)), "toolset": detail},
            status=400,
        )

    # Resolve routing — must happen before spawn so we can pin the machine
    caller_id = caller.get("sub")
    try:
        route = await admin_handlers.resolve_route(
            user_id=caller_id,
            model_alias=model_alias,
            machine_id_override=machine_id_override,
        )
    except admin_handlers.RoutingError as exc:
        return web.json_response(
            {"error": "routing_failed", "message": str(exc), "profile": exc.profile_name},
            status=503,
        )

    resolved_machine   = route["machine"]
    resolved_endpoint  = resolved_machine["endpoint_url"] if resolved_machine else None
    resolved_machine_name = resolved_machine["name"] if resolved_machine else None
    resolved_machine_id   = resolved_machine["id"]   if resolved_machine else None
    logger.info(
        "routing resolved: user=%s model=%s layer=%s machine=%s",
        caller_id, model_alias, route["layer"],
        resolved_machine_name or "none",
    )

    loop = asyncio.get_event_loop()
    executor = request.app["executor"]

    # Check resources via executor
    try:
        headroom = await loop.run_in_executor(None, executor.get_headroom)
        can_spawn_now = headroom.can_spawn
        headroom_reason = headroom.reason
    except Exception as e:
        can_spawn_now = False
        headroom_reason = f"executor unavailable: {e}"

    if not can_spawn_now:
        _instance_queue.append({"requester": requester, "soul_slug": soul_slug, "instance_label": instance_label or soul_slug, "reason": headroom_reason, "requested_at": time.time()})
        logger.info("Instance request queued for %s: %s", requester, headroom_reason)
        return web.json_response({"status": "queued", "requester": requester, "reason": headroom_reason})

    # Per-user instance limit
    max_instances = 5
    try:
        existing = await loop.run_in_executor(None, executor.list_instances)
        user_instances = [i for i in existing if (i.get("requester") or "").lower() == requester.lower()]
        if len(user_instances) >= max_instances:
            return web.json_response({
                "error": "instance_limit",
                "message": f"You already have {len(user_instances)} instances (limit: {max_instances}). Delete one before spawning another.",
            }, status=400)
    except Exception:
        pass  # don't block spawn if list_instances fails

    try:
        from gateway.executors.base import InstanceConfig as _IC
        # Default label to soul slug so each soul gets a distinct instance
        effective_label = instance_label or soul_slug
        _ic = _IC(
            name=_safe_k8s_name(requester, effective_label),
            soul_name=soul_slug,
            model=model_alias,
            requester=requester,
            instance_label=effective_label,
            tool_overrides=tool_overrides or {},
            machine_endpoint=resolved_endpoint,
            machine_name=resolved_machine_name,
            machine_id=resolved_machine_id,
        )
        spawned = await loop.run_in_executor(None, executor.spawn, _ic)
        if _RUNTIME_MODE == "local":
            result = {
                "status": "created" if spawned.healthy else "starting",
                "name": spawned.name,
                "url": spawned.url,
                "instance_name": f"Hermes for {requester}",
            }
        else:
            is_exists = spawned.url == "" and not spawned.healthy
            result = {
                "status": "exists" if is_exists else "created",
                "name": spawned.name,
                "instance_name": spawned.soul_name,
                "instance_label": effective_label,
                "soul": {"slug": spawned.soul_name, "name": spawned.soul_name},
            }
            if is_exists:
                result["message"] = f"An instance named '{effective_label}' already exists for {requester}. Choose a different name or delete the existing one."
    except Exception as e:
        logger.exception("Failed to spawn instance for %s", requester)
        return web.json_response({"error": "spawn_failed", "message": str(e)}, status=500)

    # Log routing decision
    auth_db.log_routing_decision(
        user_id=caller_id,
        model_alias=model_alias,
        model_class=route["model_class"],
        machine_id=resolved_machine_id,
        machine_name=resolved_machine_name,
        layer=route["layer"],
        instance_name=f"Hermes for {requester}",
    )

    # Audit: who spawned what
    auth_db.write_audit_log(
        caller.get("sub"), "spawn_instance",
        target_type="instance", target_id=requester,
        metadata={
            "soul_slug": soul_slug,
            "requester": requester,
            "model_alias": model_alias,
            "machine": resolved_machine_name,
            "routing_layer": route["layer"],
        },
        ip_address=request.remote,
    )

    # Try to resolve NodePort / URL (may take a moment to assign)
    await asyncio.sleep(1)
    try:
        instances = await loop.run_in_executor(None, executor.list_instances)
        dep_name = _ic.name
        match = next((i for i in instances if i["name"] == dep_name), {})
        result["node_port"] = match.get("node_port")
        result["instance_name"] = match.get("instance_name", f"Hermes for {requester}")
    except Exception:
        pass

    return web.json_response(result)


@require_permission("delete_instance")
@require_csrf
async def _handle_instances_delete(request: web.Request) -> web.Response:
    name   = request.match_info["name"]
    caller = request.get("current_user") or {}
    if name == "hermes":
        raise web.HTTPForbidden(reason="Cannot delete the primary hermes deployment")
    loop = asyncio.get_event_loop()
    executor = request.app["executor"]
    try:
        await loop.run_in_executor(None, executor.delete_instance, name)
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))
    auth_db.write_audit_log(
        caller.get("sub"), "delete_instance",
        target_type="instance", target_id=name,
        ip_address=request.remote,
    )
    return web.json_response({"status": "deleted", "name": name})


# ── Instance management API (memory, knowledge, config) ─────────────────────

def _instance_home(name: str) -> Path:
    """Resolve the HERMES_HOME directory for a named instance.

    The primary gateway agent ('hermes') uses _hermes_home directly.
    Spawned instances live under _hermes_home/instances/{name}/.
    """
    if name == "hermes":
        return _hermes_home
    return _hermes_home / "instances" / name


@require_permission("view_instances")
async def _handle_instance_memory_get(request: web.Request) -> web.Response:
    """GET /instances/{name}/memory — read all memory files for an instance."""
    name = request.match_info["name"]
    home = _instance_home(name)
    memories_dir = home / "memories"
    shared_dir = _hermes_home / "shared"

    def _read_safe(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            return ""

    return web.json_response({
        "instance": name,
        "memory": _read_safe(memories_dir / "MEMORY.md"),
        "user_profile": _read_safe(shared_dir / "USER.md"),
        "bug_notes": _read_safe(home / "bug_notes.md"),
    })


@require_permission("view_instances")
@require_csrf
async def _handle_instance_memory_put(request: web.Request) -> web.Response:
    """PUT /instances/{name}/memory — update a memory target for an instance."""
    name = request.match_info["name"]
    home = _instance_home(name)
    memories_dir = home / "memories"
    shared_dir = _hermes_home / "shared"

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    target = body.get("target", "")
    content = body.get("content", "")

    if target == "memory":
        memories_dir.mkdir(parents=True, exist_ok=True)
        (memories_dir / "MEMORY.md").write_text(content, encoding="utf-8")
    elif target == "user_profile":
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "USER.md").write_text(content, encoding="utf-8")
    elif target == "bug_notes":
        home.mkdir(parents=True, exist_ok=True)
        (home / "bug_notes.md").write_text(content, encoding="utf-8")
    else:
        return web.json_response(
            {"error": "invalid_target", "message": "Target must be: memory, user_profile, bug_notes"},
            status=400,
        )

    return web.json_response({"status": "updated", "target": target, "instance": name})


@require_permission("view_instances")
async def _handle_instance_knowledge_get(request: web.Request) -> web.Response:
    """GET /instances/{name}/knowledge — list sources and stats."""
    name = request.match_info["name"]
    home = _instance_home(name)

    from tools.knowledge_store import KnowledgeStore
    store = KnowledgeStore(knowledge_dir=home / "knowledge")
    sources = store.list_sources()
    stats = store.stats()

    return web.json_response({
        "instance": name,
        "sources": sources.get("sources", []),
        "stats": stats,
    })


@require_permission("view_instances")
@require_csrf
async def _handle_instance_knowledge_ingest(request: web.Request) -> web.Response:
    """POST /instances/{name}/knowledge/ingest — ingest a document."""
    name = request.match_info["name"]
    home = _instance_home(name)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    source_name = (body.get("source_name") or "").strip()
    content = body.get("content", "")

    if not source_name:
        return web.json_response({"error": "source_name is required"}, status=400)
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    from tools.knowledge_store import KnowledgeStore
    try:
        from logos_cli.config import load_config
        cfg = load_config().get("knowledge", {})
    except Exception:
        cfg = {}

    store = KnowledgeStore(
        knowledge_dir=home / "knowledge",
        embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
        embedding_endpoint=cfg.get("embedding_endpoint"),
        chunk_size=cfg.get("chunk_size", 512),
        chunk_overlap=cfg.get("chunk_overlap", 64),
        max_chunks=cfg.get("max_chunks", 10_000),
    )
    result = store.ingest(content, source_name=source_name, source_type="upload")

    status_code = 200 if result.get("success") else 400
    return web.json_response(result, status=status_code)


@require_permission("view_instances")
@require_csrf
async def _handle_instance_knowledge_delete(request: web.Request) -> web.Response:
    """DELETE /instances/{name}/knowledge/{source} — remove a knowledge source."""
    name = request.match_info["name"]
    source = request.match_info["source"]
    home = _instance_home(name)

    from tools.knowledge_store import KnowledgeStore
    store = KnowledgeStore(knowledge_dir=home / "knowledge")
    result = store.remove_source(source)

    status_code = 200 if result.get("success") else 404
    return web.json_response(result, status=status_code)


@require_permission("view_instances")
async def _handle_instance_knowledge_search(request: web.Request) -> web.Response:
    """GET /instances/{name}/knowledge/search?q=... — semantic search preview."""
    name = request.match_info["name"]
    query = request.query.get("q", "").strip()
    home = _instance_home(name)

    if not query:
        return web.json_response({"error": "query parameter 'q' is required"}, status=400)

    from tools.knowledge_store import KnowledgeStore
    try:
        from logos_cli.config import load_config
        cfg = load_config().get("knowledge", {})
    except Exception:
        cfg = {}

    store = KnowledgeStore(
        knowledge_dir=home / "knowledge",
        embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
        embedding_endpoint=cfg.get("embedding_endpoint"),
    )
    result = store.search(query, top_k=int(request.query.get("top_k", "5")))
    return web.json_response(result)


@require_permission("view_instances")
@require_csrf
async def _handle_instance_fork(request: web.Request) -> web.Response:
    """POST /instances/{name}/fork — copy memory and/or knowledge to another instance.

    Body: { "target_instance": "hermes-greg-coder", "copy_memory": true, "copy_knowledge": true }
    """
    source_name = request.match_info["name"]
    source_home = _instance_home(source_name)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    target_name = (body.get("target_instance") or "").strip()
    copy_memory = body.get("copy_memory", True)
    copy_knowledge = body.get("copy_knowledge", True)

    if not target_name:
        return web.json_response({"error": "target_instance is required"}, status=400)
    if target_name == source_name:
        return web.json_response({"error": "Cannot fork an instance onto itself"}, status=400)

    target_home = _instance_home(target_name)
    copied = []

    import shutil

    # Copy MEMORY.md
    if copy_memory:
        src_mem = source_home / "memories" / "MEMORY.md"
        if src_mem.exists():
            tgt_mem_dir = target_home / "memories"
            tgt_mem_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_mem), str(tgt_mem_dir / "MEMORY.md"))
            copied.append("MEMORY.md")

    # Copy knowledge base
    if copy_knowledge:
        src_knowledge = source_home / "knowledge"
        if src_knowledge.exists() and any(src_knowledge.iterdir()):
            tgt_knowledge = target_home / "knowledge"
            if tgt_knowledge.exists():
                shutil.rmtree(str(tgt_knowledge))
            shutil.copytree(str(src_knowledge), str(tgt_knowledge))
            copied.append("knowledge/")

    return web.json_response({
        "status": "forked",
        "source": source_name,
        "target": target_name,
        "copied": copied,
    })


def _spawn_templates_path() -> Path:
    return _hermes_home / "spawn_templates.json"


def _read_spawn_templates() -> list:
    p = _spawn_templates_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _write_spawn_templates(templates: list) -> None:
    p = _spawn_templates_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(templates, indent=2))


@require_permission("view_instances")
async def _handle_spawn_templates_get(request: web.Request) -> web.Response:
    return web.json_response(_read_spawn_templates())


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_put(request: web.Request) -> web.Response:
    """Replace the full list (client sends the already-deduped, ordered list)."""
    body = await request.json()
    if not isinstance(body, list):
        raise web.HTTPBadRequest(reason="Expected a JSON array")
    _write_spawn_templates(body[:12])
    return web.json_response({"status": "ok"})


@require_permission("view_instances")
@require_csrf
async def _handle_spawn_templates_delete(request: web.Request) -> web.Response:
    tpl_id = request.match_info["id"]
    templates = [t for t in _read_spawn_templates() if str(t.get("id")) != tpl_id]
    _write_spawn_templates(templates)
    return web.json_response({"status": "ok"})


async def _handle_hue(request: web.Request) -> web.Response:
    """Return the server hue epoch so the tray icon can phase-lock its cycle."""
    return web.json_response({"epoch_ms": _HUE_EPOCH_MS, "rate": 6})


async def _handle_favicon(request: web.Request) -> web.Response:
    """Serve logos.ico as /favicon.ico — public route so Edge --app shows the
    correct icon in the title bar and Windows taskbar without requiring auth."""
    import sys as _sys2
    import pathlib as _pl2
    candidates = []
    if getattr(_sys2, "frozen", False):
        candidates.append(_pl2.Path(_sys2._MEIPASS) / "launcher" / "logos.ico")
    candidates.append(_pl2.Path(__file__).parent.parent / "launcher" / "logos.ico")
    for p in candidates:
        if p.exists():
            data = p.read_bytes()
            return web.Response(
                body=data,
                content_type="image/x-icon",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    raise web.HTTPNotFound()


async def _handle_logo(request: web.Request) -> web.Response:
    """Serve the chat logo image from the baked-in app directory."""
    import pathlib
    logo = pathlib.Path("/app/chat_logo.png")
    if not logo.exists():
        raise web.HTTPNotFound()
    data = logo.read_bytes()
    return web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _handle_health(request: web.Request) -> web.Response:
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    uptime = int(time.time() - _start_time)
    from gateway.auth.db import is_setup_completed as _isc
    return web.json_response({
        "status": "ok",
        "product": "logos",
        "setup_completed": _isc(),
        "sessions": len(sessions),
        "uptime_s": uptime,
        "platform_stats": getattr(runner, "_platform_stats", {}),
    })


async def _handle_health_ready(request: web.Request) -> web.Response:
    """Deep readiness check: verifies auth DB and soul registry are operational.

    Returns 200 when ready, 503 when not. Used as the k8s readiness probe so
    traffic is only sent to pods that have fully initialised their subsystems.
    """
    checks: dict[str, str] = {}
    ok = True

    # Auth DB — a simple list_users call exercises the connection
    try:
        auth_db.list_users(limit=1)
        checks["auth_db"] = "ok"
    except Exception as exc:
        checks["auth_db"] = f"fail: {exc}"
        ok = False

    # Soul registry — must have loaded at least one soul
    souls = _souls_module._SOUL_REGISTRY
    if souls:
        checks["souls"] = f"ok ({len(souls)} loaded)"
    else:
        checks["souls"] = "empty"
        ok = False

    status = 200 if ok else 503
    return web.json_response(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status=status,
    )


_MODEL_CATALOG_PATH = Path(__file__).parent / "model_catalog.yaml"
_model_catalog_cache: list | None = None


async def _handle_model_catalog(request: web.Request) -> web.Response:
    """GET /api/model-catalog — return the Ollama model catalog.

    Loads from gateway/model_catalog.yaml on first call (cached).
    Falls back to an empty list if the file is missing.
    """
    global _model_catalog_cache
    if _model_catalog_cache is None:
        try:
            import yaml
            _model_catalog_cache = yaml.safe_load(
                _MODEL_CATALOG_PATH.read_text(encoding="utf-8")
            ) or []
        except Exception as exc:
            logger.warning("Failed to load model catalog: %s", exc)
            _model_catalog_cache = []
    return web.json_response(_model_catalog_cache)


async def _handle_sessions(request: web.Request) -> web.Response:
    if not _check_auth(request):
        raise web.HTTPUnauthorized()
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_api_platform_sessions(request: web.Request) -> web.Response:
    """GET /api/platform-sessions?platform=telegram — list server-side sessions by platform."""
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    platform_filter = request.rel_url.query.get("platform")
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    if platform_filter:
        sessions = [s for s in sessions if s.platform and s.platform.value == platform_filter]
    else:
        sessions = [s for s in sessions if s.platform and s.platform.value not in ("local",)]
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_api_session_messages(request: web.Request) -> web.Response:
    """GET /api/platform-sessions/{session_id}/messages — load transcript for a session."""
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    session_id = request.match_info["session_id"]
    runner: Any = request.app["runner"]
    messages = runner.session_store.load_transcript(session_id)
    filtered = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return web.json_response(filtered)


async def _handle_transcribe(request: web.Request) -> web.Response:
    """POST /chat/transcribe — accept a webm/wav/ogg audio blob, return transcript."""
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "audio":
            return web.json_response({"error": "missing audio field"}, status=400)
        audio_bytes = await field.read(decode=True)
    except Exception as e:
        return web.json_response({"error": f"read failed: {e}"}, status=400)

    if not audio_bytes:
        return web.json_response({"error": "empty audio"}, status=400)

    # Write to a temp file so transcribe_audio can read it
    import tempfile
    suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        from tools.transcription_tools import transcribe_audio
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, transcribe_audio, tmp_path),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = {"success": False, "error": "transcription timed out (30s)"}
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    if not result.get("success"):
        return web.json_response({"error": result.get("error", "transcription failed")}, status=500)

    return web.json_response({"transcript": result.get("transcript", "")})


# ── Action policy handlers ─────────────────────────────────────────────────

async def _handle_action_policies_list(request: web.Request) -> web.Response:
    rows = auth_db.list_action_policies()
    return web.json_response({"action_policies": rows})


async def _handle_action_policies_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    name = body.get("name", "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="name required")
    try:
        row = auth_db.create_action_policy(
            name=name,
            description=body.get("description", ""),
            network_policy=body.get("network_policy", "internet_enabled"),
            network_allowlist=body.get("network_allowlist", "[]")
                if isinstance(body.get("network_allowlist"), str)
                else json.dumps(body.get("network_allowlist", [])),
            filesystem_policy=body.get("filesystem_policy", "workspace_only"),
            exec_policy=body.get("exec_policy", "restricted"),
            write_policy=body.get("write_policy", "auto_apply"),
            provider_policy=body.get("provider_policy", "any"),
            secret_policy=body.get("secret_policy", "tool_only"),
        )
    except Exception as e:
        raise web.HTTPConflict(reason=str(e))
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "create_action_policy",
        target_type="action_policy", target_id=row["id"],
    )
    return web.json_response({"action_policy": row}, status=201)


async def _handle_action_policies_get(request: web.Request) -> web.Response:
    row = auth_db.get_action_policy(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    return web.json_response({"action_policy": row})


async def _handle_action_policies_patch(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    # Serialise allowlist if passed as list
    if "network_allowlist" in body and isinstance(body["network_allowlist"], list):
        body["network_allowlist"] = json.dumps(body["network_allowlist"])
    row = auth_db.update_action_policy(policy_id, **body)
    if not row:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "update_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"action_policy": row})


async def _handle_action_policies_delete(request: web.Request) -> web.Response:
    policy_id = request.match_info["id"]
    deleted = auth_db.delete_action_policy(policy_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Action policy not found")
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "delete_action_policy",
        target_type="action_policy", target_id=policy_id,
    )
    return web.json_response({"deleted": True})


async def _handle_user_action_policy_patch(request: web.Request) -> web.Response:
    user_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    policy_id = body.get("action_policy_id")  # None to clear
    auth_db.assign_user_action_policy(user_id, policy_id)
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "assign_action_policy",
        target_type="user", target_id=user_id,
        metadata={"action_policy_id": policy_id},
    )
    return web.json_response({"user_id": user_id, "action_policy_id": policy_id})


# ── Approval request handlers ──────────────────────────────────────────────

async def _handle_approvals_list(request: web.Request) -> web.Response:
    current_user = request.get("current_user") or {}
    role = current_user.get("role", "viewer")
    user_id = current_user.get("sub")
    # Non-admin/operator users can only see their own session's approvals
    session_id = request.rel_url.query.get("session_id")
    status_filter = request.rel_url.query.get("status")
    if role not in ("admin", "operator") and not session_id:
        # Safety: require session_id for non-privileged users
        raise web.HTTPForbidden(reason="session_id required for non-admin users")
    page = int(request.rel_url.query.get("page", 1))
    rows, total = auth_db.list_approval_requests(
        session_id=session_id, status=status_filter, page=page
    )
    return web.json_response({"approvals": rows, "total": total, "page": page})


async def _handle_approvals_get(request: web.Request) -> web.Response:
    row = auth_db.get_approval_request(request.match_info["id"])
    if not row:
        raise web.HTTPNotFound(reason="Approval request not found")
    return web.json_response({"approval": row})


async def _handle_approvals_approve(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "approved", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "approve_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )

    # ── MCP access grant hook ──────────────────────────────────────────────
    # If this approval was for an MCP server access request, grant the session
    # access and inject the server's tools so they appear on the next agent turn.
    try:
        from gateway.auth.policy import ACTION_MCP_ACCESS
        if updated.get("action_type") == ACTION_MCP_ACCESS:
            import json as _json
            meta = _json.loads(updated.get("tool_args") or "{}")
            _srv_name = meta.get("server_name")
            _sess_id  = updated.get("session_id")
            _mcp_svc  = request.app.get("mcp_service")
            if _srv_name and _sess_id and _mcp_svc:
                from gateway.mcp_access import grant_access as _grant
                from tools.mcp_tool import inject_mcp_server_for_session as _inject
                _grant(_sess_id, _srv_name)
                _url = _mcp_svc.get_server_url(_srv_name, "local")
                await asyncio.get_event_loop().run_in_executor(
                    None, _inject, _srv_name, _url
                )
                logger.info("mcp approval hook: granted session=%s server=%s", _sess_id, _srv_name)
    except Exception as _mcp_hook_err:
        logger.warning("mcp approval hook error: %s", _mcp_hook_err)

    return web.json_response({"approved": True, "approval_id": approval_id})


async def _handle_approvals_reject(request: web.Request) -> web.Response:
    approval_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = body.get("note", "")
    decided_by = (request.get("current_user") or {}).get("sub")
    updated = auth_db.resolve_approval_request(approval_id, "rejected", decided_by, note)
    if not updated:
        row = auth_db.get_approval_request(approval_id)
        if not row:
            raise web.HTTPNotFound(reason="Approval request not found")
        raise web.HTTPConflict(reason=f"Request is already {row['status']}")
    auth_db.write_audit_log(
        decided_by, "reject_tool_request",
        target_type="approval_request", target_id=approval_id,
        metadata={"note": note},
    )
    return web.json_response({"rejected": True, "approval_id": approval_id})


# ── Workflow handlers ──────────────────────────────────────────────────────

async def _handle_workflows_list(request: web.Request) -> web.Response:
    rows = auth_db.list_workflow_definitions()
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflows": [_WD.from_row(r).to_dict() for r in rows]})


async def _handle_workflows_post(request: web.Request) -> web.Response:
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    # Validate steps
    steps_raw = body.get("steps", [])
    if not isinstance(steps_raw, list):
        return web.json_response({"error": "steps must be an array"}, status=400)
    try:
        from workflows.model import StepDefinition as _SD
        _ = [_SD.from_dict(s) for s in steps_raw]
    except Exception as exc:
        return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)

    import json as _json
    row = auth_db.create_workflow_definition(
        name=name,
        steps_json=_json.dumps(steps_raw),
        description=body.get("description", ""),
        version=body.get("version", "1.0"),
        tags=_json.dumps(body.get("tags", [])),
        created_by=caller.get("sub"),
    )
    auth_db.write_audit_log(
        caller.get("sub"), "create_workflow",
        target_type="workflow", target_id=row["id"],
        metadata={"name": name},
    )
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()}, status=201)


async def _handle_workflows_get(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    row = auth_db.get_workflow_definition(wf_id)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_patch(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    import json as _json
    kwargs = {}
    if "name" in body:
        kwargs["name"] = body["name"]
    if "description" in body:
        kwargs["description"] = body["description"]
    if "version" in body:
        kwargs["version"] = body["version"]
    if "tags" in body:
        kwargs["tags"] = _json.dumps(body["tags"])
    if "steps" in body:
        try:
            from workflows.model import StepDefinition as _SD
            _ = [_SD.from_dict(s) for s in body["steps"]]
            kwargs["steps_json"] = _json.dumps(body["steps"])
        except Exception as exc:
            return web.json_response({"error": f"invalid step definition: {exc}"}, status=400)
    row = auth_db.update_workflow_definition(wf_id, **kwargs)
    if not row:
        raise web.HTTPNotFound(reason="Workflow not found")
    from workflows.model import WorkflowDefinition as _WD
    return web.json_response({"workflow": _WD.from_row(row).to_dict()})


async def _handle_workflows_delete(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    deleted = auth_db.delete_workflow_definition(wf_id)
    if not deleted:
        raise web.HTTPNotFound(reason="Workflow not found")
    auth_db.write_audit_log(
        caller.get("sub"), "delete_workflow",
        target_type="workflow", target_id=wf_id,
    )
    return web.json_response({"deleted": True})


async def _handle_workflow_trigger(request: web.Request) -> web.Response:
    wf_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    caller_id = caller.get("sub")
    try:
        body = await request.json()
    except Exception:
        body = {}
    inputs = body.get("inputs") or {}

    # Resolve caller's action policy for the run.
    _action_policy = None
    if caller_id and caller_id.startswith("usr_"):
        try:
            from gateway.auth.policy import ActionPolicy as _AP
            _pr = auth_db.get_user_action_policy_row(caller_id)
            _action_policy = _AP.from_row(_pr) if _pr else None
        except Exception:
            pass

    engine = request.app.get("workflow_engine")
    if not engine:
        return web.json_response({"error": "workflow engine not available"}, status=503)
    try:
        run_id = await engine.start_run(
            workflow_id=wf_id,
            triggered_by=caller_id,
            inputs=inputs,
            action_policy=_action_policy,
            auth_user_id=caller_id,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except Exception as exc:
        logger.exception("Failed to start workflow run")
        return web.json_response({"error": str(exc)}, status=500)

    auth_db.write_audit_log(
        caller_id, "trigger_workflow",
        target_type="workflow_run", target_id=run_id,
        metadata={"workflow_id": wf_id, "inputs": inputs},
    )
    return web.json_response({"run_id": run_id, "workflow_id": wf_id}, status=202)


async def _handle_workflow_runs_list(request: web.Request) -> web.Response:
    wf_id  = request.rel_url.query.get("workflow_id")
    status = request.rel_url.query.get("status")
    limit  = min(int(request.rel_url.query.get("limit", 50)), 200)
    offset = int(request.rel_url.query.get("offset", 0))
    runs, total = auth_db.list_workflow_runs(workflow_id=wf_id, status=status,
                                              limit=limit, offset=offset)
    return web.json_response({"runs": runs, "total": total})


async def _handle_workflow_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    steps = auth_db.get_workflow_step_runs(run_id)
    return web.json_response({"run": run, "steps": steps})


async def _handle_workflow_run_cancel(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    caller = request.get("current_user") or {}
    run = auth_db.get_workflow_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="Workflow run not found")
    if run["status"] in ("success", "failed", "cancelled"):
        return web.json_response({"error": "run already terminal"}, status=409)
    engine = request.app.get("workflow_engine")
    if engine:
        await engine.cancel_run(run_id)
    else:
        auth_db.update_workflow_run(run_id, status="cancelled",
                                    finished_at=int(time.time() * 1000))
    auth_db.write_audit_log(
        caller.get("sub"), "cancel_workflow_run",
        target_type="workflow_run", target_id=run_id,
    )
    return web.json_response({"cancelled": True, "run_id": run_id})


async def _handle_workflow_approval_decide(request: web.Request) -> web.Response:
    """Approve or reject a workflow approval step via its approval_request id."""
    approval_id = request.match_info["id"]
    decision    = request.match_info["decision"]   # 'approve' | 'reject'
    if decision not in ("approve", "reject"):
        return web.json_response({"error": "decision must be 'approve' or 'reject'"}, status=400)
    caller = request.get("current_user") or {}
    decided_by = caller.get("sub")

    engine = request.app.get("workflow_engine")
    if engine:
        await engine.resume_approval(
            approval_id=approval_id,
            approved=(decision == "approve"),
            decided_by=decided_by,
        )
    else:
        # Engine not running (e.g. tests) — just update the DB record.
        status = "approved" if decision == "approve" else "rejected"
        auth_db.resolve_approval_request(approval_id, status=status, decided_by=decided_by)
    return web.json_response({"decided": True, "decision": decision, "approval_id": approval_id})


async def _handle_chat(request: web.Request) -> web.StreamResponse:
    # /chat is intentionally unauthenticated (same-origin dashboard, LAN-only NodePort).
    # Rate limiting prevents runaway agent spawning from a single IP.
    ip = request.remote or "unknown"
    if not check_rate_limit(ip, max_requests=30, window=60):
        raise web.HTTPTooManyRequests(
            text='{"error":"rate_limited"}',
            content_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    message = body.get("message", "")
    session_id = body.get("session_id", "http-default")

    # --- Process file attachments ---
    raw_attachments = body.get("attachments") or []
    # Legacy single-image support
    legacy_image = body.get("image")
    if legacy_image and not raw_attachments:
        raw_attachments = [{"data": legacy_image, "name": "image.png", "type": "image/png"}]

    media_urls: list[str] = []
    media_types: list[str] = []
    if raw_attachments:
        import base64
        from gateway.platforms.base import (
            cache_image_from_bytes,
            cache_audio_from_bytes,
            cache_document_from_bytes,
        )
        _ATTACH_MAX = 5
        _ATTACH_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
        for att in raw_attachments[:_ATTACH_MAX]:
            data_url = att.get("data", "")
            att_name = att.get("name", "file")
            att_type = att.get("type", "application/octet-stream")
            # Decode base64 data URL: "data:<mime>;base64,<payload>"
            if ";base64," not in data_url:
                continue
            payload = data_url.split(";base64,", 1)[1]
            try:
                raw_bytes = base64.b64decode(payload)
            except Exception:
                continue
            if len(raw_bytes) > _ATTACH_MAX_SIZE:
                continue
            # Cache to disk using existing helpers
            ext = os.path.splitext(att_name)[1] or ".bin"
            if att_type.startswith("image/"):
                cached = cache_image_from_bytes(raw_bytes, ext)
            elif att_type.startswith("audio/"):
                cached = cache_audio_from_bytes(raw_bytes, ext)
            else:
                cached = cache_document_from_bytes(raw_bytes, att_name)
            media_urls.append(cached)
            media_types.append(att_type)

    # Use authenticated identity; fall back to body fields for backwards-compat
    auth_user = request.get("current_user") or {}
    user_name = (
        auth_db.get_user_by_id(auth_user.get("sub", ""))or {}
    ).get("display_name") or auth_user.get("email") or body.get("user_name", "User")
    user_id = auth_user.get("sub") or body.get("user_id", "http-user")

    if not message:
        raise web.HTTPBadRequest(reason="message is required")

    runner: Any = request.app["runner"]

    # Resolve the authenticated user's action policy (if any).
    # Applies only to auth-db users (usr_... IDs); platform/anonymous users get DEFAULT_POLICY.
    _action_policy = None
    _auth_user_id = None
    _real_user_id = auth_user.get("sub", "")
    if _real_user_id and _real_user_id.startswith("usr_"):
        _auth_user_id = _real_user_id
        try:
            from gateway.auth.policy import ActionPolicy as _AP, merge_policies as _merge
            _policy_row = auth_db.get_user_action_policy_row(_real_user_id)
            _action_policy = _AP.from_row(_policy_row) if _policy_row else None
            # Session-level tightening: caller may request a stricter policy for this request only.
            # Requires manage_action_policies permission (admins/operators creating sandboxed sessions).
            _session_policy_id = body.get("action_policy_id")
            from gateway.auth.rbac import has_permission as _has_perm
            if _session_policy_id and _has_perm(auth_user.get("role", "viewer"), "manage_action_policies"):
                _sess_row = auth_db.get_action_policy(_session_policy_id)
                if _sess_row:
                    _action_policy = _merge(_action_policy, _AP.from_row(_sess_row))
        except Exception as _pe:
            logger.warning("Failed to resolve action policy for %s: %s", _real_user_id, _pe)

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id=session_id,
        chat_type="dm",
        user_id=user_id,
        user_name=user_name,
    )

    session_entry = runner.session_store.get_or_create_session(source)
    session_key = session_entry.session_key
    history = runner.session_store.load_transcript(session_entry.session_id)
    context = build_session_context(source, runner.config, session_entry)
    context_prompt = build_session_context_prompt(context)

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await resp.prepare(request)

    async def send_event(data: dict) -> None:
        try:
            await resp.write(f"data: {json.dumps(data)}\n\n".encode())
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # Client disconnected mid-stream — nothing we can do

    async def heartbeat_loop() -> None:
        """Send SSE comments every 20s to keep the connection alive through proxies."""
        while True:
            await asyncio.sleep(20)
            try:
                await resp.write(b": heartbeat\n\n")
            except Exception:
                break

    await send_event({"type": "start"})

    # Enrich message with attachment analysis (vision, transcription, doc context)
    if media_urls:
        message = await runner._enrich_message_with_attachments(
            message, media_urls, media_types,
        )

    heartbeat = asyncio.ensure_future(heartbeat_loop())
    result = {}
    t_agent_start = time.time()
    try:
        result = await runner._run_agent(
            message=message,
            context_prompt=context_prompt,
            history=history,
            source=source,
            session_id=session_entry.session_id,
            session_key=session_key,
            action_policy=_action_policy,
            auth_user_id=_auth_user_id,
        )
        final = result.get("final_response", "")
        await send_event({"type": "message", "content": final})
    except Exception as exc:
        # Distinguish real tool/agent errors from transport failures so the UI
        # can show a more informative message than "network error".
        logger.exception("Error running agent for HTTP /chat")
        err_str = str(exc)
        # Surface as a typed error so the frontend can decide how to display it
        await send_event({"type": "error", "content": err_str, "error_class": type(exc).__name__})
    finally:
        heartbeat.cancel()

    await send_event({
        "type":            "done",
        "elapsed_s":       round(time.time() - t_agent_start, 1),
        "prompt_tokens":   result.get("last_prompt_tokens", 0),
        "api_calls":       result.get("api_calls", 0),
        "tools_used":      result.get("tools_used", 0),
        "tools_available": len(result.get("tools", [])),
        "model":           result.get("model", ""),
        "tool_detail":     result.get("tool_detail", []),
    })
    return resp


# ── Agent Runs handlers ──────────────────────────────────────────────────────

async def _handle_runs_list(request: web.Request) -> web.Response:
    user = request.get("current_user") or {}
    role = user.get("role", "viewer")
    uid = user.get("sub", "")
    # Operators/admins see all runs; users see only their own
    from gateway.auth.rbac import has_permission
    see_all = has_permission(role, "manage_users")
    params = request.rel_url.query
    status_f = params.get("status") or None
    session_f = params.get("session_id") or None
    limit = min(int(params.get("limit", 50)), 200)
    offset = int(params.get("offset", 0))
    runs, total = auth_db.list_agent_runs(
        user_id=None if see_all else uid,
        status=status_f,
        session_id=session_f,
        limit=limit,
        offset=offset,
    )
    # Parse JSON fields and resolve user_id → username
    user_ids = {r["user_id"] for r in runs if r.get("user_id")}
    user_map = {}
    for uid in user_ids:
        u = auth_db.get_user_by_id(uid)
        if u:
            user_map[uid] = u.get("username") or u.get("email") or uid
    for r in runs:
        for field in ("tool_sequence", "tool_detail", "approval_ids"):
            try:
                r[field] = json.loads(r[field] or "[]")
            except Exception:
                r[field] = []
        if r.get("user_id"):
            r["username"] = user_map.get(r["user_id"], r["user_id"])
    return web.json_response({"runs": runs, "total": total})


async def _handle_run_get(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    for field in ("tool_sequence", "tool_detail", "approval_ids"):
        try:
            run[field] = json.loads(run[field] or "[]")
        except Exception:
            run[field] = []
    return web.json_response({"run": run})


async def _handle_run_clone(request: web.Request) -> web.Response:
    """Clone a run — return a prefilled payload the UI can use to start a new chat."""
    run_id = request.match_info["id"]
    run = auth_db.get_agent_run(run_id)
    if not run:
        raise web.HTTPNotFound(reason="run_not_found")
    tool_seq = []
    try:
        tool_seq = json.loads(run.get("tool_sequence") or "[]")
    except Exception:
        pass
    destructive_tools = {"write_file", "patch", "terminal", "execute_code", "delete_file"}
    had_destructive = bool(set(tool_seq) & destructive_tools)
    return web.json_response({
        "clone": {
            "user_message": run.get("user_message", ""),
            "session_id": run.get("session_id", ""),
            "model": run.get("model", ""),
            "original_run_id": run_id,
            "had_destructive_tools": had_destructive,
            "warning": (
                "This run used destructive tools. Review carefully before running."
                if had_destructive else None
            ),
        }
    })


async def start_http_api(runner: Any, port: int = 8080) -> None:
    """Start the aiohttp server. Call as an asyncio task."""
    global _start_time
    _start_time = time.time()

    # Initialise auth DB alongside existing Logos state
    global _hermes_home
    hermes_home = Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(Path.home() / ".logos"))
    _hermes_home = hermes_home
    auth_db.init_db(hermes_home)

    # Ensure a stable JWT secret exists for local installs.
    # K8s sets HERMES_JWT_SECRET via a k8s Secret; local desktop/CLI installs
    # never set it.  Generate once, persist to ~/.logos/.jwt_secret so tokens
    # survive gateway restarts without forcing re-login every time.
    # Also treat the known template placeholder as unset — k8s/02-secret.yaml ships
    # with REPLACE_WITH_JWT_SECRET so a fresh cluster with un-edited secrets gets a
    # real random value rather than the publicly-known placeholder.
    _KNOWN_JWT_PLACEHOLDERS = {"", "REPLACE_WITH_JWT_SECRET", "replace_with_jwt_secret"}
    if os.environ.get("HERMES_JWT_SECRET", "") in _KNOWN_JWT_PLACEHOLDERS:
        import secrets as _secrets
        _jwt_secret_path = hermes_home / ".jwt_secret"
        if _jwt_secret_path.exists():
            os.environ["HERMES_JWT_SECRET"] = _jwt_secret_path.read_text().strip()
        else:
            _jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
            _new_secret = _secrets.token_hex(32)
            _jwt_secret_path.write_text(_new_secret)
            _jwt_secret_path.chmod(0o600)
            os.environ["HERMES_JWT_SECRET"] = _new_secret
            logger.info("Generated new JWT secret at %s", _jwt_secret_path)
    # HERMES_WIPE_ON_START: wipe setup state so /setup always runs fresh (setup-test deployments)
    if os.environ.get("HERMES_WIPE_ON_START", "").lower() in ("1", "true", "yes"):
        try:
            auth_db.reset_setup_completed()
            for _m in auth_db.list_machines():
                auth_db.delete_machine(_m["id"])
            for _p in auth_db.list_policies():
                auth_db.delete_policy(_p["id"])
            logger.info("HERMES_WIPE_ON_START: wiped setup state, machines, and policies")
        except Exception as _wipe_err:
            logger.warning("HERMES_WIPE_ON_START: partial failure: %s", _wipe_err)
    # Env-var admin seeding (takes priority over generic seed)
    _ensure_admin_exists()
    # Generic seed: machines → profiles → admin user (all no-ops on existing data)
    from gateway import seed as _seed
    _seed.run_seed()

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-CSRF-Token",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app["runner"] = runner

    # Executor — selects kubernetes or local-process backend based on runtime mode
    from gateway.executors import build_executor
    app["executor"] = build_executor(_RUNTIME_MODE)
    logger.info("Instance executor: %s (runtime_mode=%s)", type(app["executor"]).__name__, _RUNTIME_MODE)

    # Worker registry — tracks connected agent workers via WebSocket
    from gateway.worker_registry import WorkerRegistry
    worker_registry = WorkerRegistry()
    app["worker_registry"] = worker_registry

    # ── Centralized MCP gateway service ────────────────────────────────────
    # Boots all configured MCP servers once and exposes them over HTTP so
    # agents in any executor mode (local, OpenShell, k8s) can connect via URL.
    try:
        from gateway.mcp_service import MCPGatewayService, load_mcp_gateway_config
        import gateway as _gw_module
        _mcp_cfg = load_mcp_gateway_config()
        _mcp_svc = MCPGatewayService(_mcp_cfg)
        app["mcp_service"] = _mcp_svc
        # Expose via module-level ref so mcp_access_tool.py can reach it
        _gw_module._mcp_service_ref = _mcp_svc
        import os as _os
        _os.environ["HERMES_GATEWAY_MCP"] = "1"
        asyncio.ensure_future(_mcp_svc.start())
        logger.info("MCP gateway service initialised (%d server(s) configured)",
                    len(_mcp_cfg.get("mcp_servers") or {}))
    except Exception as _mcp_err:
        logger.warning("MCP gateway service failed to initialise: %s", _mcp_err)
        app["mcp_service"] = None

    # ── Inject tool credentials from DB into os.environ ────────────────
    try:
        from gateway.services import inject_credentials
        _n_creds = inject_credentials()
        if _n_creds:
            logger.info("Injected %d tool credential(s) from DB", _n_creds)
    except Exception as _cred_err:
        logger.debug("Could not inject credentials: %s", _cred_err)

    # Workflow engine — lazily imported to avoid circular deps at module load.
    try:
        from workflows.engine import WorkflowEngine as _WFEngine
        app["workflow_engine"] = _WFEngine(runner)
        logger.info("Workflow engine initialised")
    except Exception as _wf_err:
        logger.warning("Workflow engine failed to initialise: %s", _wf_err)
        app["workflow_engine"] = None

    _load_souls()

    # ── Public routes ──────────────────────────────────────────────────────
    app.router.add_get("/health",        _handle_health)
    app.router.add_get("/healthz",       _handle_health)       # K8s liveness probe alias
    app.router.add_get("/health/ready",  _handle_health_ready)
    app.router.add_get("/favicon.ico",   _handle_favicon)      # public — Edge --app needs this before auth
    app.router.add_get("/api/hue",       _handle_hue)          # public — tray icon phase-lock
    app.router.add_get("/chat_logo.png", _handle_logo)
    app.router.add_get("/login",         _handle_login_page)
    app.router.add_get("/api/model-catalog", _handle_model_catalog)

    # ── Auth routes (no cookie required) ───────────────────────────────────
    app.router.add_post("/auth/login",   handle_login)
    app.router.add_post("/auth/logout",  handle_logout)
    app.router.add_post("/auth/refresh", handle_refresh)

    # ── Authenticated routes ───────────────────────────────────────────────
    from gateway import setup_handlers as _sh
    # ── MCP gateway routes ─────────────────────────────────────────────────
    from gateway import mcp_handlers as _mch
    app.router.add_get("/api/mcp/catalogue",                    _mch.handle_catalogue)
    app.router.add_get("/api/mcp/status",                       _mch.handle_mcp_status)
    app.router.add_post("/api/mcp/grants/{session_id}/{server}", _mch.handle_grant)
    app.router.add_delete("/api/mcp/grants/{session_id}/{server}", _mch.handle_revoke)
    # StreamableHTTP proxy — catch-all for /mcp/{name} and /mcp/{name}/...
    app.router.add_route("*", r"/mcp/{server_name}",           _mch.handle_mcp_proxy)
    app.router.add_route("*", r"/mcp/{server_name}/{tail:.*}", _mch.handle_mcp_proxy)

    # ── Tools tab — MCP server management ────────────────────────────
    from gateway import mcp_management as _mcm
    _mcm.register_routes(app)

    # ── Unified Services (tool credentials + MCP catalogue) ────────────
    app.router.add_get("/api/services",       _handle_services_catalogue)
    app.router.add_post("/api/services/keys", _handle_services_set_key)
    app.router.add_delete("/api/services/keys", _handle_services_delete_key)
    app.router.add_post("/api/services/validate", _handle_services_validate_key)
    app.router.add_post("/api/services/inference", _handle_services_inference)

    app.router.add_get("/setup",              _handle_setup_page)
    app.router.add_get("/api/setup/probe",    _sh.handle_setup_probe)
    app.router.add_get("/api/setup/scan",     _sh.handle_setup_scan)
    app.router.add_get("/api/setup/status",   _handle_setup_status)
    app.router.add_post("/api/setup/pull",    _sh.handle_setup_pull)
    app.router.add_post("/api/setup/compare", _sh.handle_setup_compare)
    app.router.add_post("/api/setup/compare/cancel-server", _sh.handle_setup_compare_cancel_server)
    app.router.add_post("/api/setup/test-k8s", _sh.handle_setup_test_k8s)
    app.router.add_post("/api/setup/test",    _sh.handle_setup_test)
    app.router.add_post("/api/setup/complete",    _sh.handle_setup_complete)
    app.router.add_get("/api/setup/discover",       _sh.handle_setup_discover)
    app.router.add_post("/api/setup/set-remote",   _sh.handle_setup_set_remote)
    app.router.add_get("/api/setup/env-probe",       _sh.handle_setup_env_probe)
    app.router.add_post("/api/setup/sandbox-setup",  _sh.handle_setup_sandbox_setup)
    app.router.add_post("/api/setup/k3s-install",    _sh.handle_setup_k3s_install)
    app.router.add_post("/api/setup/launch-docker",  _sh.handle_setup_launch_docker)
    app.router.add_post("/api/setup/reset",
        require_csrf(require_permission("manage_platform")(_handle_setup_reset)))

    app.router.add_get("/",              _handle_index)
    app.router.add_get("/auth/me",       handle_me)
    app.router.add_get("/users/me",      handle_me)
    app.router.add_patch("/users/me",    handle_users_me_patch)
    app.router.add_get(
        "/users",
        require_permission("manage_users")(handle_users_list),
    )
    app.router.add_post(
        "/users",
        require_permission("manage_users")(require_csrf(handle_users_post)),
    )
    app.router.add_patch(
        "/users/{id}",
        require_permission("manage_users")(require_csrf(handle_users_patch)),
    )
    app.router.add_delete(
        "/users/{id}",
        require_permission("manage_users")(require_csrf(admin_handlers.handle_users_delete)),
    )
    app.router.add_post(
        "/users/{id}/reset",
        require_permission("manage_users")(require_csrf(admin_handlers.handle_users_reset)),
    )
    app.router.add_get(
        "/audit-logs",
        require_permission("view_audit_logs")(handle_audit_logs),
    )
    app.router.add_get(
        "/api/logs",
        require_permission("view_audit_logs")(_handle_log_tail),
    )
    app.router.add_get("/souls",         _handle_souls_get)
    app.router.add_get("/souls/{slug}",  _handle_soul_detail)
    app.router.add_get(
        "/instances",
        require_permission("view_instances")(_handle_instances_get),
    )
    app.router.add_post("/instances",    _handle_instances_post)
    app.router.add_delete("/instances/{name}", _handle_instances_delete)
    # Instance management (memory, knowledge, config)
    app.router.add_get("/instances/{name}/memory",              _handle_instance_memory_get)
    app.router.add_put("/instances/{name}/memory",              _handle_instance_memory_put)
    app.router.add_get("/instances/{name}/knowledge",           _handle_instance_knowledge_get)
    app.router.add_post("/instances/{name}/knowledge/ingest",   _handle_instance_knowledge_ingest)
    app.router.add_delete("/instances/{name}/knowledge/{source}", _handle_instance_knowledge_delete)
    app.router.add_get("/instances/{name}/knowledge/search",    _handle_instance_knowledge_search)
    app.router.add_post("/instances/{name}/fork",               _handle_instance_fork)
    # Worker WebSocket + REST
    app.router.add_get("/ws/worker", worker_registry.handle_ws)
    app.router.add_get("/api/workers", lambda r: web.json_response(
        {"workers": r.app["worker_registry"].list_workers()}
    ))
    app.router.add_get("/spawn-templates",         _handle_spawn_templates_get)
    app.router.add_put("/spawn-templates",         _handle_spawn_templates_put)
    app.router.add_delete("/spawn-templates/{id}", _handle_spawn_templates_delete)
    app.router.add_get("/status",        _handle_status)
    app.router.add_get("/toolsets",      _handle_toolsets)
    app.router.add_post("/api/toolsets/toggle", _handle_toolsets_toggle)
    app.router.add_get("/sessions",      _handle_sessions)
    app.router.add_get("/api/platform-sessions", _handle_api_platform_sessions)
    app.router.add_get("/api/platform-sessions/{session_id}/messages", _handle_api_session_messages)
    app.router.add_post("/chat",               _handle_chat)
    app.router.add_post("/chat/transcribe",    require_csrf(_handle_transcribe))
    app.router.add_route("OPTIONS", "/chat",   _handle_index)
    app.router.add_get("/canary/status", _handle_canary_status)
    app.router.add_get("/proxy/state",        _handle_proxy_state)
    app.router.add_post("/proxy/providers/{key}/toggle", _handle_proxy_toggle)
    app.router.add_get("/proxy/models-live",  _handle_proxy_models_live)
    app.router.add_post("/proxy/benchmark",   _handle_proxy_benchmark)
    app.router.add_get("/internal/routing/claims",  _handle_routing_claims)
    app.router.add_post("/internal/routing/apply",  require_csrf(_handle_routing_apply))
    app.router.add_patch("/api/model", require_csrf(_handle_model_patch))

    # ── Admin routes ───────────────────────────────────────────────────────
    _mm  = require_permission("manage_machines")
    _mp  = require_permission("claim_machine")
    _mpr = require_permission("manage_profiles")
    _mu  = require_permission("manage_users")
    _ap  = require_permission("assign_profile")
    _vrd = require_permission("view_routing_debug")

    app.router.add_get("/admin/model-classes", _mm(admin_handlers.handle_model_classes))
    app.router.add_get("/admin/machines",      _mm(admin_handlers.handle_machines_list))
    app.router.add_post("/admin/machines",     _mm(require_csrf(admin_handlers.handle_machines_post)))
    app.router.add_patch("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_patch)))
    app.router.add_delete("/admin/machines/{id}", _mm(require_csrf(admin_handlers.handle_machines_delete)))
    app.router.add_post("/admin/machines/reorder", _mm(require_csrf(admin_handlers.handle_machines_reorder)))
    app.router.add_get("/admin/machines/{id}/claims",  _mm(admin_handlers.handle_machine_claims_get))
    app.router.add_put("/machines/{id}/claim",         _mp(require_csrf(admin_handlers.handle_machine_claim_put)))
    app.router.add_delete("/machines/{id}/claim",      _mp(require_csrf(admin_handlers.handle_machine_claim_delete)))
    app.router.add_put("/admin/machines/{id}/capabilities", _mm(require_csrf(admin_handlers.handle_machine_capabilities_put)))
    app.router.add_get("/admin/machines/{id}/health", _mm(admin_handlers.handle_machine_health))
    app.router.add_get("/admin/policies",      _mpr(admin_handlers.handle_policies_list))
    app.router.add_post("/admin/policies",     _mpr(require_csrf(admin_handlers.handle_policies_post)))
    app.router.add_patch("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_patch)))
    app.router.add_delete("/admin/policies/{id}", _mpr(require_csrf(admin_handlers.handle_policies_delete)))
    app.router.add_put("/admin/policies/{id}/rules", _mpr(require_csrf(admin_handlers.handle_policy_rules_put)))
    app.router.add_patch("/admin/users/{id}/policy", _ap(require_csrf(admin_handlers.handle_user_policy_patch)))

    # ── Action policies (behaviour enforcement) ────────────────────────────
    _map = require_permission("manage_action_policies")
    _aap = require_permission("assign_action_policy")
    _vap = require_permission("view_approvals")
    _dap = require_permission("decide_approvals")

    app.router.add_get("/action-policies",         _map(_handle_action_policies_list))
    app.router.add_post("/action-policies",        _map(require_csrf(_handle_action_policies_post)))
    app.router.add_get("/action-policies/{id}",    _map(_handle_action_policies_get))
    app.router.add_patch("/action-policies/{id}",  _map(require_csrf(_handle_action_policies_patch)))
    app.router.add_delete("/action-policies/{id}", _map(require_csrf(_handle_action_policies_delete)))
    app.router.add_patch("/users/{id}/action-policy", _aap(require_csrf(_handle_user_action_policy_patch)))

    # ── Approval requests ──────────────────────────────────────────────────
    app.router.add_get("/approvals",              _vap(_handle_approvals_list))
    app.router.add_get("/approvals/{id}",         _vap(_handle_approvals_get))
    app.router.add_post("/approvals/{id}/approve", _dap(require_csrf(_handle_approvals_approve)))
    app.router.add_post("/approvals/{id}/reject",  _dap(require_csrf(_handle_approvals_reject)))

    # ── Workflow execution layer ────────────────────────────────────────────
    _mwf = require_permission("manage_workflows")
    _twf = require_permission("trigger_workflow")
    _vwf = require_permission("view_workflows")
    _dwf = require_permission("decide_workflow_approvals")

    app.router.add_get("/workflows",               _vwf(_handle_workflows_list))
    app.router.add_post("/workflows",              _mwf(require_csrf(_handle_workflows_post)))
    app.router.add_get("/workflows/{id}",          _vwf(_handle_workflows_get))
    app.router.add_patch("/workflows/{id}",        _mwf(require_csrf(_handle_workflows_patch)))
    app.router.add_delete("/workflows/{id}",       _mwf(require_csrf(_handle_workflows_delete)))
    app.router.add_post("/workflows/{id}/trigger", _twf(require_csrf(_handle_workflow_trigger)))
    app.router.add_get("/workflow-runs",           _vwf(_handle_workflow_runs_list))
    app.router.add_get("/workflow-runs/{id}",      _vwf(_handle_workflow_run_get))
    app.router.add_post("/workflow-runs/{id}/cancel", _twf(require_csrf(_handle_workflow_run_cancel)))
    app.router.add_post("/workflow-runs/approvals/{id}/{decision}", _dwf(require_csrf(_handle_workflow_approval_decide)))

    # ── Agent run records ───────────────────────────────────────────────────
    _vrun = require_permission("view_runs")
    app.router.add_get("/runs",            _vrun(_handle_runs_list))
    app.router.add_get("/runs/{id}",       _vrun(_handle_run_get))
    app.router.add_get("/runs/{id}/clone", _vrun(_handle_run_clone))

    # ── Evolution ───────────────────────────────────────────────────────────
    from gateway import evolution_handlers as _eh
    _vev  = require_permission("view_evolution")
    _mev  = require_permission("manage_evolution")
    _dev  = require_permission("decide_evolution")
    app.router.add_get("/evolution/proposals",           _vev(_eh.handle_list_proposals))
    app.router.add_get("/evolution/proposals/{id}",      _vev(_eh.handle_get_proposal))
    app.router.add_post("/evolution/proposals",          _mev(require_csrf(_eh.handle_create_proposal)))
    app.router.add_post("/evolution/proposals/{id}/decide", _dev(require_csrf(_eh.handle_decide_proposal)))
    app.router.add_post("/evolution/proposals/{id}/answer", _mev(require_csrf(_eh.handle_answer_question)))
    app.router.add_post("/evolution/proposals/{id}/consult", _dev(require_csrf(_eh.handle_consult_frontier)))
    app.router.add_post("/evolution/proposals/{id}/apply",   _dev(require_csrf(_eh.handle_apply_proposal)))
    app.router.add_get("/evolution/settings",            _vev(_eh.handle_get_settings))
    app.router.add_patch("/evolution/settings",          _mev(require_csrf(_eh.handle_update_settings)))

    app.router.add_get("/admin/routing/resolve",  _vrd(admin_handlers.handle_routing_resolve))
    app.router.add_get("/admin/routing/log",      require_permission("view_audit_logs")(admin_handlers.handle_routing_log))
    app.router.add_post("/admin/setup",           _mm(require_csrf(admin_handlers.handle_setup_wizard)))
    app.router.add_get("/routing/preview",        admin_handlers.handle_routing_preview)

    # ── Update status/trigger (launcher file-based IPC) ─────────────────
    import json as _json
    import pathlib as _up_pathlib

    _HERMES_HOME_UPD = _up_pathlib.Path(os.environ.get("LOGOS_HOME") or os.environ.get("HERMES_HOME") or str(_up_pathlib.Path.home() / ".logos"))
    _UPDATE_STATUS_FILE  = _HERMES_HOME_UPD / "update_status.json"
    _UPDATE_TRIGGER_FILE = _HERMES_HOME_UPD / "update_trigger.json"

    async def _handle_update_status(request: web.Request) -> web.Response:
        try:
            if _UPDATE_STATUS_FILE.exists():
                data = _json.loads(_UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
                return web.json_response(data)
        except Exception:
            pass
        return web.json_response({"available": "", "downloading": False, "ready": False, "ready_path": None})

    async def _handle_update_trigger(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            action = body.get("action")
            if action not in ("download", "install"):
                return web.json_response({"error": "invalid action"}, status=400)
            _UPDATE_TRIGGER_FILE.write_text(_json.dumps({"action": action}), encoding="utf-8")
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    _mm_upd = require_permission("manage_machines")
    app.router.add_get("/update-status",   _mm_upd(_handle_update_status))
    app.router.add_post("/update-trigger", _mm_upd(require_csrf(_handle_update_trigger)))

    # Serve static assets (logo, etc.)
    import pathlib as _pathlib
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: __file__ doesn't resolve relative to source tree
        _static_dir = _pathlib.Path(_sys._MEIPASS) / "assets"
    else:
        _static_dir = _pathlib.Path(__file__).parent.parent / "assets"
    if _static_dir.exists():
        app.router.add_static("/static", str(_static_dir), show_index=False)
    # Agent World JS modules
    _world_dir = _pathlib.Path(__file__).parent / "world"
    if _world_dir.exists():
        app.router.add_static("/world", str(_world_dir), show_index=False)

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

    async def _queue_retry_loop():
        """Retry queued instance requests when resources free up."""
        _executor = app["executor"]
        while True:
            await asyncio.sleep(60)
            if not _instance_queue:
                continue
            try:
                loop = asyncio.get_event_loop()
                headroom = await loop.run_in_executor(None, _executor.get_headroom)
                if headroom.can_spawn:
                    req = _instance_queue.pop(0)
                    logger.info("Retrying queued instance for %s", req["requester"])
                    from gateway.executors.base import InstanceConfig as _IC
                    _qlabel = req.get("instance_label") or req.get("soul_slug", "general")
                    await loop.run_in_executor(
                        None, _executor.spawn,
                        _IC(
                            name=_safe_k8s_name(req["requester"], _qlabel),
                            soul_name=req.get("soul_slug", "general"),
                            requester=req["requester"],
                            instance_label=_qlabel,
                        ),
                    )
            except Exception as e:
                logger.warning("Queue retry failed: %s", e)

    asyncio.create_task(_queue_retry_loop())

    # ── Workspace TTL cleanup ───────────────────────────────────────────────
    # Run once at startup to remove any workspaces left over from a previous
    # pod lifecycle, then schedule periodic sweeps.
    _ws_cleanup_interval_hours = float(
        os.environ.get("HERMES_WORKSPACE_CLEANUP_INTERVAL_HOURS", "1")
    )

    async def _workspace_cleanup_loop():
        """Delete ephemeral workspace directories whose TTL has expired."""
        # Startup sweep — workspaces from crashed/restarted pods accumulate
        try:
            from gateway import workspace as _ws_mod
            loop = asyncio.get_event_loop()
            removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
            if removed:
                logger.info("Startup workspace cleanup: removed %d expired workspaces", removed)
            else:
                logger.debug("Startup workspace cleanup: no expired workspaces found")
        except Exception as _wse:
            logger.warning("Startup workspace cleanup failed: %s", _wse)

        # Periodic sweeps
        while True:
            await asyncio.sleep(_ws_cleanup_interval_hours * 3600)
            try:
                from gateway import workspace as _ws_mod
                loop = asyncio.get_event_loop()
                removed = await loop.run_in_executor(None, _ws_mod.cleanup_expired)
                if removed:
                    logger.info(
                        "Periodic workspace cleanup: removed %d expired workspaces", removed
                    )
            except Exception as _wse:
                logger.warning("Periodic workspace cleanup error: %s", _wse)

    asyncio.create_task(_workspace_cleanup_loop())
