"""First-run setup: create the admin account, optionally a second account.

v1 shipped a seven-step wizard here (server scan, model benchmark, route
provisioning, sandbox spawn, tool setup, messaging). v2 agents are created
by ``scripts/logos-agent`` from ``agents/<name>.yaml``, so the only thing a
fresh install still has to ask is who logs in.

    POST /api/setup/complete
        {"name": ..., "email": ..., "password": ...,
         "second": {"name": ..., "email": ..., "password": ...}}   # optional

Creates the admin (or, when ``LOGOS_ADMIN_EMAIL`` seeding already made one,
replaces that account's credentials), creates the optional second user, flips
the ``setup_completed`` flag, and logs the admin in via cookies on the response.
Refused with 409 once setup has completed.

Setup is the one moment an install has no accounts, so it is guarded: the
request must come from this machine, or carry the one-time setup token the
gateway writes to ``LOGOS_HOME/.setup_token`` and prints at startup
(``logos gateway setup-link`` shows the link again). There is no default
admin account.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Optional

from aiohttp import web

import gateway.auth.db as auth_db
from gateway.auth.password import hash_password
from gateway.auth.tokens import (
    REFRESH_TOKEN_TTL,
    issue_access_token,
    issue_refresh_token,
    set_auth_cookies,
)

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
SETUP_TOKEN_FILE = ".setup_token"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_token_path: Optional[Path] = None


def ensure_setup_token(home: Path) -> Optional[str]:
    """Return the one-time setup token, creating it if setup has not run yet.
    Returns ``None`` (and removes any leftover token) once setup is complete."""
    global _token_path
    _token_path = Path(home) / SETUP_TOKEN_FILE
    if auth_db.is_setup_completed():
        _token_path.unlink(missing_ok=True)
        return None
    if _token_path.exists():
        token = _token_path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    _token_path.parent.mkdir(parents=True, exist_ok=True)
    _token_path.write_text(token)
    _token_path.chmod(0o600)
    return token


def _is_loopback(remote: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address((remote or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def setup_allowed(remote: Optional[str], offered: object) -> bool:
    """Setup may be completed from this machine, or by whoever holds the token."""
    if _is_loopback(remote):
        return True
    expected = _token_path.read_text().strip() if _token_path and _token_path.exists() else ""
    return bool(expected) and isinstance(offered, str) and hmac.compare_digest(offered.strip(), expected)


class SetupError(Exception):
    """A request the person can fix; carries the JSON error + HTTP status."""

    def __init__(self, error: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.status = status


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9_]+", "-", ascii_text.lower()).strip("-")[:64]


def username_from_name(name: str, email: str) -> str:
    """Derive a login username from the display name ("Béré D." -> "bere-d").

    Usernames are restricted to ASCII letters, digits, ``-`` and ``_`` (the
    same rule as /auth/register). Falls back to the email's local part when
    the name has no usable characters.
    """
    return _slug(name) or _slug(email.split("@", 1)[0]) or "user"


def _parse_account(raw: object, label: str) -> dict:
    """Validate one {name, email, password} object. Raises SetupError."""
    if not isinstance(raw, dict):
        raise SetupError("invalid_account", f"{label}: expected an object.")
    name = str(raw.get("name") or "").strip()
    email = str(raw.get("email") or "").strip().lower()
    password = raw.get("password")
    if not isinstance(password, str):
        password = ""
    if not name or len(name) > 100:
        raise SetupError("invalid_name", f"{label}: a name is required.")
    if not _EMAIL.match(email) or len(email) > 254:
        raise SetupError("invalid_email", f"{label}: a valid email address is required.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SetupError(
            "password_too_short",
            f"{label}: the password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    return {
        "name": name,
        "email": email,
        "password": password,
        "username": username_from_name(name, email),
    }


def _second_account(raw: object) -> Optional[dict]:
    """The second account is optional: absent, null or all-blank means none."""
    if raw is None:
        return None
    if isinstance(raw, dict) and not any(
        str(raw.get(k) or "").strip() for k in ("name", "email", "password")
    ):
        return None
    return _parse_account(raw, "Second account")


def _check_free(account: dict, label: str, ignore_user_id: Optional[str]) -> None:
    """Raise SetupError(409) if the email or username belongs to someone else."""
    for field, lookup in (
        ("email", auth_db.get_user_by_email),
        ("username", auth_db.get_user_by_username),
    ):
        existing = lookup(account[field])
        if existing and existing["id"] != ignore_user_id:
            raise SetupError(
                f"{field}_taken",
                f"{label}: {field} '{account[field]}' is already in use.",
                status=409,
            )


def complete_setup(admin_raw: object, second_raw: object = None) -> tuple[dict, Optional[dict]]:
    """Create the accounts and mark setup completed.

    Returns ``(admin_row, second_row_or_None)``. Everything is validated
    before anything is written, so a rejected second account never leaves a
    half-finished install behind.
    """
    if auth_db.is_setup_completed():
        raise SetupError(
            "setup_already_completed",
            "Setup has already been completed. Sign in instead.",
            status=409,
        )

    admin = _parse_account(admin_raw, "Admin account")
    second = _second_account(second_raw)
    if second:
        if second["email"] == admin["email"]:
            raise SetupError(
                "duplicate_email", "The two accounts need different email addresses."
            )
        if second["username"] == admin["username"]:
            raise SetupError("duplicate_name", "The two accounts need different names.")

    # Startup seeding (LOGOS_ADMIN_EMAIL, or the admin@example.com default)
    # may already have created an admin. Setup takes that account over rather
    # than leaving a second admin with a known default password behind.
    existing_admin = auth_db.get_primary_admin()
    existing_id = existing_admin["id"] if existing_admin else None
    _check_free(admin, "Admin account", ignore_user_id=existing_id)
    if second:
        _check_free(second, "Second account", ignore_user_id=None)

    if existing_admin:
        admin_user = auth_db.update_user(
            existing_id,
            email=admin["email"],
            username=admin["username"],
            display_name=admin["name"],
            password_hash=hash_password(admin["password"]),
            status="active",
        )
        # Sessions minted for the seeded credentials must not outlive them.
        auth_db.revoke_all_user_tokens(existing_id)
    else:
        admin_user = auth_db.create_user(
            email=admin["email"],
            username=admin["username"],
            password_hash=hash_password(admin["password"]),
            role="admin",
            display_name=admin["name"],
        )

    second_user = None
    if second:
        second_user = auth_db.create_user(
            email=second["email"],
            username=second["username"],
            password_hash=hash_password(second["password"]),
            role="user",
            display_name=second["name"],
        )

    auth_db.mark_setup_completed()
    if _token_path:
        _token_path.unlink(missing_ok=True)
    return admin_user, second_user


async def handle_setup_complete(request: web.Request) -> web.Response:
    """POST /api/setup/complete — create the account(s) and log the admin in."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_json"}, status=400)

    if not auth_db.is_setup_completed() and not setup_allowed(
            request.remote, body.get("token") or request.query.get("token")):
        return web.json_response(
            {"error": "setup_token_required",
             "detail": "Open the setup link printed when the server started "
                       "(or run `logos gateway setup-link` on the server)."}, status=403)

    try:
        admin_user, second_user = complete_setup(body, body.get("second"))
    except SetupError as err:
        return web.json_response(
            {"error": err.error, "detail": err.detail}, status=err.status
        )

    user_id = admin_user["id"]
    auth_db.write_audit_log(
        user_id,
        "setup_completed",
        metadata={"second_account": second_user is not None},
        ip_address=request.remote,
    )
    logger.info("setup completed by %s", user_id)

    access_token = issue_access_token(user_id, admin_user["email"], admin_user["role"])
    raw_refresh, refresh_hash = issue_refresh_token()
    auth_db.store_refresh_token(
        user_id,
        refresh_hash,
        expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
        ip=request.remote,
        ua=request.headers.get("User-Agent"),
    )
    auth_db.update_last_login(user_id)

    def _public(user: dict) -> dict:
        return {
            "id": user["id"],
            "email": user["email"],
            "username": user["username"],
            "display_name": user.get("display_name"),
            "role": user["role"],
        }

    resp = web.json_response(
        {
            "ok": True,
            "user": _public(admin_user),
            "second_user": _public(second_user) if second_user else None,
        },
        headers={"Cache-Control": "no-store"},
    )
    set_auth_cookies(resp, access_token, raw_refresh)
    return resp
