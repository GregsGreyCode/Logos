"""JWT access tokens and opaque refresh tokens."""

import hashlib
import os
import secrets
import time
from typing import Optional

import jwt as pyjwt

ACCESS_TOKEN_TTL  = 15 * 60          # 15 minutes
REFRESH_TOKEN_TTL = 7 * 24 * 3600    # 7 days
ALGORITHM = "HS256"

# Set to true when running behind an HTTPS reverse proxy.
# Without HTTPS, Secure cookie flag is omitted so cookies work over HTTP.
_COOKIE_SECURE = os.environ.get("HERMES_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def _secret() -> str:
    s = os.environ.get("HERMES_JWT_SECRET", "")
    if not s:
        raise RuntimeError("HERMES_JWT_SECRET env var is not set")
    return s


def issue_access_token(user_id: str, email: str, role: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub":   user_id,
            "email": email,
            "role":  role,
            "iat":   now,
            "exp":   now + ACCESS_TOKEN_TTL,
            "jti":   f"tok_{secrets.token_hex(8)}",
        },
        _secret(),
        algorithm=ALGORITHM,
    )


# Sentinel returned by decode_access_token when the token is valid but expired.
# Distinct from None (missing/invalid) so callers can return 401 token_expired.
TOKEN_EXPIRED: dict = {"_token_expired": True}


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return pyjwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        return TOKEN_EXPIRED
    except pyjwt.PyJWTError:
        return None


def issue_refresh_token() -> tuple[str, str]:
    """Returns (raw_token, sha256_hash). Store hash, send raw in cookie."""
    raw    = secrets.token_hex(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _cookie(name: str, value: str, path: str, max_age: int, http_only: bool = True) -> str:
    parts = [f"{name}={value}"]
    if http_only:
        parts.append("HttpOnly")
    if _COOKIE_SECURE:
        parts.append("Secure")
    parts.extend([f"SameSite=Strict", f"Path={path}", f"Max-Age={max_age}"])
    return "; ".join(parts)


def set_auth_cookies(response, access_token: str, refresh_token: str) -> None:
    csrf = secrets.token_hex(16)
    response.headers.add("Set-Cookie", _cookie(
        "access_token", access_token, "/", ACCESS_TOKEN_TTL, http_only=True
    ))
    response.headers.add("Set-Cookie", _cookie(
        "refresh_token", refresh_token, "/auth/refresh", REFRESH_TOKEN_TTL, http_only=True
    ))
    # csrf_token is intentionally NOT HttpOnly — JS must read it
    response.headers.add("Set-Cookie", _cookie(
        "csrf_token", csrf, "/", REFRESH_TOKEN_TTL, http_only=False
    ))


def clear_auth_cookies(response) -> None:
    for name, path, http_only in [
        ("access_token",  "/",            True),
        ("refresh_token", "/auth/refresh", True),
        ("csrf_token",    "/",            False),  # must match creation flag so browser deletes it
    ]:
        response.headers.add("Set-Cookie", _cookie(name, "", path, 0, http_only=http_only))
