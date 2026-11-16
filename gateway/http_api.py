"""
HTTP API server for the Hermes gateway.

Provides:
  GET  /          — unified admin dashboard (no auth)
  GET  /health    — health check (no auth)
  GET  /status    — agent execution status JSON (no auth)
  GET  /sessions  — list active sessions (Bearer auth)
  POST /chat      — send a message, SSE stream (no auth — same-origin dashboard)
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

from aiohttp import web

from gateway.auth import db as auth_db
from gateway.auth.handlers import (
    handle_audit_logs,
    handle_login,
    handle_logout,
    handle_me,
    handle_refresh,
    handle_platform_settings_get,
    handle_platform_settings_patch,
    handle_register,
    handle_registration_status,
    handle_users_list,
    handle_users_me_patch,
    handle_users_patch,
    handle_users_post,
)
from gateway import admin_handlers
from gateway.auth.middleware import auth_middleware, check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password
from gateway.auth.rbac import can_spawn

logger = logging.getLogger(__name__)

_start_time: float = 0.0
_hermes_home: Path = Path(
    os.environ.get("LOGOS_HOME")
    or str(Path.home() / ".logos")
)
_INSTANCE_NAME = (
    os.environ.get("LOGOS_INSTANCE_NAME")
    or "Hermes"
)

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
_VERSION_LABEL = f"v{_APP_VERSION} · {_BUILD_SHA}"
# Agent sandbox runtime seam. NullBackend (no sandboxes) until epic E2.
from gateway.sandbox_backend import (  # noqa: E402
    NO_BACKEND_MESSAGE as _NO_BACKEND_MESSAGE,
    get_backend as _get_sandbox_backend,
)

# Chat / spawn requests answer with this until E2 lands a sandbox backend.
_NO_AGENTS_RUNNING = {
    "error": "no_agents_running",
    "message": "No agents are running yet: the v2 sandbox backend arrives in epic E2.",
}


def _no_agents_running_response() -> web.Response:
    return web.json_response(_NO_AGENTS_RUNNING, status=503)

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


# mtime-cached HTML loader. Previously `main_app.html` / `login.html` /
# `setup.html` were read once at import time and frozen into module-
# level strings, which meant a running gateway never picked up HTML
# edits — every "my change isn't showing up" ticket traced back to the
# same stale snapshot. Re-reading on mtime change is effectively free
# (main_app.html is ~300 KB, read at most once per page navigation) and
# removes that whole class of foot-gun.
_HTML_CACHE: dict[str, tuple[float, str]] = {}


def _read_html(filename: str) -> str:
    path = _html_dir / filename
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _HTML_CACHE.get(filename, (0.0, ""))[1]
    cached = _HTML_CACHE.get(filename)
    if cached is None or cached[0] != mtime:
        _HTML_CACHE[filename] = (mtime, path.read_text(encoding="utf-8"))
    return _HTML_CACHE[filename][1]


# Warm the cache at import time so the first hit doesn't pay the read.
_read_html("main_app.html")
_read_html("login.html")
_read_html("setup.html")


def _check_auth(request: web.Request) -> bool:
    """Legacy internal-token check — still used by /sessions endpoint."""
    token = (
        os.environ.get("LOGOS_INTERNAL_TOKEN")
        or ""
    )
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {token}"


def _ensure_admin_exists() -> None:
    """Seed the first admin account from env vars if the users table is empty."""
    admin_email = (
        os.environ.get("LOGOS_ADMIN_EMAIL")
        or ""
    ).strip()
    admin_pass = (
        os.environ.get("LOGOS_ADMIN_PASSWORD")
        or ""
    ).strip()
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
            display_name=(
                os.environ.get("LOGOS_ADMIN_NAME")
                or "Admin"
            ),
        )
        logger.info("Seeded admin account: %s", admin_email)
    except Exception as exc:
        logger.warning("Failed to seed admin account: %s", exc)


# ── Unified Services (tool credentials) ──────────────────────────────────
#
# The gateway-wide tool-credential catalogue (TOOL_INTEGRATIONS) was
# removed 2026-04-21. It stored keys in Logos's os.environ + DB but
# never forwarded them into sandboxes, so hermes-side tools couldn't
# use them anyway. Replaced by agent_env_credentials + the /setup
# slash command in chat, which writes into each agent's own sandbox
# .env. See admin_handlers.handle_agent_env_credentials_* and
# the /setup handler in this file. Messaging integrations still use
# the /api/services/messaging/* endpoints below — those are unrelated
# and still functional.


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
        _hermes_home = Path(os.environ.get("LOGOS_HOME") or str(Path.home() / ".logos"))
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


async def _handle_messaging_catalogue(request: web.Request) -> web.Response:
    """GET /api/services/messaging — list messaging platform integrations."""
    from gateway.services import get_messaging_integrations
    return web.json_response({"messaging": get_messaging_integrations()})


async def _handle_messaging_set_key(request: web.Request) -> web.Response:
    """POST /api/services/messaging/keys — set a messaging token (validate first)."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "error": "env_var and value required"}, status=400)

    from gateway.services import MESSAGING_INTEGRATIONS, validate_messaging_credential, set_credential, get_messaging_integrations
    if env_var not in MESSAGING_INTEGRATIONS:
        return web.json_response({"ok": False, "error": f"Unknown platform: {env_var}"}, status=400)

    # Validate before saving
    result = await validate_messaging_credential(env_var, value)
    if not result["ok"]:
        return web.json_response({"ok": False, "error": result["message"]}, status=400)

    set_credential(env_var, value)

    # LOG-44.3: adapters live in the sandbox now; saving a global
    # messaging token just updates Logos's DB + env so the sandbox
    # spawner picks it up on the next respawn. There is no Logos-side
    # adapter to reconnect here.

    return web.json_response({"ok": True, "details": result.get("details", {}), "messaging": get_messaging_integrations()})


