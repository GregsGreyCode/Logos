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
import logging
import os
import pathlib
import time
from pathlib import Path
from typing import Any


from aiohttp import web

from gateway.auth import db as auth_db
from gateway.auth.handlers import (
    handle_login,
    handle_logout,
    handle_me,
    handle_refresh,
    handle_register,
    handle_registration_status,
    handle_users_me_patch,
    handle_users_post,
)
from gateway.auth.middleware import auth_middleware, check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password

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
    get_backend as _get_sandbox_backend,
)

# Chat / spawn requests answer with this until E2 lands a sandbox backend.
_NO_AGENTS_RUNNING = {
    "error": "no_agents_running",
    "message": "No agents are running yet: the v2 sandbox backend arrives in epic E2.",
}


def _no_agents_running_response() -> web.Response:
    return web.json_response(_NO_AGENTS_RUNNING, status=503)




# When running as a PyInstaller frozen executable, __file__ points inside the
# zip archive and "html/" must be resolved via sys._MEIPASS instead.
import sys as _sys
_html_dir = (
    Path(_sys._MEIPASS) / "gateway" / "html"
    if getattr(_sys, "frozen", False)
    else Path(__file__).parent / "html"
)


# mtime-cached HTML loader. Previously `app.html` / `login.html` /
# `setup.html` were read once at import time and frozen into module-
# level strings, which meant a running gateway never picked up HTML
# edits — every "my change isn't showing up" ticket traced back to the
# same stale snapshot. Re-reading on mtime change is effectively free
# (the pages are small and read at most once per page navigation) and
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
_read_html("login.html")
_read_html("setup.html")


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


# ── First-run setup ──────────────────────────────────────────────────────────

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


async def _handle_old_page(request: web.Request) -> web.Response:
    """The old v1 dashboard's addresses (/classic, /agents, ...): it was removed, so go to the dashboard."""
    raise web.HTTPFound("/")


async def _handle_options(request: web.Request) -> web.Response:
    """CORS preflight for /chat: nothing to add beyond what the middleware sets."""
    return web.Response(status=204)


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


async def _handle_health(request: web.Request) -> web.Response:
    uptime = int(time.time() - _start_time)
    from gateway.auth.db import is_setup_completed as _isc
    return web.json_response({
        "status": "ok",
        "product": "logos",
        "setup_completed": _isc(),
        "uptime_s": uptime,
    })


