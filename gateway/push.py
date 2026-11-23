"""Phone notifications (F37): the dashboard is the Matrix push gateway for its own web app.

How it fits together (docs/push.md):

1. On a phone, Settings › Notifications turns them on for that device. The browser asks permission,
   subscribes to Web Push with this server's public key, and posts the subscription here
   (``POST /api/v2/push/subscribe``). Logos keeps it under a random ``pushkey``.
2. The browser then registers that pushkey with the person's Matrix account as an HTTP pusher whose
   gateway is this dashboard on the Docker bridge (``/_matrix/push/v1/notify``).
3. When a message should notify the person (Matrix's push rules: their DMs, group messages,
   mentions; never their own), the Matrix server posts here. Logos sends the phone an encrypted Web
   Push (RFC 8291, aes128gcm; VAPID, RFC 8292) saying who wrote. The content is end-to-end encrypted
   and is not read here. For an agent's reply, the agent itself hands Logos its first words as it
   sends it (gateway/alert_text.py), and the alert shows them; a person's message stays "New message".
   A push without an event (only the room's unread count, after it was read) sends nothing: the page
   clears a room's notification when the room is opened.

The VAPID key is made once and kept, owner-only, beside auth.db. A subscription the push service
says is gone (404/410) is forgotten and its pushkey returned to Matrix as rejected.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

APP_ID = "logos.web"
NOTIFY_PATH = "/_matrix/push/v1/notify"
VAPID_SUBJECT = "mailto:push@logos.invalid"      # no person's address leaves this machine
TTL = 12 * 3600                                  # a phone off for longer misses the push, not the message
RECORD_SIZE = 4096
# The Matrix server reaches the dashboard from the Docker bridge; nothing else may post pushes.
TRUSTED = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "172.16.0.0/12")]
TIMEOUT = aiohttp.ClientTimeout(total=15)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── where things are kept ────────────────────────────────────────────────────────────────────

class Store:
    """Subscriptions (pushkey -> person, endpoint, keys) and the VAPID key, in ``folder``."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.db = self.folder / "push.db"
        self.key_file = self.folder / "push-vapid.pem"
        self.folder.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS subs (pushkey TEXT PRIMARY KEY, username TEXT NOT NULL, "
                      "endpoint TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, device TEXT, created INTEGER)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)

    def vapid_key(self) -> ec.EllipticCurvePrivateKey:
        if self.key_file.exists():
            return serialization.load_pem_private_key(self.key_file.read_bytes(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return key

    def public_key(self) -> str:
        return b64u(raw_public(self.vapid_key().public_key()))

    def add(self, username: str, endpoint: str, p256dh: str, auth: str, device: str = "") -> str:
        pushkey = secrets.token_urlsafe(24)
        with self._conn() as c:
            c.execute("DELETE FROM subs WHERE endpoint = ?", (endpoint,))      # the same phone subscribing again
            c.execute("INSERT INTO subs VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (pushkey, username, endpoint, p256dh, auth, device[:80], int(time.time())))
        return pushkey

    def get(self, pushkey: str) -> Optional[Dict[str, str]]:
        with self._conn() as c:
            row = c.execute("SELECT pushkey, username, endpoint, p256dh, auth, device FROM subs WHERE pushkey = ?",
                            (pushkey,)).fetchone()
        return dict(zip(("pushkey", "username", "endpoint", "p256dh", "auth", "device"), row)) if row else None

    def remove(self, pushkey: str, username: Optional[str] = None) -> bool:
        with self._conn() as c:
            if username is None:
                cur = c.execute("DELETE FROM subs WHERE pushkey = ?", (pushkey,))
            else:
                cur = c.execute("DELETE FROM subs WHERE pushkey = ? AND username = ?", (pushkey, username))
        return cur.rowcount > 0

    def devices(self, username: str) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT pushkey, device, created FROM subs WHERE username = ? ORDER BY created", (username,)).fetchall()
        return [{"pushkey": p, "device": d, "created": t} for p, d, t in rows]


def raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ── Web Push ─────────────────────────────────────────────────────────────────────────────────

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, salt: Optional[bytes] = None,
            server_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    """RFC 8291 message encryption, one aes128gcm record (RFC 8188)."""
    ua_public = unb64u(p256dh)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = raw_public(server_key.public_key())
    shared = server_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > RECORD_SIZE - 17:
        raise ValueError("push payload too long")
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def vapid_headers(endpoint: str, key: ec.EllipticCurvePrivateKey, now: Optional[float] = None) -> Dict[str, str]:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": VAPID_SUBJECT}
    token = jwt.encode(claims, key, algorithm="ES256")
    return {"Authorization": f"vapid t={token}, k={b64u(raw_public(key.public_key()))}"}


async def send(session: aiohttp.ClientSession, sub: Dict[str, str], message: Dict[str, Any],
               key: ec.EllipticCurvePrivateKey) -> int:
    """Send one push; returns the push service's HTTP status (0 when it could not be reached)."""
    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), sub["p256dh"], sub["auth"])
    headers = {**vapid_headers(sub["endpoint"], key), "TTL": str(TTL), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "Urgency": "high"}
    try:
        async with session.post(sub["endpoint"], data=body, headers=headers, timeout=TIMEOUT) as r:
            return r.status
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        logger.warning("push: could not reach the push service: %s", type(e).__name__)
        return 0


# ── what a Matrix notification becomes ───────────────────────────────────────────────────────

def message_for(notification: Dict[str, Any], private: bool = False, said: Optional[str] = None) -> Dict[str, Any]:
    """Who wrote, where, and the chat to open. The content of a Matrix message is encrypted and not
    read; ``said`` is what an agent told Logos it wrote (alert_text.py), shown instead of "New message".
    ``private`` (the person's choice in Settings › Phone alerts): not even who or where, only
    "Logos · New message"."""
    room = str(notification.get("room_id") or "")
    if not notification.get("event_id"):
        return {"clear": room, "unread": int((notification.get("counts") or {}).get("unread") or 0)}
    who = str(notification.get("sender_display_name") or notification.get("sender") or "Someone")
    room_name = str(notification.get("room_name") or "")
    unread = int((notification.get("counts") or {}).get("unread") or 0)
    words = "New message" if unread <= 1 else f"{unread} new messages"
    if private:
        return {"title": "Logos", "body": words, "room": room, "link": f"#/chat/room/{room}", "unread": unread}
    if said:
        words = said
    return {"title": room_name or who, "body": f"{who}: {words}" if room_name else words,
            "room": room, "link": f"#/chat/room/{room}", "unread": unread}


def alerts_private(username: str) -> bool:
    """The person chose "Nothing but New message" for their phone alerts (prefs.alerts == "private")."""
    try:
        from gateway.auth import db as auth_db
        from gateway.dashboard_api import prefs_of
        user = auth_db.get_user_by_username(username) if username else None
        return bool(user) and prefs_of(str(user["id"])).get("alerts") == "private"
    except Exception:                                    # pragma: no cover - a lookup that fails keeps the default
        logger.warning("push: could not read %s's alert choice", username, exc_info=True)
        return False


def trusted(remote: Optional[str]) -> bool:
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    return any(ip in n for n in TRUSTED)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _store(request: web.Request) -> Store:
    return request.app["push_store"]


def gateway_url(request: web.Request) -> str:
    """Where the Matrix server posts: this dashboard, on the Docker bridge."""
    if os.getenv("LOGOS_PUSH_GATEWAY_URL"):
        return os.environ["LOGOS_PUSH_GATEWAY_URL"]
    host = "172.17.0.1"
    runtime = request.app.get("sandbox_backend")
    try:
        from gateway import agent_chat
        from gateway.matrix_host import MatrixHost
        rt = agent_chat.runtime_of(runtime)
        if rt is not None:
            host = MatrixHost(rt).bind_address()
    except Exception:
        pass
    return f"http://{host}:{os.getenv('LOGOS_PORT') or '8091'}{NOTIFY_PATH}"


async def handle_key(request: web.Request) -> web.Response:
    """GET /api/v2/push — the public key to subscribe with, the gateway for the Matrix pusher, and
    which of your devices have notifications on."""
    from gateway.dashboard_api import _user
    user = _user(request)
    return web.json_response({"public_key": _store(request).public_key(), "app_id": APP_ID,
                              "gateway_url": gateway_url(request), "devices": _store(request).devices(user["username"])})


async def handle_subscribe(request: web.Request) -> web.Response:
    """POST /api/v2/push/subscribe {subscription: PushSubscription.toJSON(), device} -> {pushkey}."""
    from gateway.dashboard_api import _user
    user = _user(request)
    try:
        body = await request.json()
        sub = body["subscription"]
        endpoint, keys = str(sub["endpoint"]), sub["keys"]
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
        if urlsplit(endpoint).scheme != "https" or len(unb64u(p256dh)) != 65 or len(unb64u(auth)) != 16:
            raise ValueError("bad subscription")
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_subscription"}, status=400)
    pushkey = _store(request).add(user["username"], endpoint, p256dh, auth, str(body.get("device") or ""))
    logger.info("push: notifications on for %s (%s)", user["username"], body.get("device") or "a device")
    return web.json_response({"pushkey": pushkey})


async def handle_unsubscribe(request: web.Request) -> web.Response:
    """POST /api/v2/push/unsubscribe {pushkey}."""
    from gateway.dashboard_api import _user
    user = _user(request)
    try:
        pushkey = str((await request.json())["pushkey"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    return web.json_response({"removed": _store(request).remove(pushkey, user["username"])})


async def handle_test(request: web.Request) -> web.Response:
    """POST /api/v2/push/test {pushkey} — a test notification to one of your devices."""
    from gateway.dashboard_api import _user
    user = _user(request)
    try:
        pushkey = str((await request.json())["pushkey"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    sub = _store(request).get(pushkey)
    if not sub or sub["username"] != user["username"]:
        return web.json_response({"error": "unknown_device"}, status=404)
    status = await send(request.app["push_session"], sub, {"title": "Test notification", "body": "Messages from your agents and groups will arrive like this.",
                                                           "link": "#/home", "room": "test"}, _store(request).vapid_key())
    return web.json_response({"sent": 200 <= status < 300, "status": status})


async def handle_notify(request: web.Request) -> web.Response:
    """POST /_matrix/push/v1/notify — from the Matrix server (push gateway API)."""
    if not trusted(request.remote):
        return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
    try:
        notification = (await request.json())["notification"]
        devices = list(notification.get("devices") or [])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"errcode": "M_BAD_JSON"}, status=400)
    store, rejected = _store(request), []
    message = message_for(notification)
    if "clear" in message:
        # Only an unread count (the room was read). Android shows a generic notice for a push that shows
        # nothing, so none is sent: the page takes the room's notification away when it is opened.
        return web.json_response({"rejected": []})
    key = store.vapid_key()
    hidden: Dict[str, bool] = {}
    said: Optional[str] = None
    ours = [d for d in devices if d.get("app_id") == APP_ID and store.get(str(d.get("pushkey") or ""))]
    if any(not alerts_private(store.get(str(d["pushkey"]))["username"]) for d in ours):
        # An agent's reply: its first words, if its plugin handed them over (alert_text.py).
        from gateway import alert_text
        kept = request.app.get("alert_text")
        if kept is not None:
            said = await alert_text.wait_words(kept, str(notification.get("event_id") or ""),
                                               str(notification.get("sender") or ""))
    message = message_for(notification, said=said)
    if "-agent:" in str(notification.get("sender") or ""):
        logger.info("push: an agent's reply, alert %s", "with its first words" if said else "says New message")
    for d in devices:
        pushkey = str(d.get("pushkey") or "")
        if d.get("app_id") != APP_ID:
            continue
        sub = store.get(pushkey)
        if not sub:
            rejected.append(pushkey)
            continue
        who = sub.get("username") or ""
        if who not in hidden:
            hidden[who] = alerts_private(who)
        status = await send(request.app["push_session"], sub, message_for(notification, True) if hidden[who] else message, key)
        if status in (404, 410):
            store.remove(pushkey)
            rejected.append(pushkey)
            logger.info("push: a device of %s is gone; forgotten", sub["username"])
    return web.json_response({"rejected": rejected})


async def _session(app: web.Application):
    app["push_session"] = aiohttp.ClientSession()
    yield
    await app["push_session"].close()


def register(app: web.Application, folder: Path) -> None:
    """Routes; call before the Matrix pass-through's catch-all /_matrix/ route."""
    from gateway.auth.middleware import require_csrf
    app["push_store"] = Store(folder)
    app.cleanup_ctx.append(_session)
    app.router.add_post(NOTIFY_PATH, handle_notify)
    app.router.add_get("/api/v2/push", handle_key)
    app.router.add_post("/api/v2/push/subscribe", require_csrf(handle_subscribe))
    app.router.add_post("/api/v2/push/unsubscribe", require_csrf(handle_unsubscribe))
    app.router.add_post("/api/v2/push/test", require_csrf(handle_test))