async def _handle_messaging_delete_key(request: web.Request) -> web.Response:
    """DELETE /api/services/messaging/keys — remove a messaging token."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    if not env_var:
        return web.json_response({"ok": False, "error": "env_var required"}, status=400)

    from gateway.services import MESSAGING_INTEGRATIONS, delete_credential, get_messaging_integrations
    if env_var not in MESSAGING_INTEGRATIONS:
        return web.json_response({"ok": False, "error": f"Unknown platform: {env_var}"}, status=400)

    delete_credential(env_var)

    # LOG-44.3: no Logos-side adapter to disconnect; sandbox hermes
    # picks up the token removal on its next respawn or credential
    # refresh.

    return web.json_response({"ok": True, "messaging": get_messaging_integrations()})


async def _handle_messaging_validate(request: web.Request) -> web.Response:
    """POST /api/services/messaging/validate — test a token without saving."""
    user = request.get("current_user", {})
    if user.get("role") not in ("admin",):
        raise web.HTTPForbidden(text='{"error":"admin_required"}', content_type="application/json")
    body = await request.json()
    env_var = (body.get("env_var") or "").strip()
    value = (body.get("value") or "").strip()
    if not env_var or not value:
        return web.json_response({"ok": False, "message": "env_var and value required"}, status=400)

    from gateway.services import validate_messaging_credential
    result = await validate_messaging_credential(env_var, value)
    return web.json_response(result)


async def _handle_setup_page(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if is_setup_completed():
        raise web.HTTPFound("/")
    html = _read_html("setup.html").replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def _handle_setup_status(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    return web.json_response({"completed": is_setup_completed()})


async def _handle_setup_wipe(request: web.Request) -> web.Response:
    """POST /api/setup/wipe — destructive factory reset.

    True first-install simulation: wipes everything user-scoped so the
    next visit goes through /setup, which creates a fresh admin account.

    Wipes:
      - Every agent (DB row + on-disk ~/.logos/agents/<name>/ via
        delete_agent's filesystem hook)
      - Dispatch ledger, evolution_proposals, audit log
      - All users (caller is logging themselves out — the wizard's
        admin-create step will mint the next admin account)
      - Auth session cookie (force a clean re-auth flow)
      - The setup_completed flag (so /setup re-prompts on next load)

    Deliberately KEEPS:
      - Souls / skills / themes (platform fixtures, not user data)

    Body must contain {"confirm": "WIPE"} — the literal string. The
    UI's type-to-confirm modal sends exactly that, so a stray POST
    can't accidentally nuke a deployment.
    """
    from gateway.auth.db import (
        list_agents, delete_agent, write_audit_log,
        reset_setup_completed, _conn,
    )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if (body.get("confirm") or "") != "WIPE":
        return web.json_response(
            {"error": "confirmation_missing", "detail": "Body must contain {\"confirm\": \"WIPE\"}."},
            status=400,
        )

    user_id = request["current_user"]["sub"]
    counts = {"agents": 0, "users": 0}

    # Delete all agents — delete_agent() already wipes ~/.logos/agents/<name>/
    # (sessions, memories, logs) and cleans dispatches/proposals
    # for that agent.
    for a in list_agents():
        try:
            delete_agent(a["id"])
            counts["agents"] += 1
        except Exception as exc:
            logger.warning("wipe: failed to delete agent %s (%s): %s", a.get("name"), a["id"], exc)

    # Audit log BEFORE we truncate it + wipe the user (FK on user_id).
    # Logged at warn level since this is a high-impact action.
    write_audit_log(user_id, "setup_wipe", metadata=counts, ip_address=request.remote)

    # Truncate orphan tables that aren't tied to a specific agent.
    with _conn() as conn:
        for tbl in ("dispatches", "evolution_proposals", "audit_log"):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass  # missing table is fine

        # Wipe ALL users — this is true factory reset, not just data.
        # The next /setup visit will mint a fresh admin account from
        # the wizard's user-creation step. Caller's session cookie is
        # cleared on the response below so the redirect lands on /setup
        # without an auth loop.
        try:
            cur = conn.execute("DELETE FROM users")
            counts["users"] = cur.rowcount
        except Exception as exc:
            logger.warning("wipe: user delete failed: %s", exc)

    reset_setup_completed()
    # Setup is open again: it needs a fresh one-time token.
    from gateway import setup_handlers as _setup_handlers
    if _hermes_home is not None:
        _setup_handlers.ensure_setup_token(_hermes_home)
    logger.warning("setup wipe by user_id=%s: deleted %d agent(s), %d user(s)",
                   user_id, counts["agents"], counts["users"])
    resp = web.json_response({"ok": True, "deleted": counts})
    # Strip the session cookies so the browser can't keep using a token
    # whose user no longer exists — clear_auth_cookies handles all three
    # (access_token / refresh_token / csrf_token) with the right paths.
    from gateway.auth.tokens import clear_auth_cookies
    clear_auth_cookies(resp)
    return resp


async def _handle_index(request: web.Request) -> web.Response:
    from gateway.auth.db import is_setup_completed
    if not is_setup_completed():
        raise web.HTTPFound("/login")
    inject = f'<script>window.__LOGOS__={{runtimeMode:"openshell",version:"{_VERSION_LABEL}"}};window._hueEpochMs={_HUE_EPOCH_MS};</script>'
    html = _read_html("main_app.html").replace("</head>", inject + "</head>", 1)
    # no-store so no intermediate cache (browser, reverse proxy, CDN)
    # can hold a stale version after an HTML edit. The ES module
    # cache-bust query strings inside the HTML already version the
    # JS; the HTML itself must be cheap to re-fetch.
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def _handle_app(request: web.Request) -> web.Response:
    """GET / — the v2 dashboard (gateway/html/app.html plus the modules under gateway/ui/)."""
    from gateway.auth.db import is_setup_completed
    if not is_setup_completed():
        raise web.HTTPFound("/login")
    return web.Response(text=_read_html("app.html"), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def _handle_login_page(request: web.Request) -> web.Response:
    html = _read_html("login.html").replace("__VERSION_LABEL__", _VERSION_LABEL)
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


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
            "agent_name": s.get("agent_name", ""),
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
            # LOG-51.4: forward task_id so the Live Executions UI can
            # render a per-row Stop button that POSTs
            # /chat/{task_id}/cancel. Only the v2 dispatch path seeds
            # task_id today; older/legacy rows omit the key and the
            # UI hides the button accordingly.
            "task_id": s.get("task_id"),
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

    current_model = os.getenv("LOGOS_MODEL") or os.getenv("LLM_MODEL") or ""

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


# ── Souls admin CRUD ────────────────────────────────────────────────────────
# Same shape as the Sandbox Policies editor: list + manifest/md files
# + Save/hot-reload/Delete. Mutations re-read the souls directory via
# souls.load_souls() so agents see edits on their next dispatch without
# a gateway restart.

import re as _souls_re
_SOULS_SLUG_RE = _souls_re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _souls_count_agents_using(slug: str) -> int:
    """Count agents currently tied to this soul slug."""
    try:
        agents = auth_db.list_agents()
    except Exception:
        return 0
    return sum(1 for a in (agents or []) if (a.get("soul_slug") or "") == slug)


async def _handle_admin_souls_list(request: web.Request) -> web.Response:
    """GET /api/admin/souls — list souls with per-soul agent usage counts."""
    from gateway import souls as _souls
    registry = _souls.get_soul_registry()
    out = []
    for slug, soul in registry.items():
        out.append({
            **soul.to_dict(include_soul_md=False),
            "agent_count": _souls_count_agents_using(slug),
        })
    # In-use first (so admins see what's affecting live agents), then
    # alphabetical.
    out.sort(key=lambda s: (-s.get("agent_count", 0), s.get("slug", "")))
    return web.json_response({"souls": out})


async def _handle_admin_soul_get(request: web.Request) -> web.Response:
    """GET /api/admin/souls/{slug} — raw manifest YAML + soul.md text.

    The editor writes both files, so the response carries both so the
    UI can round-trip without surprise overrides.
    """
    from gateway import souls as _souls
    slug = request.match_info["slug"]
    soul_dir = _souls._SOULS_DIR / slug
    manifest_path = soul_dir / "soul.manifest.yaml"
    soul_md_path = soul_dir / "soul.md"
    if not manifest_path.exists():
        raise web.HTTPNotFound(reason=f"soul not found: {slug}")
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        manifest_text = ""
    soul_md_text = ""
    if soul_md_path.exists():
        try:
            soul_md_text = soul_md_path.read_text(encoding="utf-8")
        except OSError:
            soul_md_text = ""
    return web.json_response({
        "slug": slug,
        "manifest_yaml": manifest_text,
        "soul_md": soul_md_text,
        "agent_count": _souls_count_agents_using(slug),
    })


async def _handle_admin_soul_put(request: web.Request) -> web.Response:
    """PUT /api/admin/souls/{slug} — create or update a soul.

    Body: ``{"manifest_yaml": "...", "soul_md": "..."}``. Validates the
    manifest YAML parses to a dict with a matching slug, writes both
    files atomically (tmpfile + rename), then hot-reloads the soul
    registry so the next agent dispatch picks up the edit.
    """
    from gateway import souls as _souls
    import yaml as _yaml
    import tempfile as _tempfile
    slug = request.match_info["slug"]
    if not _SOULS_SLUG_RE.match(slug):
        raise web.HTTPBadRequest(reason="invalid slug (lowercase, alphanumeric, _-)")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    manifest_yaml = body.get("manifest_yaml")
    soul_md = body.get("soul_md", "")
    if not isinstance(manifest_yaml, str) or not manifest_yaml.strip():
        raise web.HTTPBadRequest(reason="body.manifest_yaml is required")
    if not isinstance(soul_md, str):
        raise web.HTTPBadRequest(reason="body.soul_md must be a string")
    # Validate YAML parses and the slug inside matches the URL. Guards
    # against typos that would otherwise leave the filesystem and the
    # manifest out of sync (registry keys by data.slug, not dirname).
    try:
        parsed = _yaml.safe_load(manifest_yaml) or {}
    except _yaml.YAMLError as exc:
        return web.json_response({"error": "yaml_parse_error", "detail": str(exc)}, status=400)
    if not isinstance(parsed, dict):
        return web.json_response({"error": "manifest_not_a_mapping"}, status=400)
    manifest_slug = parsed.get("slug") or parsed.get("id")
    if manifest_slug and manifest_slug != slug:
        return web.json_response(
            {"error": "slug_mismatch", "detail": f"manifest slug={manifest_slug!r} must match URL slug={slug!r}"},
            status=400,
        )
    # Write atomically — write to a tmp file in the same dir then
    # rename. Both files together so a failure partway doesn't leave
    # the dir in a half-updated state the registry would then pick up.
    soul_dir = _souls._SOULS_DIR / slug
    soul_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in (("soul.manifest.yaml", manifest_yaml), ("soul.md", soul_md)):
        target = soul_dir / filename
        tmp = _tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(soul_dir),
            prefix=f".{filename}.", suffix=".tmp", delete=False,
        )
        try:
            tmp.write(content)
            tmp.flush()
            tmp.close()
            os.replace(tmp.name, target)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
    # Hot-reload the in-process soul registry so the next dispatch
    # reads the new manifest. Sandboxes themselves don't cache souls
    # (they re-run sandbox_worker per dispatch and pull config fresh),
    # so no sandbox-side work needed.
    try:
        _souls.load_souls()
    except Exception as exc:
        logger.warning("load_souls reload failed after PUT %s: %s", slug, exc)
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "update_soul",
        target_type="soul", target_id=slug,
    )
    return web.json_response({
        "ok": True,
        "slug": slug,
        "agent_count": _souls_count_agents_using(slug),
    })


async def _handle_admin_soul_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/souls/{slug} — remove a soul directory.

    Refuses if any agent still picks the soul — the admin needs to
    migrate those agents first, mirroring sandbox_preset behaviour.
    """
    from gateway import souls as _souls
    import shutil as _shutil
    slug = request.match_info["slug"]
    soul_dir = _souls._SOULS_DIR / slug
    if not soul_dir.is_dir():
        raise web.HTTPNotFound(reason="soul not found")
    n = _souls_count_agents_using(slug)
    if n > 0:
        return web.json_response({
            "error": "in_use",
            "detail": f"{n} agent(s) still use this soul",
            "agent_count": n,
        }, status=409)
    try:
        _shutil.rmtree(soul_dir)
    except OSError as exc:
        return web.json_response({"error": "delete_failed", "detail": str(exc)}, status=500)
    try:
        _souls.load_souls()
    except Exception:
        pass
    auth_db.write_audit_log(
        request.get("current_user", {}).get("sub"),
        "delete_soul",
        target_type="soul", target_id=slug,
    )
    return web.json_response({"ok": True})


async def _handle_instances_get(request: web.Request) -> web.Response:
    """GET /instances — sandboxes from the backend (none until E2)."""
    backend = request.app.get("sandbox_backend") or _get_sandbox_backend()
    return web.json_response({
        "instances": [st.to_dict() for st in backend.list()],
        "resources": {"can_spawn": False, "reason": _NO_BACKEND_MESSAGE,
                      "executor": "none"},
        "queue": [],
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

    # RBAC: toolset overrides require override_toolsets permission
    if tool_overrides and not _has_perm(caller_role, "override_toolsets"):
        return web.json_response(
            {"error": "forbidden", "message": "Toolset overrides require the admin role"},
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

    # Validation above stays so the request contract is unchanged;
    # routing + spawning need a sandbox backend (E2).
    return _no_agents_running_response()


@require_permission("delete_instance")
@require_csrf
async def _handle_instances_delete(request: web.Request) -> web.Response:
    name   = request.match_info["name"]
    caller = request.get("current_user") or {}
    if name == "hermes":
        raise web.HTTPForbidden(reason="Cannot delete the primary hermes deployment")
    backend = request.app.get("sandbox_backend") or _get_sandbox_backend()
    try:
        await asyncio.to_thread(backend.delete, name)
    except NotImplementedError:
        return _no_agents_running_response()
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
    """Resolve the LOGOS_HOME directory for a named instance.

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
    # 1-year immutable cache. The favicon is a tiny static asset that
    # changes at most once per release; browsers skip even the
    # conditional GET when max-age is this high with immutable, which
    # clears the /favicon.ico line-noise out of DevTools Network tab.
    cache_hdr = "public, max-age=31536000, immutable"
    for p in candidates:
        if p.exists():
            data = p.read_bytes()
            import hashlib
            etag = '"' + hashlib.md5(data).hexdigest()[:16] + '"'
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers={"ETag": etag, "Cache-Control": cache_hdr})
            return web.Response(
                body=data,
                content_type="image/x-icon",
                headers={"Cache-Control": cache_hdr, "ETag": etag},
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


async def _handle_sessions(request: web.Request) -> web.Response:
    if not _check_auth(request):
        raise web.HTTPUnauthorized()
    runner: Any = request.app["runner"]
    sessions = runner.session_store.list_sessions()
    return web.json_response([s.to_dict() for s in sessions])


async def _handle_api_platform_sessions(request: web.Request) -> web.Response:
    """GET /api/platform-sessions?platform=telegram — list server-side sessions by platform.

    Queries the session SQLite DB directly rather than the in-memory
    session store. The in-memory store only has sessions from this
    gateway's lifetime; the DB has the full history — which is what
    the user expects when they click the 📱 TG pill in /chats.
    """
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    platform_filter = request.rel_url.query.get("platform")
    agent_id_filter = request.rel_url.query.get("agent_id")
    # LOG-39: "Show hidden" toggle. When include_hidden=1, return
    # soft-deleted (hidden=1) sessions alongside visible ones so users
    # can restore or permanently delete them. Default 0 keeps the
    # existing "clean list" UX.
    include_hidden = request.rel_url.query.get("include_hidden") in ("1", "true", "yes")
    runner: Any = request.app["runner"]
    db = getattr(runner.session_store, "_db", None)
    if not db:
        return web.json_response([])

    # When an agent_id filter is given, JOIN through the auth DB's
    # dispatches table to find sessions that belong to that agent.
    # This lets the UI show only Hermes's Telegram conversations
    # when Hermes is selected, not Henry's.
    try:
        import sqlite3 as _sql3
        from pathlib import Path as _Path
        _auth_path = _Path(
            os.getenv("LOGOS_HOME")
            or str(_Path.home() / ".logos")
        ) / "auth.db"

        if agent_id_filter and platform_filter and _auth_path.exists():
            # Cross-DB query: find session_ids from dispatches where
            # agent_id matches, then look them up in the state DB.
            with _sql3.connect(str(_auth_path), timeout=2) as _ac:
                _ac.row_factory = _sql3.Row
                _origin = f"platform_{platform_filter}"
                _dispatch_rows = _ac.execute(
                    """SELECT DISTINCT session_id FROM dispatches
                       WHERE agent_id = ? AND origin = ?
                       ORDER BY started_at DESC LIMIT 50""",
                    (agent_id_filter, _origin),
                ).fetchall()
            _session_ids = [r["session_id"] for r in _dispatch_rows if r["session_id"]]
            if not _session_ids:
                rows = []
            else:
                placeholders = ",".join("?" for _ in _session_ids)
                rows = db._conn.execute(
                    f"""SELECT id, source, title, started_at, ended_at, message_count,
                              user_id, model
                       FROM sessions WHERE id IN ({placeholders})
                       ORDER BY started_at DESC""",
                    _session_ids,
                ).fetchall()
        elif platform_filter:
            _hidden_clause = "" if include_hidden else " AND COALESCE(hidden, 0) = 0"
            rows = db._conn.execute(
                f"""SELECT id, source, title, started_at, ended_at, message_count,
                           user_id, model, COALESCE(hidden, 0) as hidden
                    FROM sessions WHERE source = ?{_hidden_clause}
                    ORDER BY started_at DESC LIMIT 50""",
                (platform_filter,),
            ).fetchall()
        else:
            _hidden_clause = "" if include_hidden else " AND COALESCE(hidden, 0) = 0"
            rows = db._conn.execute(
                f"""SELECT id, source, title, started_at, ended_at, message_count,
                           user_id, model, COALESCE(hidden, 0) as hidden
                    FROM sessions WHERE source NOT IN ('local', 'cron'){_hidden_clause}
                    ORDER BY started_at DESC LIMIT 50""",
            ).fetchall()
    except Exception:
        rows = []

    from datetime import datetime as _dt
    result = []
    for r in rows:
        started = _dt.fromtimestamp(r["started_at"]) if r["started_at"] else None
        updated = _dt.fromtimestamp(r["ended_at"]) if r["ended_at"] else started
        # LOG-39: include `hidden` so the UI can dim / add a restore
        # affordance on soft-deleted rows when include_hidden=1 is set.
        try:
            _hidden_flag = bool(r["hidden"]) if "hidden" in r.keys() else False
        except Exception:
            _hidden_flag = False
        result.append({
            "session_id": r["id"],
            "session_key": r["id"],
            "display_name": r["title"] or r["user_id"] or r["id"][:20],
            "platform": r["source"],
            "chat_type": "dm",
            "created_at": started.isoformat() if started else "",
            "updated_at": (updated or started).isoformat() if started else "",
            "message_count": r["message_count"] or 0,
            "hidden": _hidden_flag,
        })
    return web.json_response(result)


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


async def _handle_api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/platform-sessions/{session_id} — soft-delete (hide) a session.

    Sets ``hidden=1`` on the session row so it disappears from the
    sidebar without destroying messages, embeddings, or dispatches.
    The data stays searchable via session_search and the Runs tab.
    """
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    session_id = request.match_info["session_id"]
    runner: Any = request.app["runner"]
    db = getattr(runner.session_store, "_db", None)
    if not db:
        return web.json_response({"error": "session DB not available"}, status=503)
    try:
        db._conn.execute("UPDATE sessions SET hidden=1 WHERE id=?", (session_id,))
        db._conn.commit()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True})


async def _handle_api_session_restore(request: web.Request) -> web.Response:
    """POST /api/platform-sessions/{session_id}/restore — un-soft-delete a session (LOG-39).

    Inverse of _handle_api_session_delete. Flips ``hidden=0`` so the row
    reappears in the sidebar. Messages/embeddings/dispatches never moved
    (soft-delete preserves everything), so restore is a one-column UPDATE.
    """
    current_user = request.get("current_user") or {}
    if current_user.get("role", "viewer") not in ("admin", "operator"):
        raise web.HTTPForbidden()
    session_id = request.match_info["session_id"]
    runner: Any = request.app["runner"]
    db = getattr(runner.session_store, "_db", None)
    if not db:
        return web.json_response({"error": "session DB not available"}, status=503)
    try:
        db._conn.execute("UPDATE sessions SET hidden=0 WHERE id=?", (session_id,))
        db._conn.commit()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True})