async def _handle_health_ready(request: web.Request) -> web.Response:
    """Deep readiness check: verifies the auth DB is operational.

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

    status = 200 if ok else 503
    return web.json_response(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status=status,
    )


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


# ── Web chat with an agent ────────────────────────────────────────────────────

async def _handle_chat(request: web.Request) -> web.StreamResponse:
    # Signed-in users only (the auth middleware); rate-limited per IP as well.
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
        # A local model that was unloaded is loaded here with the agent's own
        # settings, and the person is told why the first reply is slow.
        model_off = False
        async for payload in _agent_chat.wake_model(
                _definition.get("model") or {}, links.runtime.runner.http,
                backup=await asyncio.to_thread(_agent_chat.has_backup, _definition)):
            await resp.write(_agent_chat.sse(payload))
            model_off = model_off or payload.get("error_type") == _agent_chat.MODEL_OFF
        if model_off:
            # Nothing was handed to the agent, so there is no run to wait for.
            logger.info("chat: the model server is off; %s was not asked", agent_name)
            beat.cancel()
            await resp.write_eof()
            return resp
        async for payload in _agent_chat.stream_run(
            client, message, session_id=hermes_session,
            model=_agent_config.get("model") or "", on_started=_remember,
        ):
            await resp.write(_agent_chat.sse(payload))
    except ConnectionResetError:
        # The browser went away. The run carries on inside the agent and its
        # result is kept in the agent's own session history, where the chat
        # screen looks for it. Not an error, so nothing is raised.
        logger.info("chat: client left during a run for %s", agent_name)
        return resp
    except asyncio.CancelledError:
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

    # The dashboard's scripts and styles: the browser must ask (cheaply, by ETag) whether its copy is
    # still current. Without this a phone browser kept some of yesterday's files after a deploy, mixed
    # them with new ones, and the dashboard did not start (Greg's phone, 21 Nov).
    async def _ui_revalidate(request, response):
        if request.path.startswith("/ui/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
    app.on_response_prepare.append(_ui_revalidate)
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

        async def _model_metrics(_app: web.Application) -> None:
            # Model status and spend, pushed to an OpenTelemetry collector once a minute
            # (gateway/model_metrics.py). Off unless LOGOS_OTLP_ENDPOINT is set.
            from gateway import dashboard_api as _dash, model_metrics as _metrics
            endpoint = os.environ.get(_metrics.ENV, "").strip()
            if not endpoint:
                return

            async def _loop() -> None:
                failing = False
                while True:
                    try:
                        overview = await _dash.models_overview(_app, _agent_runtime)
                        body = _metrics.payload(_metrics.points(overview))
                        await asyncio.to_thread(_metrics.push, _agent_runtime.runner.http, endpoint, body)
                        if failing:
                            logger.info("model metrics: the collector is accepting data again")
                        failing = False
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not failing:           # say it once, not every minute
                            logger.warning("model metrics: could not send to the collector: %s", exc)
                        failing = True
                    await asyncio.sleep(_metrics.INTERVAL_SECONDS)
            _app["model_metrics"] = asyncio.create_task(_loop())
            logger.info("model metrics: sending to the collector every %s s", _metrics.INTERVAL_SECONDS)
        app.on_startup.append(_model_metrics)

    # ── Public routes ──────────────────────────────────────────────────────
    app.router.add_get("/health",        _handle_health)
    app.router.add_get("/healthz",       _handle_health)
    app.router.add_get("/health/ready",  _handle_health_ready)
    app.router.add_get("/favicon.ico",   _handle_favicon)      # public: browsers ask before signing in
    app.router.add_get("/login",         _handle_login_page)

    # ── Sign-in (no cookie required) ───────────────────────────────────────
    app.router.add_post("/auth/login",   handle_login)
    # Self-service sign-up, gated by platform_settings.allow_registration in the handler.
    app.router.add_post("/auth/register", handle_register)
    app.router.add_get("/auth/registration-status", handle_registration_status)
    app.router.add_post("/auth/logout",  handle_logout)
    app.router.add_post("/auth/refresh", handle_refresh)

    # ── First-run setup ────────────────────────────────────────────────────
    from gateway import setup_handlers as _sh
    app.router.add_get("/setup",              _handle_setup_page)
    app.router.add_get("/api/setup/status",   _handle_setup_status)
    app.router.add_post("/api/setup/complete", _sh.handle_setup_complete)
    app.router.add_post("/api/setup/wipe",
        require_csrf(require_permission("manage_platform")(_handle_setup_wipe)))

    # ── The dashboard (gateway/html/app.html + gateway/ui/) ────────────────
    app.router.add_get("/",              _handle_app)
    from gateway import dashboard_api as _dashboard_api
    _dashboard_api.register(app)
    from gateway import network_api as _network_api     # E6: sites an agent's sandbox refused, for its person to allow
    _network_api.register(app)
    from gateway import people_api as _people_api          # F38: People › Add a person, invitations
    _people_api.register(app)
    from gateway import push as _push                   # F37: phone notifications; its /_matrix/push route goes first
    _push.register(app, _hermes_home)
    from gateway import activity as _activity           # what each agent did today (counts only)
    app["activity"] = _activity.Activity(_hermes_home)
    from gateway import alert_text as _alert_text       # what an agent said, for its phone alert
    _alert_text.register(app)
    from gateway import models_catalogue as _models_catalogue  # the agents' copy of models.dev's catalogue (read only)
    _models_catalogue.register(app)
    from gateway import voice_converse as _voice_converse  # Voice V1: the house speaker, through Home Assistant
    _voice_converse.register(app)
    from gateway import speaker_voice as _speaker_voice  # Voice V1.2: each agent in its own voice on the speaker
    _speaker_voice.register(app)
    from gateway import speaker_say as _speaker_say      # Voice V3.1: agents speak up on the speaker
    _speaker_say.register(app)
    from gateway import voice_id as _voice_id            # Voice VO.2: who is speaking; Settings › Your voice
    _voice_id.register(app)
    from gateway import wake_host as _wake_host  # Voice VW.1: the speaker wakes to each agent's name
    _wake_host.register(app)
    from gateway import agent_settings_api as _agent_settings_api  # E6: an agent's personality, model, chat settings
    _agent_settings_api.register(app)
    from gateway import agent_rename as _agent_rename    # an agent asks for a new name; its person confirms
    _agent_rename.register(app)
    from gateway import household_words_api as _household_words  # Settings › Household words, for speech-to-text
    _household_words.register(app)
    from gateway import widgets as _widgets             # F42: homelab widgets on Home
    _widgets.register(app)
    from gateway import metrics as _metrics             # F42: Prometheus /metrics, off until switched on
    _metrics.register(app)
    from gateway import local_cost as _local_cost       # Models › Local vs cloud: the usage and energy ledger
    _local_cost.register(app)
    from gateway import matrix_proxy as _matrix_proxy   # M1: the browser's Matrix device reaches the server here
    _matrix_proxy.register(app)
    from gateway import updates as _updates
    _updates.register(app)
    from gateway import mail_connect as _mail_connect   # F40: connect your own mailbox from Settings › Email
    _mail_connect.register(app)
    app.router.add_static("/ui", str(Path(__file__).parent / "ui"), show_index=False)

    async def _handle_sw(request):
        # F37: the service worker, at the site's root so it may handle the whole app.
        return web.FileResponse(Path(__file__).parent / "ui" / "sw.js",
                                headers={"Content-Type": "text/javascript", "Cache-Control": "no-cache"})
    app.router.add_get("/sw.js", _handle_sw)
    # Addresses of the old v1 dashboard (removed in S6.8): bookmarks lead to the dashboard.
    for _tab_path in (
        "/classic",
        "/agents",
        "/chats",
        "/activity",
        "/activity/events",
        "/activity/approvals",
        "/admin",
        "/admin/users",
        "/admin/audit",
    ):
        app.router.add_get(_tab_path, _handle_old_page)

    # ── Accounts (the People screen) ───────────────────────────────────────
    app.router.add_get("/auth/me",       handle_me)
    app.router.add_get("/users/me",      handle_me)
    app.router.add_patch("/users/me",    handle_users_me_patch)
    app.router.add_post(
        "/users",
        require_permission("manage_users")(require_csrf(handle_users_post)),
    )

    # ── Web chat with an agent ─────────────────────────────────────────────
    app.router.add_post("/chat",               _handle_chat)
    app.router.add_post("/chat/{task_id}/cancel", require_csrf(_handle_chat_cancel))
    app.router.add_route("OPTIONS", "/chat",   _handle_options)

    # Serve static assets (logo, icons, the web app manifest)
    import pathlib as _pathlib
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: __file__ doesn't resolve relative to source tree
        _static_dir = _pathlib.Path(_sys._MEIPASS) / "assets"
    else:
        _static_dir = _pathlib.Path(__file__).parent.parent / "assets"
    if _static_dir.exists():
        import mimetypes as _mimetypes
        _mimetypes.add_type("application/manifest+json", ".webmanifest")   # Add to Home screen
        app.router.add_static("/static", str(_static_dir), show_index=False)

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP API listening on port %d", port)

