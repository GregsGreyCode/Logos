"""aiohttp middleware and decorators for auth and RBAC."""

import hmac
import logging
import os
import time
from collections import defaultdict
from functools import wraps
from typing import Optional

from aiohttp import web

from gateway.auth.tokens import decode_access_token, TOKEN_EXPIRED
from gateway.auth.rbac import has_permission

logger = logging.getLogger(__name__)

# Paths that never require authentication
_PUBLIC_PATHS = frozenset({
    "/health",
    "/health/ready",
    "/api/hue",
    "/login",
    "/auth/login",
    "/auth/register",  # LOG-25.7: self-service registration, gated by
                       # platform_settings.allow_registration at handler
                       # time (default off). Public so un-authed visitors
                       # can create accounts when admins flip it on.
    "/auth/registration-status",  # public read-only probe used by
                                  # login.html to decide whether to
                                  # render a "Create an account" link.
    "/auth/logout",
    "/auth/refresh",
    "/chat_logo.png",
    "/favicon.ico",
    "/sw.js",          # F37: the service worker (code, no data); the browser fetches it to update
    "/metrics",        # Prometheus: off unless switched on in Settings; checks its own bearer token (gateway/metrics.py)
    "/api/v2/voice/converse",  # the house speaker (Home Assistant); checks its own token (gateway/voice_converse.py)
    "/api/v2/alert-text",  # an agent's first words for a phone alert; checks the agent's own token
                           # (gateway/alert_text.py), and the sandbox may reach only this path
    "/api/v2/speaker/say",  # an agent speaking up on the house speaker; checks the agent's own token
                            # (gateway/speaker_say.py), and the sandbox may reach only this path
    "/api/v2/agent-rename/ask",  # an agent asking for a new name (its person confirms); checks the
                                 # agent's own token (gateway/agent_rename.py); the sandbox reaches only this
    # "/ws/worker" removed — Plan A refactor (TASKS.md #24) deleted the
    # reverse-connection WebSocket endpoint. Workers are now subprocess-
    # per-sandbox, launched via `openshell sandbox exec` from the host
    # gateway, so no sandbox traffic ever hits an auth-gated URL.
    "/invite",             # F38: the page an invited person opens; the secret travels in a JSON body
    "/auth/invite/check",  # (never in an address) and is checked, single use, by gateway/people_api.py
    "/auth/invite/accept",
    "/api/setup/status",
    "/api/setup/complete",
    "/setup",
})

# Simple in-process rate limiter keyed by IP
_rate_store: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(ip: str, max_requests: int = 30, window: int = 60) -> bool:
    """Returns True if the request is allowed, False if rate limited."""
    now = time.time()
    hits = _rate_store[ip]
    _rate_store[ip] = [t for t in hits if now - t < window]
    if len(_rate_store[ip]) >= max_requests:
        return False
    _rate_store[ip].append(now)
    return True


def get_user_from_request(request: web.Request) -> Optional[dict]:
    """Extract authenticated user payload from cookie JWT or service Bearer token."""
    # M2M / service bypass (K8s health probes, internal services)
    internal_token = (
        os.environ.get("LOGOS_INTERNAL_TOKEN")
        or ""
    )
    auth_header = request.headers.get("Authorization", "")
    if internal_token and hmac.compare_digest(auth_header.encode(), f"Bearer {internal_token}".encode()):
        return {"sub": "system", "email": "system@internal", "role": "admin"}

    token = request.cookies.get("access_token")
    if not token:
        return None
    result = decode_access_token(token)
    # Propagate the expired sentinel so auth_middleware can return a specific error
    return result


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Global middleware: public paths pass through; everything else requires a valid session."""
    path = request.path

    # /ui/ holds the dashboard's own scripts and styles: code, no data.
    if path in _PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/ui/"):
        return await handler(request)

    # MCP proxy handles its own auth (bearer token + session grant check).
    # Skip cookie-redirect logic so agent HTTP clients don't get a 302.
    if path.startswith("/mcp/"):
        return await handler(request)

    # The Matrix pass-through checks the dashboard session itself: an expired one must be answered
    # 503 (try again), never 401, or the browser's Matrix client signs its device out (M1).
    if path.startswith("/_matrix/"):
        return await handler(request)

    user = get_user_from_request(request)
    if user is TOKEN_EXPIRED:
        # Token present but expired — client should refresh, not re-authenticate
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(
            text='{"error":"token_expired"}',
            content_type="application/json",
        )
    if user is None:
        # Browser (HTML) request → redirect to login page
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(
            text='{"error":"unauthenticated"}',
            content_type="application/json",
        )

    if not _still_active(user):
        # F38: someone removed (suspended) or not yet set up cannot go on using a session they had.
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(text='{"error":"unauthenticated"}', content_type="application/json")
    request["current_user"] = user
    return await handler(request)


def _still_active(user: dict) -> bool:
    """The account behind a session is still active. Sessions last up to 15 minutes after a refresh, so
    without this a person removed from the People screen could keep using theirs until it ran out."""
    sub = user.get("sub")
    if not sub or sub == "system":
        return True
    try:
        import gateway.auth.db as auth_db
        row = auth_db.get_user_by_id(sub)
    except RuntimeError:              # no database (some tests): nothing to check against
        return True
    return bool(row) and row.get("status") == "active"


def require_permission(permission: str):
    """Decorator factory: 403 if the authenticated user lacks the specified permission."""
    def decorator(handler):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            user = request.get("current_user") or get_user_from_request(request)
            if user is None:
                raise web.HTTPUnauthorized(
                    text='{"error":"unauthenticated"}',
                    content_type="application/json",
                )
            request["current_user"] = user
            if not has_permission(user.get("role", "viewer"), permission):
                raise web.HTTPForbidden(
                    text=f'{{"error":"forbidden","requires":"{permission}"}}',
                    content_type="application/json",
                )
            return await handler(request)
        return wrapper
    return decorator


def require_csrf(handler):
    """Decorator: enforce CSRF double-submit cookie check on state-changing methods."""
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            # Service Bearer token bypasses CSRF (machine-to-machine)
            internal_token = (
                os.environ.get("LOGOS_INTERNAL_TOKEN")
                or ""
            )
            auth_header = request.headers.get("Authorization", "")
            if internal_token and hmac.compare_digest(auth_header.encode(), f"Bearer {internal_token}".encode()):
                return await handler(request)

            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("X-CSRF-Token")
            if not cookie_token or cookie_token != header_token:
                raise web.HTTPForbidden(
                    text='{"error":"csrf_invalid"}',
                    content_type="application/json",
                )
        return await handler(request)
    return wrapper