async def _handle_chat_cancel(request: web.Request) -> web.Response:
    """POST /chat/{task_id}/cancel — stop an in-flight chat turn.

    ``task_id`` is the hermes run id sent to the browser as ``task_started``.
    """
    task_id = request.match_info["task_id"]
    links = request.app.get("agent_links")
    agent_name = (request.app.get("chat_runs") or {}).get(task_id)
    if links is None:
        return _no_agents_running_response()
    if not agent_name:
        return web.json_response({"error": "not_found", "message": "No such running reply."}, status=404)
    from gateway.agent_links import AgentLinkError as _AgentLinkError
    from gateway.hermes_client import HermesError as _HermesError
    try:
        client = await links.client(agent_name)
        await client.stop_run(task_id)
    except (_AgentLinkError, _HermesError) as exc:
        return web.json_response({"error": "stop_failed", "message": str(exc)}, status=502)
    return web.json_response({"ok": True, "task_id": task_id})


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
        from gateway.lib.transcription import transcribe_audio
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
    # access to that server on the gateway's MCP proxy.
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
                _grant(_sess_id, _srv_name)
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

async def _handle_world_state(request: web.Request) -> web.Response:
    """GET /world/state — snapshot of all named agents for world awareness.

    Consumed by the ``get_agent_world`` tool inside sandboxes so an
    agent can check who else is around, their soul, appearance, and
    whether they are currently busy. Also used by the dashboard to
    render the world roster without hitting the admin ``/agents``
    endpoint (which returns much richer, caller-restricted data).

    Query params:
      ?agent_id=<id>  — mark this agent as "self" in the response
                        (``is_self: true`` on the matching row).
    """
    from gateway.world_awareness import build_world_snapshot
    self_id = request.query.get("agent_id")
    snapshot = build_world_snapshot(self_agent_id=self_id, include_self=True)
    return web.json_response(snapshot)



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
    agent_id = body.get("agent_id")

    # M6 correlation IDs: stamp every log record emitted during this request
    # with identifiers that can be grepped from the unified log afterwards.
    # Generate a fresh task_id per dispatch so a single chat turn (worker
    # dispatch → inference → streaming reply → tool calls) is queryable as
    # one unit via `logos debug tail --filter task_id=<id>`. See MISSING.md M6.
    import uuid as _uuid
    _task_id = _uuid.uuid4().hex[:12]
    try:
        from gateway.run import set_log_context
        _current_user = request.get("current_user") or {}
        set_log_context(
            session_id=session_id,
            task_id=_task_id,
            user_id=_current_user.get("sub") or _current_user.get("id"),
            chat_id=body.get("chat_id"),
            # worker_id gets set later once the sandbox_name is resolved —
            # at this point `agent_id` is still the raw DB id, not the
            # sanitised sandbox name the worker registry keys on.
        )
    except Exception as _ctx_exc:
        logger.debug("set_log_context skipped: %s", _ctx_exc)

    # OpenShell-only routing: every chat must target a named agent that has
    # its own sandbox worker. No in-process fallback.
    if not agent_id:
        raise web.HTTPBadRequest(
            reason="agent_id is required — chat must target a named sandboxed agent",
        )
    try:
        _agent_config = auth_db.get_agent(agent_id)
    except Exception:
        _agent_config = None
    if not _agent_config:
        raise web.HTTPNotFound(reason=f"agent {agent_id} not found")

    # LOG-25.3: private-agent visibility. Non-admin/operator callers
    # can chat with (a) shared agents, or (b) their own private ones.
    # A private agent owned by someone else is effectively not-theirs —
    # return 404 (not 403) so the endpoint doesn't leak agent existence
    # to users who shouldn't know about it.
    _auth_user = request.get("current_user") or {}
    if _auth_user.get("role") not in ("admin", "operator"):
        _is_shared = bool(_agent_config.get("shared"))
        _creator = _agent_config.get("creator_id") or ""
        _caller = _auth_user.get("id") or ""
        if not _is_shared and _creator and _creator != _caller:
            logger.info(
                "chat: denying %s access to private agent %s (owner=%s)",
                _caller or "anon", agent_id, _creator,
            )
            raise web.HTTPNotFound(reason=f"agent {agent_id} not found")

    if not message:
        raise web.HTTPBadRequest(reason="message is required")

    # Dispatch to the agent's hermes server inside its OpenShell sandbox
    # (gateway/agent_links.py, gateway/agent_chat.py). Without the
    # OpenShell backend there is nothing to dispatch to.
    links = request.app.get("agent_links")
    backend = request.app.get("sandbox_backend") or _get_sandbox_backend()
    agent_name = _agent_config.get("name") or ""
    if links is None:
        logger.info("chat: agent_id=%r has no running sandbox (no backend)", agent_id)
        return _no_agents_running_response()
    from gateway import agent_chat as _agent_chat
    try:
        _definition = await asyncio.to_thread(links.runtime.load, agent_name)
    except Exception:
        raise web.HTTPNotFound(reason=f"agent {agent_id} not found")
    if not _agent_chat.may_use(_agent_chat.owners_of(_definition),
                               _agent_chat.username_of(_auth_user, auth_db)):
        # Someone else's private agent: same answer as an unknown one.
        raise web.HTTPNotFound(reason=f"agent {agent_id} not found")
    status = await asyncio.to_thread(backend.status, agent_name)
    if not status.running:
        return web.json_response({
            "error": "agent_not_running",
            "message": f"{agent_name} is not running ({status.detail or status.state}). "
                       f"Start it with: scripts/logos-agent up {agent_name}",
        }, status=503)
    from gateway.agent_links import AgentLinkError as _AgentLinkError
    from gateway.hermes_client import HermesError as _HermesError
    try:
        await links.health(agent_name)
        client = await links.client(agent_name)
    except (_AgentLinkError, _HermesError) as exc:
        logger.warning("chat: %s unreachable: %s", agent_name, exc)
        return web.json_response({
            "error": "agent_unreachable",
            "message": f"Could not reach {agent_name}: {exc}",
        }, status=503)

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    runs: dict = request.app["chat_runs"]
    started: list = []

    def _remember(run_id: str) -> None:
        started.append(run_id)
        runs[run_id] = agent_name

    hermes_session = _agent_chat.session_id_for(
        _auth_user.get("sub") or _auth_user.get("id") or "", str(session_id))

    beat = asyncio.ensure_future(_agent_chat.keep_alive(resp.write))
    try:
        async for payload in _agent_chat.stream_run(
            client, message, session_id=hermes_session,
            model=_agent_config.get("model") or "", on_started=_remember,
        ):
            await resp.write(_agent_chat.sse(payload))
    except (ConnectionResetError, asyncio.CancelledError):
        # The browser went away. The run carries on inside the agent and its
        # result is kept in the agent's own session history.
        logger.info("chat: client left during a run for %s", agent_name)
        raise
    finally:
        beat.cancel()
        for run_id in started:
            runs.pop(run_id, None)
    await resp.write_eof()
    return resp


