"""Auth route handlers: login, logout, refresh, me, users CRUD."""

import logging
import time

from aiohttp import web

import gateway.auth.db as auth_db
from gateway.auth.middleware import check_rate_limit
from gateway.auth.password import hash_password, verify_password, needs_rehash
from gateway.auth.rbac import get_permissions
from gateway.auth.tokens import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    clear_auth_cookies,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    set_auth_cookies,
)

logger = logging.getLogger(__name__)

_LOCKOUT_MAX_FAILURES = 10
_LOCKOUT_DURATION_MS  = 15 * 60 * 1000   # 15 minutes


def _user_public(user: dict) -> dict:
    """Strip password_hash and internal lockout fields for API responses."""
    return {
        "id":           user["id"],
        "email":        user["email"],
        "username":     user["username"],
        "role":         user["role"],
        "status":       user["status"],
        "display_name": user.get("display_name"),
        "created_at":   user["created_at"],
        "last_login":   user.get("last_login"),
        "policy_id":    user.get("policy_id"),
    }


def _settings_public(settings: dict | None) -> dict:
    if not settings:
        return {"ui_theme": "midnight", "default_soul": None, "default_model": None,
                "notification_telegram": False, "spawn_defaults": None}
    return {
        "ui_theme":              settings.get("ui_theme", "midnight"),
        "default_soul":          settings.get("default_soul"),
        "default_model":         settings.get("default_model"),
        "notification_telegram": bool(settings.get("notification_telegram", 0)),
        "spawn_defaults":        settings.get("spawn_defaults"),
    }


# ── Auth endpoints ─────────────────────────────────────────────────────────

async def handle_login(request: web.Request) -> web.Response:
    ip = request.remote or "unknown"
    if not check_rate_limit(ip, max_requests=30, window=60):
        return web.json_response({"error": "rate_limited"}, status=429)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    email    = (body.get("email") or "").lower().strip()
    password = body.get("password") or ""
    if not email or not password:
        return web.json_response({"error": "missing_fields"}, status=400)

    user = auth_db.get_user_by_email(email)

    # Constant-time path for unknown email (prevent enumeration)
    if user is None:
        hash_password("dummy_constant_time_comparison")
        return web.json_response({"error": "invalid_credentials"}, status=401)

    # Lockout check
    now_ms = int(time.time() * 1000)
    locked_until = user.get("locked_until") or 0
    if locked_until > now_ms:
        retry_after = (locked_until - now_ms) // 1000
        return web.json_response(
            {"error": "account_locked", "retry_after": retry_after}, status=423
        )

    # Password verification
    if not verify_password(user["password_hash"], password):
        new_count  = (user.get("failed_login_count") or 0) + 1
        new_locked = None
        if new_count >= _LOCKOUT_MAX_FAILURES:
            new_locked = now_ms + _LOCKOUT_DURATION_MS
        auth_db.record_failed_login(user["id"], new_count, new_locked)
        auth_db.write_audit_log(user["id"], "login_failed", ip_address=ip)
        return web.json_response({"error": "invalid_credentials"}, status=401)

    if user["status"] != "active":
        # Same error — don't leak account existence to brute-force scanners
        return web.json_response({"error": "invalid_credentials"}, status=401)

    # Rehash if Argon2 parameters have changed
    if needs_rehash(user["password_hash"]):
        auth_db.update_user(user["id"], password_hash=hash_password(password))

    # Issue tokens
    access_token             = issue_access_token(user["id"], user["email"], user["role"])
    raw_refresh, rtk_hash    = issue_refresh_token()
    auth_db.store_refresh_token(
        user["id"], rtk_hash,
        expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
        ip=ip,
        ua=request.headers.get("User-Agent"),
    )
    auth_db.update_last_login(user["id"])
    auth_db.write_audit_log(user["id"], "login", ip_address=ip)

    resp = web.json_response({"user": _user_public(user)})
    set_auth_cookies(resp, access_token, raw_refresh)
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    raw_refresh = request.cookies.get("refresh_token")
    if raw_refresh:
        try:
            auth_db.revoke_refresh_token(hash_refresh_token(raw_refresh))
        except Exception:
            pass

    user = request.get("current_user")
    if user:
        auth_db.write_audit_log(user.get("sub"), "logout", ip_address=request.remote)

    resp = web.Response(status=204)
    clear_auth_cookies(resp)
    return resp


async def handle_refresh(request: web.Request) -> web.Response:
    raw_refresh = request.cookies.get("refresh_token")
    if not raw_refresh:
        return web.json_response({"error": "no_refresh_token"}, status=401)

    token_hash = hash_refresh_token(raw_refresh)
    stored     = auth_db.get_refresh_token(token_hash)
    if stored is None:
        return web.json_response({"error": "invalid_refresh_token"}, status=401)

    now = int(time.time())
    if stored["expires_at"] < now:
        auth_db.revoke_refresh_token(token_hash)
        return web.json_response({"error": "refresh_token_expired"}, status=401)

    user = auth_db.get_user_by_id(stored["user_id"])
    if not user or user["status"] != "active":
        return web.json_response({"error": "invalid_refresh_token"}, status=401)

    # Rotate: revoke old token, issue new pair
    auth_db.revoke_refresh_token(token_hash)
    new_access               = issue_access_token(user["id"], user["email"], user["role"])
    raw_new_refresh, new_hash = issue_refresh_token()
    auth_db.store_refresh_token(
        user["id"], new_hash,
        expires_at=now + REFRESH_TOKEN_TTL,
        ip=request.remote,
        ua=request.headers.get("User-Agent"),
    )

    resp = web.json_response({"ok": True})
    set_auth_cookies(resp, new_access, raw_new_refresh)
    return resp


