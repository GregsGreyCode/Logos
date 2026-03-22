"""aiohttp middleware and decorators for auth and RBAC."""

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
    "/login",
    "/auth/login",
    "/auth/logout",
    "/auth/refresh",
    "/chat_logo.png",
    "/api/setup/status",
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
    internal_token = os.environ.get("HERMES_INTERNAL_TOKEN", "")
    auth_header = request.headers.get("Authorization", "")
    if internal_token and auth_header == f"Bearer {internal_token}":
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

    if path in _PUBLIC_PATHS or path.startswith("/static/"):
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

    request["current_user"] = user
    return await handler(request)


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
            internal_token = os.environ.get("HERMES_INTERNAL_TOKEN", "")
            auth_header = request.headers.get("Authorization", "")
            if internal_token and auth_header == f"Bearer {internal_token}":
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