async def start_http_api(runner: Any, port: int = 8091) -> None:
    """Start the aiohttp server. Call as an asyncio task."""
    global _start_time
    _start_time = time.time()

    # Initialise auth DB alongside existing Logos state
    global _hermes_home
    hermes_home = Path(os.environ.get("LOGOS_HOME") or str(Path.home() / ".logos"))
    _hermes_home = hermes_home
    auth_db.init_db(hermes_home)

    # Ensure a stable JWT secret exists for local installs.
    # A deployment may set LOGOS_JWT_SECRET;
    # local desktop/CLI installs never set it.  Generate once, persist to
    # ~/.logos/.jwt_secret so tokens survive gateway restarts without forcing
    # re-login every time.  Also treat the known template placeholder as unset —
    # k8s/02-secret.yaml ships with REPLACE_WITH_JWT_SECRET so a fresh cluster
    # with un-edited secrets gets a real random value rather than the publicly
    # known placeholder.
    _KNOWN_JWT_PLACEHOLDERS = {"", "REPLACE_WITH_JWT_SECRET", "replace_with_jwt_secret"}
    _current_jwt = (
        os.environ.get("LOGOS_JWT_SECRET")
        or ""
    )
    if _current_jwt in _KNOWN_JWT_PLACEHOLDERS:
        import secrets as _secrets
        _jwt_secret_path = hermes_home / ".jwt_secret"
        if _jwt_secret_path.exists():
            _persisted = _jwt_secret_path.read_text().strip()
            os.environ["LOGOS_JWT_SECRET"] = _persisted
        else:
            _jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
            _new_secret = _secrets.token_hex(32)
            _jwt_secret_path.write_text(_new_secret)
            _jwt_secret_path.chmod(0o600)
            os.environ["LOGOS_JWT_SECRET"] = _new_secret
            logger.info("Generated new JWT secret at %s", _jwt_secret_path)

    # ── Host preflight: fs.inotify.max_user_instances ──────────────
    # Every OpenShell gateway runs a full k3s cluster inside its
    # container, and k3s's kubelet/containerd/coredns/CNI chain
    # consumes ~50-80 inotify instances per cluster. The kernel
    # default (``512`` on most distros) caps you at ~7 gateways
    # before provisioning starts failing with "k3s healthcheck loop"
    # which surfaces in the UI as the generic "gateway info reported
    # the underlying gateway is unreachable."
    #
    # We can't bump the sysctl from inside the gateway process (needs
    # root), but a warning at boot + the fix inline saves an onboarding
    # trap where someone adds a 4th-8th agent months later and has no
    # idea why the route won't come up.
    try:
        _mui_path = pathlib.Path("/proc/sys/fs/inotify/max_user_instances")
        _muw_path = pathlib.Path("/proc/sys/fs/inotify/max_user_watches")
        _mui = int(_mui_path.read_text().strip()) if _mui_path.exists() else 0
        _muw = int(_muw_path.read_text().strip()) if _muw_path.exists() else 0
        # Rough rule of thumb: one openshell k3s cluster ~= 70 instances.
        # Warn below 2048 (which gives ~28 clusters headroom, enough for
        # any realistic homelab multi-agent setup).
        _MUI_SAFE_THRESHOLD = 2048
        _MUW_SAFE_THRESHOLD = 524288
        _route_budget = max(1, _mui // 70) if _mui else 0
        if _mui and _mui < _MUI_SAFE_THRESHOLD:
            logger.warning(
                "preflight: fs.inotify.max_user_instances=%d — you can reliably "
                "run ~%d OpenShell routes on this host before provisioning "
                "starts failing with 'gateway info reported unreachable'. "
                "Raise with:  sudo sysctl -w fs.inotify.max_user_instances=8192  "
                "(and persist: echo 'fs.inotify.max_user_instances=8192' | sudo "
                "tee -a /etc/sysctl.d/99-openshell.conf && sudo sysctl --system)",
                _mui, _route_budget,
            )
        elif _mui:
            logger.info(
                "preflight: fs.inotify.max_user_instances=%d (~%d routes headroom) ✓",
                _mui, _route_budget,
            )
        if _muw and _muw < _MUW_SAFE_THRESHOLD:
            logger.warning(
                "preflight: fs.inotify.max_user_watches=%d — bump to 1048576 "
                "to avoid containerd stalls on large sandbox filesystems.",
                _muw,
            )
    except Exception as _pf_exc:
        logger.debug("preflight inotify check skipped: %s", _pf_exc)

    # LOGOS_WIPE_ON_START: wipe setup state so /setup always runs fresh (setup-test deployments)
    _wipe_flag = (
        os.environ.get("LOGOS_WIPE_ON_START")
        or ""
    ).lower()
    if _wipe_flag in ("1", "true", "yes"):
        try:
            auth_db.reset_setup_completed()
            logger.info("LOGOS_WIPE_ON_START: wiped setup state")
        except Exception as _wipe_err:
            logger.warning("LOGOS_WIPE_ON_START: partial failure: %s", _wipe_err)
    # Env-var admin seeding (takes priority over generic seed)
    _ensure_admin_exists()
    # No default admin is created. Until setup has run there are no accounts, and setup needs
    # the one-time token below (or a request from this machine).
    from gateway import setup_handlers as _setup_handlers
    _setup_token = _setup_handlers.ensure_setup_token(hermes_home)
    if _setup_token:
        logger.warning("Setup is not finished. Open /setup?token=%s on this server's address "
                       "(or run: logos gateway setup-link).", _setup_token)

    # Zombie-dispatch sweep: any dispatch row still at status='running'
    # when the process starts was orphaned by a crash or restart of the
    # previous gateway instance — no worker is alive to complete it, so
    # leaving it 'running' contaminates the Activity → Events view as
    # though a task is in-flight. Mark all such rows as 'interrupted'
    # with a short explanatory error. One-shot; subsequent dispatches
    # follow the normal running → ok/error lifecycle.
    try:
        _swept = auth_db.sweep_orphaned_dispatches()
        if _swept:
            logger.info("startup: swept %d orphaned dispatch row(s) (gateway restarted mid-flight)", _swept)
    except Exception as _sweep_err:
        logger.warning("startup: dispatch sweep failed: %s", _sweep_err)

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

    # Agent sandbox runtime (NullBackend until E2), owned by the runner.
    app["sandbox_backend"] = getattr(runner, "sandbox_backend", None) or _get_sandbox_backend()
    logger.info("Sandbox backend: %s", type(app["sandbox_backend"]).__name__)

    # Links to each agent's hermes server (only with a backend that runs agents).
    app["chat_runs"] = {}
    app["agent_links"] = None
    from gateway import agent_chat as _agent_chat
    _agent_runtime = _agent_chat.runtime_of(app["sandbox_backend"])
    if _agent_runtime is not None:
        from gateway.agent_links import AgentLinks as _AgentLinks
        app["agent_links"] = _AgentLinks(_agent_runtime)

        async def _close_agent_links(_app: web.Application) -> None:
            await _app["agent_links"].close()
        app.on_cleanup.append(_close_agent_links)

        async def _agents_boot_check(_app: web.Application) -> None:
            # S2.8: on boot, register the defined agents and report which are reachable.
            async def _check() -> None:
                try:
                    names = await asyncio.to_thread(_agent_chat.sync_definitions, _agent_runtime, auth_db)
                    phases = {st.name: st for st in await asyncio.to_thread(_app["sandbox_backend"].list)}
                except Exception as exc:
                    logger.warning("Agent boot check failed: %s", exc)
                    return
                for name in names:
                    st = phases.get(name)
                    if not (st and st.running):
                        logger.warning("Agent %s: not running (%s)", name, st.detail if st else "no sandbox")
                        continue
                    try:
                        h = await _app["agent_links"].health(name)
                        logger.info("Agent %s: healthy (hermes %s)", name, h.get("version", "?"))
                    except Exception as exc:
                        logger.warning("Agent %s: sandbox is Ready but hermes is unreachable: %s", name, exc)
            _app["agents_boot_check"] = asyncio.create_task(_check())
        app.on_startup.append(_agents_boot_check)

    # ── Pre-fetch cloud-model pricing from OpenRouter ──────────────────────
    # So the first dispatch doesn't stall waiting on a cold catalogue
    # fetch. Best-effort — if OpenRouter is unreachable at boot the
    # cost tracker silently records rows with pricing_known=False and
    # the UI surfaces "N requests with unknown pricing".
    try:
        from gateway import pricing as _pricing
        import asyncio as _asyncio_local
        await _asyncio_local.to_thread(_pricing.ensure_loaded)
    except Exception as exc:
        logger.debug("startup pricing fetch skipped: %s", exc)

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
        _os.environ["LOGOS_GATEWAY_MCP"] = "1"

        # Register the in-process 'logos' capabilities server. This has to
        # happen BEFORE `_mcp_svc.start()` so the logos server is visible
        # in the very first catalogue snapshot; the start() coroutine just
        # boots external stdio subprocesses and doesn't touch our injected
        # entries. The runner is passed directly from start_http_api so
        # tools can reach runner.adapters / runner.session_store / etc.
        try:
            from gateway.mcp_logos import register_logos_server
            _mcp_svc._logos_server = register_logos_server(runner, _mcp_svc)
            logger.info("MCP gateway: in-process 'logos' server registered")
        except Exception as _logos_err:
            logger.warning("MCP gateway: failed to register logos server: %s", _logos_err)

        asyncio.ensure_future(_mcp_svc.start())
        logger.info("MCP gateway service initialised (%d server(s) configured)",
                    len(_mcp_cfg.get("mcp_servers") or {}))

        # Rewire docker-deployed MCP servers on startup. Their config
        # lives in the mcp_servers DB table (not config.yaml) so the
        # startup load above doesn't see them, which means after a
        # gateway restart the DB row shows status=running but the
        # gateway's proxy has no idea the server exists. Re-register
        # each one into mcp_service._servers_cfg so /mcp/<name> works
        # and auto-granted toolsets resolve to real tools.
        async def _rewire_docker_mcp_servers():
            try:
                from gateway.auth import db as _mcp_db
                from gateway.mcp_management import _auto_wire_server
            except Exception as _imp_err:
                logger.warning("MCP rewire: import failed: %s", _imp_err)
                return
            try:
                _rows = _mcp_db.list_mcp_servers() or []
            except Exception as _list_err:
                logger.warning("MCP rewire: list_mcp_servers failed: %s", _list_err)
                return
            _rewired = 0
            for _row in _rows:
                if _row.get("deploy_mode") != "docker":
                    continue
                if _row.get("status") != "running" or not _row.get("url"):
                    continue
                try:
                    await _auto_wire_server(
                        app, _row["name"], _row["url"], _row.get("token") or "",
                    )
                    _rewired += 1
                except Exception as _wire_err:
                    logger.warning(
                        "MCP rewire: auto_wire_server(%s) failed: %s",
                        _row.get("name"), _wire_err,
                    )
            if _rewired:
                logger.info("MCP rewire: re-registered %d docker server(s)", _rewired)

        asyncio.ensure_future(_rewire_docker_mcp_servers())
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

    _load_souls()

    # ── Public routes ──────────────────────────────────────────────────────
    app.router.add_get("/health",        _handle_health)
    app.router.add_get("/healthz",       _handle_health)       # K8s liveness probe alias
    app.router.add_get("/health/ready",  _handle_health_ready)
    app.router.add_get("/favicon.ico",   _handle_favicon)      # public — Edge --app needs this before auth
    app.router.add_get("/api/hue",       _handle_hue)          # public — tray icon phase-lock
    app.router.add_get("/chat_logo.png", _handle_logo)
    app.router.add_get("/login",         _handle_login_page)

    # ── Auth routes (no cookie required) ───────────────────────────────────
    app.router.add_post("/auth/login",   handle_login)
    # LOG-25.7: self-service registration. Gated by
    # platform_settings.allow_registration at handler time; endpoint is
    # public so un-authed visitors can reach it when admins enable it.
    app.router.add_post("/auth/register", handle_register)
    app.router.add_get("/auth/registration-status", handle_registration_status)
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

    # ── Unified Services — inference settings + messaging (catalogue
    # + per-key tool-credential endpoints were removed 2026-04-21; see
    # admin_handlers.handle_agent_env_credentials_* for the replacement).
    app.router.add_post("/api/services/inference", _handle_services_inference)

    # ── Messaging platform integrations (Channels tab) ────────────────
    app.router.add_get("/api/services/messaging",          _handle_messaging_catalogue)
    app.router.add_post("/api/services/messaging/keys",    _handle_messaging_set_key)
    app.router.add_delete("/api/services/messaging/keys",  _handle_messaging_delete_key)
    app.router.add_post("/api/services/messaging/validate", _handle_messaging_validate)

    app.router.add_get("/setup",              _handle_setup_page)
    app.router.add_get("/api/setup/status",   _handle_setup_status)
    app.router.add_post("/api/setup/complete", _sh.handle_setup_complete)
    app.router.add_post("/api/setup/wipe",
        require_csrf(require_permission("manage_platform")(_handle_setup_wipe)))

    app.router.add_get("/",              _handle_app)
    app.router.add_get("/classic",       _handle_index)      # the v1-era page, until v2 covers it
    from gateway import dashboard_api as _dashboard_api
    _dashboard_api.register(app)
    app.router.add_static("/ui", str(Path(__file__).parent / "ui"), show_index=False)
    # Per-tab URL paths. Each one serves the same SPA (main_app.html);
    # Alpine reads window.location.pathname on init and pushes state on
    # tab changes so refresh keeps the user on the current page.
    for _tab_path in (
        "/agents",
        "/chats",
        "/activity",
        "/activity/events",
        "/activity/approvals",
        "/admin",
        "/admin/users",
        "/admin/audit",
    ):
        app.router.add_get(_tab_path, _handle_index)
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
    app.router.add_get("/api/world/state",   _handle_world_state)
    app.router.add_get(
        "/instances",
        require_permission("view_instances")(_handle_instances_get),
    )
    app.router.add_post("/instances",    _handle_instances_post)
    app.router.add_delete("/instances/{name}", _handle_instances_delete)
    # Instance management (memory, knowledge, config)
    app.router.add_get("/instances/{name}/memory",              _handle_instance_memory_get)
    app.router.add_put("/instances/{name}/memory",              _handle_instance_memory_put)
    app.router.add_post("/instances/{name}/fork",               _handle_instance_fork)
    app.router.add_get("/spawn-templates",         _handle_spawn_templates_get)
    app.router.add_put("/spawn-templates",         _handle_spawn_templates_put)
    app.router.add_delete("/spawn-templates/{id}", _handle_spawn_templates_delete)
    app.router.add_get("/status",        _handle_status)
    app.router.add_get("/sessions",      _handle_sessions)
    app.router.add_get("/api/platform-sessions", _handle_api_platform_sessions)
    app.router.add_get("/api/platform-sessions/{session_id}/messages", _handle_api_session_messages)
    app.router.add_delete("/api/platform-sessions/{session_id}", require_csrf(_handle_api_session_delete))
    app.router.add_post("/api/platform-sessions/{session_id}/restore",
                        require_csrf(_handle_api_session_restore))
    app.router.add_post("/chat",               _handle_chat)
    app.router.add_post("/chat/{task_id}/cancel", require_csrf(_handle_chat_cancel))
    app.router.add_post("/chat/transcribe",    require_csrf(_handle_transcribe))
    # /tools/configure removed 2026-04-21 — per-agent keys now go via
    # /admin/agents/<id>/env-credentials (registered below).
    app.router.add_route("OPTIONS", "/chat",   _handle_index)

    # ── Admin routes ───────────────────────────────────────────────────────
    _mm  = require_permission("manage_machines")
    _mu  = require_permission("manage_users")

    # LOG-25.7: admin-only platform-settings read/write. Backs the
    # registration toggles in Admin → Users. Gated on manage_users so
    # the permission tracks other admin-user operations in the same tab.
    app.router.add_get("/admin/platform-settings",
                       _mu(handle_platform_settings_get))
    app.router.add_patch("/admin/platform-settings",
                         _mu(require_csrf(handle_platform_settings_patch)))

    app.router.add_get("/admin/spawn-stats",   _mm(admin_handlers.handle_spawn_stats))
    app.router.add_get("/admin/costs",         _mm(admin_handlers.handle_costs))
    app.router.add_get("/admin/pricing/status",    _mm(admin_handlers.handle_pricing_status))
    app.router.add_post("/admin/pricing/refresh",  _mm(require_csrf(admin_handlers.handle_pricing_refresh)))
    # LOG-25.1: view_runs (not manage_machines). Users have view_runs in
    # RBAC and need the Runs tab to actually load; handle_dispatches_list
    # filters rows to the caller's own user_id for non-admin/operator so
    # they see their own runs but not other users' histories.
    app.router.add_get("/admin/dispatches",           require_permission("view_runs")(admin_handlers.handle_dispatches_list))
    # Any signed-in user may list agents (they need the list to chat); changing them stays admin-only.
    app.router.add_get("/admin/agents",              admin_handlers.handle_agents_list)
    app.router.add_post("/admin/agents",             _mm(require_csrf(admin_handlers.handle_agents_post)))
    app.router.add_patch("/admin/agents/{id}",       _mm(require_csrf(admin_handlers.handle_agents_patch)))
    app.router.add_delete("/admin/agents/{id}",      _mm(require_csrf(admin_handlers.handle_agents_delete)))
    # Per-agent Tools editor (the T pill dropdown). Read-only.
    app.router.add_get("/admin/agents/{id}/tools",
                       _mm(admin_handlers.handle_agent_tools_get))
    # Agent runtime logs — tails ~/.logos/agents/<name>/logs/agent.log.
    # Read-only, JSONL-ish response; paginated by ``?tail=N`` (default 200,
    # max 2000). Admin/operator only; regular users get 403. Used by the
    # "📋 Logs" modal on the Agents tab + compare pane so a user can see
    # what their agent was actually doing without shelling into the host.
    app.router.add_get("/admin/agents/{id}/logs",
                       _mm(admin_handlers.handle_agent_logs_get))
    # Per-agent lifetime-state endpoints backing the "🧠 Mind" modal:
    # memories (curated entries), sessions (list only — no transcripts
    # for now), activity (dispatches + cost rollup), files (on-disk
    # tree under ~/.logos/agents/<name>/).
    app.router.add_get("/admin/agents/{id}/memories",
                       _mm(admin_handlers.handle_agent_memories_get))
    app.router.add_get("/admin/agents/{id}/skills",
                       _mm(admin_handlers.handle_agent_skills_get))
    app.router.add_get("/admin/agents/{id}/sessions",
                       _mm(admin_handlers.handle_agent_sessions_get))
    app.router.add_get("/admin/agents/{id}/activity",
                       _mm(admin_handlers.handle_agent_activity_get))
    app.router.add_get("/admin/agents/{id}/files",
                       _mm(admin_handlers.handle_agent_files_get))
    # LOG-49: per-file download of agent-produced files (host-side).
    app.router.add_get("/admin/agents/{id}/files/download",
                       _mm(admin_handlers.handle_agent_file_download))
    # LOG-49.1/49.2/49.3: live sandbox filesystem browse + download.
    # Read-only introspection via `openshell sandbox exec` + `download`.
    app.router.add_get("/admin/agents/{id}/sandbox-files",
                       _mm(admin_handlers.handle_agent_sandbox_files_get))
    app.router.add_get("/admin/agents/{id}/sandbox-files/download",
                       _mm(admin_handlers.handle_agent_sandbox_file_download))
    # Per-agent channel credentials — Agent editor → Channels subtab.
    # Each agent owns its own messaging platform tokens; the adapter
    # lifecycle spawns one adapter instance per enabled row.
    app.router.add_get("/admin/agents/{id}/channels",
                       _mm(admin_handlers.handle_agent_channels_list))
    app.router.add_post("/admin/agents/{id}/channels",
                        _mm(require_csrf(admin_handlers.handle_agent_channels_post)))
    app.router.add_delete("/admin/agents/{id}/channels/{cred_id}",
                          _mm(require_csrf(admin_handlers.handle_agent_channels_delete)))
    app.router.add_post("/admin/agents/{id}/channels/{cred_id}/toggle",
                        _mm(require_csrf(admin_handlers.handle_agent_channels_toggle)))
    # Per-agent cloud-tool env credentials (plan-C, 2026-04-21). Same
    # shape as channel credentials but no platform/label — just
    # (env_var, value). Persists to agent_env_credentials + writes the
    # sandbox's .env in place. UI entry point is the /setup slash
    # command in chat.
    app.router.add_get("/admin/agents/{id}/env-credentials",
                       _mm(admin_handlers.handle_agent_env_credentials_list))
    app.router.add_post("/admin/agents/{id}/env-credentials",
                        _mm(require_csrf(admin_handlers.handle_agent_env_credentials_post)))
    app.router.add_delete("/admin/agents/{id}/env-credentials/{env_var}",
                          _mm(require_csrf(admin_handlers.handle_agent_env_credentials_delete)))
    # User ↔ platform identity links (Admin → Users or Config → Messaging)
    app.router.add_get("/api/admin/platform-links",
                       _mm(admin_handlers.handle_platform_links_list))
    app.router.add_post("/api/admin/platform-links",
                        _mm(require_csrf(admin_handlers.handle_platform_links_post)))
    app.router.add_delete("/api/admin/platform-links/{id}",
                          _mm(require_csrf(admin_handlers.handle_platform_links_delete)))
    # ── Action policies (behaviour enforcement) ────────────────────────────
    _vap = require_permission("view_approvals")
    _dap = require_permission("decide_approvals")

    # ── Souls admin CRUD ──────────────────────────────────────────────────
    _vsl = require_permission("view_souls")
    _msl = require_permission("manage_souls")
    app.router.add_get("/api/admin/souls",               _vsl(_handle_admin_souls_list))
    app.router.add_get("/api/admin/souls/{slug}",        _vsl(_handle_admin_soul_get))
    app.router.add_put("/api/admin/souls/{slug}",        _msl(require_csrf(_handle_admin_soul_put)))
    app.router.add_delete("/api/admin/souls/{slug}",     _msl(require_csrf(_handle_admin_soul_delete)))

    # ── Approval requests ──────────────────────────────────────────────────
    app.router.add_get("/approvals",              _vap(_handle_approvals_list))
    app.router.add_get("/approvals/{id}",         _vap(_handle_approvals_get))
    app.router.add_post("/approvals/{id}/approve", _dap(require_csrf(_handle_approvals_approve)))
    app.router.add_post("/approvals/{id}/reject",  _dap(require_csrf(_handle_approvals_reject)))

    # ── Evolution ──────────────────────────────────────────────────────
    # No dashboard UI since S1.10 (the Proposals tab is gone). The API
    # stays because skills/evolution/self-improvement still tells agents
    # to POST proposals here.
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

    # ── Update status/trigger (launcher file-based IPC) ─────────────────
    import json as _json
    import pathlib as _up_pathlib

    _HERMES_HOME_UPD = _up_pathlib.Path(os.environ.get("LOGOS_HOME") or str(_up_pathlib.Path.home() / ".logos"))
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

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

    # ── Workspace TTL cleanup ───────────────────────────────────────────────
    # Run once at startup to remove any workspaces left over from a previous
    # pod lifecycle, then schedule periodic sweeps.
    _ws_cleanup_interval_hours = float(
        os.environ.get("LOGOS_WORKSPACE_CLEANUP_INTERVAL_HOURS")
        or "1"
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