async def handle_me(request: web.Request) -> web.Response:
    user_payload = request["current_user"]
    user = auth_db.get_user_by_id(user_payload["sub"])
    if not user:
        return web.json_response({"error": "user_not_found"}, status=404)
    settings = auth_db.get_user_settings(user["id"])
    return web.json_response({
        "user":        _user_public(user),
        "settings":    _settings_public(settings),
        "permissions": get_permissions(user["role"]),
    })


# ── User management ─────────────────────────────────────────────────────────

async def handle_users_me_patch(request: web.Request) -> web.Response:
    user_payload = request["current_user"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    settings_fields = {}
    for f in ("default_soul", "default_model", "ui_theme", "notification_telegram", "spawn_defaults"):
        if f in body:
            settings_fields[f] = body[f]
    if settings_fields:
        auth_db.update_user_settings(user_payload["sub"], **settings_fields)

    if "display_name" in body:
        auth_db.update_user(user_payload["sub"], display_name=body["display_name"])

    if "new_password" in body:
        current_password = body.get("current_password", "")
        new_password     = body.get("new_password", "")
        if not current_password or not new_password:
            return web.json_response({"error": "current_password and new_password required"}, status=400)
        if len(new_password) < 8:
            return web.json_response({"error": "password_too_short"}, status=400)
        user_row = auth_db.get_user_by_id(user_payload["sub"])
        if not user_row or not verify_password(user_row["password_hash"], current_password):
            return web.json_response({"error": "invalid_current_password"}, status=403)
        auth_db.update_user(user_payload["sub"], password_hash=hash_password(new_password))

    user     = auth_db.get_user_by_id(user_payload["sub"])
    settings = auth_db.get_user_settings(user_payload["sub"])
    return web.json_response({"user": _user_public(user), "settings": _settings_public(settings)})


async def handle_users_list(request: web.Request) -> web.Response:
    page   = int(request.rel_url.query.get("page", 1))
    limit  = min(int(request.rel_url.query.get("limit", 20)), 100)
    role   = request.rel_url.query.get("role") or None
    status = request.rel_url.query.get("status") or None
    users, total = auth_db.list_users(page=page, limit=limit, role=role, status=status)
    return web.json_response({
        "users": [_user_public(u) for u in users],
        "total": total,
        "page":  page,
        "limit": limit,
    })


async def handle_users_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    required = ("email", "username", "password")
    if not all(body.get(k) for k in required):
        return web.json_response({"error": "missing_fields", "required": list(required)}, status=400)

    if auth_db.get_user_by_email(body["email"]):
        return web.json_response({"error": "email_exists"}, status=409)

    user = auth_db.create_user(
        email=body["email"],
        username=body["username"],
        password_hash=hash_password(body["password"]),
        role=body.get("role", "user"),
        display_name=body.get("display_name"),
    )
    auth_db.write_audit_log(
        request["current_user"]["sub"], "create_user",
        target_type="user", target_id=user["id"],
        metadata={"email": user["email"], "role": user["role"]},
        ip_address=request.remote,
    )
    return web.json_response({"user": _user_public(user)}, status=201)


async def handle_users_patch(request: web.Request) -> web.Response:
    target_id = request.match_info["id"]
    caller    = request["current_user"]

    target = auth_db.get_user_by_id(target_id)
    if not target:
        raise web.HTTPNotFound(reason="user_not_found")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    updates = {f: body[f] for f in ("role", "status", "display_name") if f in body}
    if updates:
        auth_db.update_user(target_id, **updates)
        auth_db.write_audit_log(
            caller["sub"], "update_user",
            target_type="user", target_id=target_id,
            metadata=updates, ip_address=request.remote,
        )

    if "new_password" in body:
        new_pw = body.get("new_password", "")
        if not new_pw or len(new_pw) < 8:
            return web.json_response({"error": "password_too_short"}, status=400)
        auth_db.update_user(target_id, password_hash=hash_password(new_pw))
        auth_db.revoke_all_user_tokens(target_id)
        auth_db.write_audit_log(
            caller["sub"], "admin_set_password",
            target_type="user", target_id=target_id,
            metadata={"email": target["email"]},
            ip_address=request.remote,
        )

    return web.json_response({"user": _user_public(auth_db.get_user_by_id(target_id))})


async def handle_audit_logs(request: web.Request) -> web.Response:
    page    = int(request.rel_url.query.get("page", 1))
    limit   = min(int(request.rel_url.query.get("limit", 50)), 200)
    user_id = request.rel_url.query.get("user_id") or None
    action  = request.rel_url.query.get("action") or None
    logs, total = auth_db.list_audit_logs(page=page, limit=limit, user_id=user_id, action=action)
    return web.json_response({"logs": logs, "total": total, "page": page, "limit": limit})
