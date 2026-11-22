"""F40: connecting your own mailbox from Settings › Email, instead of scripts/logos-mail on the server.

Each signed-in person connects only their own mailbox (their username is the mail ``person`` of their
agent). The login goes where the script puts it, ~/.config/logos/mail/<person>.json (0600), and the
mail service is restarted with it; nothing is ever sent back to the browser but the address.

* Gmail: an app password, checked with a real (read-only) sign-in before it is kept.
* Outlook.com / Hotmail: Microsoft's device sign-in with ONE Logos app registration for the whole
  install (its Application ID is set once by whoever manages Logos, in outlook-app.json). The page shows
  the code; this server waits for the sign-in in the background.
"""
from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aiohttp import web

from gateway import mail_host

logger = logging.getLogger(__name__)

LOGIN = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPES = "offline_access User.Read Mail.Read"          # read only: nothing here sends, deletes or moves mail
APP_FILE = "outlook-app.json"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ADDRESS = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
SENDER = re.compile(r"^([^@\s]{1,64}@)?[a-z0-9.-]{1,190}\.[a-z]{2,}$")


class Refused(Exception):
    """Something the person can fix; the message is shown as it is."""


# ── the files (shared with scripts/logos-mail) ─────────────────────────────────────────────────

def folder(env: Mapping[str, str]) -> Path:
    return mail_host.config_dir(env)


def load_box(env: Mapping[str, str], person: str) -> Optional[dict]:
    try:
        return json.loads((folder(env) / f"{person}.json").read_text())
    except (OSError, ValueError):
        return None


def save_box(env: Mapping[str, str], person: str, box: dict) -> Path:
    d = folder(env)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / f"{person}.json"
    old = load_box(env, person) or {}
    if old.get("exclude") and "exclude" not in box:
        box["exclude"] = old["exclude"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(box, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def remove_box(env: Mapping[str, str], person: str) -> bool:
    try:
        (folder(env) / f"{person}.json").unlink()
        return True
    except FileNotFoundError:
        return False


def outlook_app(env: Mapping[str, str]) -> str:
    try:
        cid = json.loads((folder(env) / APP_FILE).read_text()).get("client_id", "")
    except (OSError, ValueError):
        return ""
    return cid if GUID.match(cid or "") else ""


def set_outlook_app(env: Mapping[str, str], client_id: str) -> None:
    if not GUID.match(client_id or ""):
        raise Refused("That is not an Application (client) ID: it looks like 1a2b3c4d-… with 36 characters.")
    d = folder(env)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / APP_FILE).write_text(json.dumps({"client_id": client_id.lower()}))


# ── checking a login ─────────────────────────────────────────────────────────────────────────

def gmail_check(address: str, password: str, imap_cls=imaplib.IMAP4_SSL) -> int:
    """Sign in to Gmail read-only; the number of emails in the Inbox. Refused with a plain reason."""
    if not ADDRESS.match(address or ""):
        raise Refused("That does not look like an email address.")
    password = (password or "").replace(" ", "")
    if len(password) != 16 or not password.isalpha():
        raise Refused("A Gmail app password is 16 letters (Google shows it in groups of four).")
    try:
        imap = imap_cls("imap.gmail.com", 993, timeout=30)
        imap.login(address, password)
        _status, data = imap.select("INBOX", readonly=True)
        imap.logout()
    except imaplib.IMAP4.error:
        raise Refused("Gmail did not accept it. Check the address, that 2-Step Verification is on, "
                      "and make a new app password.") from None
    except OSError:
        raise Refused("Gmail could not be reached just now. Try again in a minute.") from None
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return 0


def _post(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"error": f"HTTP {e.code}"}


def outlook_start(client_id: str, post=_post) -> dict:
    start = post(LOGIN + "/devicecode", {"client_id": client_id, "scope": SCOPES})
    if "device_code" not in start:
        raise Refused("Microsoft refused to start the sign-in: "
                      + str(start.get("error_description") or start.get("error") or "no reason given").split("\r")[0])
    return start


def outlook_wait(client_id: str, start: dict, post=_post, sleep=time.sleep, now=time.time) -> dict:
    """Wait for the person to sign in on Microsoft's page; the token answer, or Refused."""
    interval, deadline = int(start.get("interval", 5)), now() + int(start.get("expires_in", 900))
    while now() < deadline:
        sleep(interval)
        tok = post(LOGIN + "/token", {"client_id": client_id, "device_code": start["device_code"],
                                      "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if "refresh_token" in tok:
            return tok
        if tok.get("error") == "authorization_pending":
            continue
        if tok.get("error") == "slow_down":
            interval += 5
            continue
        if tok.get("error") == "authorization_declined":
            raise Refused("The sign-in was cancelled on Microsoft's page.")
        raise Refused("Not connected: " + str(tok.get("error_description") or tok.get("error")).split("\r")[0])
    raise Refused("The code expired before the sign-in finished. Start again.")


def outlook_address(access_token: str) -> str:
    req = urllib.request.Request("https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
                                 headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as r:
        me = json.load(r)
    return str(me.get("mail") or me.get("userPrincipalName") or "")


# ── the handlers ─────────────────────────────────────────────────────────────────────────────

def _runtime(request: web.Request):
    from gateway import agent_chat
    return agent_chat.runtime_of(request.app.get("sandbox_backend"))


def _env(request: web.Request) -> Mapping[str, str]:
    rt = _runtime(request)
    return getattr(rt, "env", None) or os.environ


def _agent_for(rt, person: str) -> Optional[str]:
    """The agent that reads this person's mail (mail.enabled, mail.person == them), if any."""
    if rt is None:
        return None
    for name in rt.defined():
        a = rt.load(name)
        if mail_host.uses_mail(a) and mail_host.person_of(a) == person:
            return name
    return None


async def _restart(request: web.Request, person: str, fresh_login: bool) -> None:
    rt = _runtime(request)
    if rt is None:
        return
    host = mail_host.MailHost(rt)
    if fresh_login:
        await asyncio.to_thread(host.forget_token, person)
    await asyncio.to_thread(host.up)


def _health(rt, person: str) -> Optional[dict]:
    try:
        host = mail_host.MailHost(rt)
        with urllib.request.urlopen(f"http://{host.bind_address()}:{mail_host.PORT}/health", timeout=3) as r:
            return (json.load(r).get("mailboxes") or {}).get(person)
    except Exception:
        return None


def _user(request):
    from gateway.dashboard_api import _user as u
    return u(request)


async def handle_get(request: web.Request) -> web.Response:
    """GET /api/v2/mail: your mailbox (address and kind only), what Logos can connect, and how it is doing."""
    user = _user(request)
    person, env, rt = user["username"], _env(request), _runtime(request)
    box = load_box(env, person)
    agent = await asyncio.to_thread(_agent_for, rt, person)
    flow = (request.app.get("mail_flows") or {}).get(person)
    out: Dict[str, Any] = {
        "agent": agent, "outlook_ready": bool(outlook_app(env)), "is_admin": user.get("role") == "admin",
        "mailbox": {"kind": box.get("kind"), "address": box.get("address"), "exclude": box.get("exclude") or []} if box else None,
        "signin": {k: flow[k] for k in ("state", "code", "url", "expires", "error") if k in flow} if flow else None,
    }
    if box and rt is not None:
        out["health"] = await asyncio.to_thread(_health, rt, person)
    return web.json_response(out)


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        b = None
    return b if isinstance(b, dict) else {}


def _refused(e: Exception, status: int = 400) -> web.Response:
    return web.json_response({"error": "refused", "message": str(e)}, status=status)


def _lock(request: web.Request, person: str) -> asyncio.Lock:
    return request.app.setdefault("mail_locks", {}).setdefault(person, asyncio.Lock())


async def handle_gmail(request: web.Request) -> web.Response:
    """POST /api/v2/mail/gmail {address, app_password}: check it with a real sign-in, keep it, start reading."""
    person = _user(request)["username"]
    b = await _body(request)
    address, password = str(b.get("address") or "").strip(), str(b.get("app_password") or "")
    lock = _lock(request, person)
    if lock.locked():
        return _refused(Refused("Already checking. Wait a moment."), 409)
    async with lock:
        try:
            count = await asyncio.to_thread(gmail_check, address, password)
        except Refused as e:
            return _refused(e)
        await asyncio.to_thread(save_box, _env(request), person,
                                {"kind": "gmail", "address": address, "app_password": password.replace(" ", "")})
        await _restart(request, person, False)
    logger.info("mail: %s connected Gmail", person)
    return web.json_response({"connected": True, "inbox": count})


async def _outlook_flow(app: web.Application, request: web.Request, person: str, client_id: str, start: dict) -> None:
    flow = app["mail_flows"][person]
    try:
        tok = await asyncio.to_thread(outlook_wait, client_id, start)
        try:
            address = await asyncio.to_thread(outlook_address, tok["access_token"])
        except Exception:
            address = ""
        await asyncio.to_thread(save_box, _env(request), person,
                                {"kind": "outlook", "address": address or flow.get("address") or "",
                                 "client_id": client_id, "refresh_token": tok["refresh_token"]})
        await _restart(request, person, True)
        flow.update(state="connected", address=address)
        logger.info("mail: %s connected Outlook", person)
    except Refused as e:
        flow.update(state="failed", error=str(e))
    except Exception as e:                                # pragma: no cover - shown, then logged
        logger.warning("mail: Outlook sign-in for %s failed", person, exc_info=True)
        flow.update(state="failed", error=f"Something went wrong: {type(e).__name__}.")


async def handle_outlook(request: web.Request) -> web.Response:
    """POST /api/v2/mail/outlook: start Microsoft's sign-in; the page shows the code and asks how it goes."""
    person = _user(request)["username"]
    env = _env(request)
    client_id = outlook_app(env)
    if not client_id:
        return _refused(Refused("Outlook is not set up on this Logos yet: whoever manages it adds the Logos app first."), 409)
    flows = request.app.setdefault("mail_flows", {})
    old = flows.get(person)
    if old and old.get("state") == "waiting" and old.get("task") and not old["task"].done():
        old["task"].cancel()
    try:
        start = await asyncio.to_thread(outlook_start, client_id)
    except Refused as e:
        return _refused(e, 502)
    flows[person] = {"state": "waiting", "code": start["user_code"], "url": start["verification_uri"],
                     "expires": time.time() + int(start.get("expires_in", 900))}
    flows[person]["task"] = asyncio.ensure_future(_outlook_flow(request.app, request, person, client_id, start))
    return web.json_response({k: v for k, v in flows[person].items() if k != "task"})


async def handle_disconnect(request: web.Request) -> web.Response:
    """DELETE /api/v2/mail: forget your mailbox; your agent stops reading it."""
    person = _user(request)["username"]
    removed = await asyncio.to_thread(remove_box, _env(request), person)
    if removed:
        await _restart(request, person, True)
        logger.info("mail: %s disconnected", person)
    (request.app.get("mail_flows") or {}).pop(person, None)
    return web.json_response({"disconnected": removed})


async def handle_exclude(request: web.Request) -> web.Response:
    """POST /api/v2/mail/exclude {sender, remove?}: never copy mail from a domain or address (or again do)."""
    person = _user(request)["username"]
    env = _env(request)
    b = await _body(request)
    sender = str(b.get("sender") or "").strip().lower()
    if not SENDER.match(sender):
        return _refused(Refused("A domain (mybank.example) or an address (someone@mybank.example)."))
    box = load_box(env, person)
    if not box:
        return _refused(Refused("Connect a mailbox first."), 409)
    have = set(box.get("exclude") or [])
    box["exclude"] = sorted(have - {sender} if b.get("remove") else have | {sender})
    await asyncio.to_thread(save_box, env, person, box)
    await _restart(request, person, False)
    return web.json_response({"exclude": box["exclude"]})


async def handle_outlook_app(request: web.Request) -> web.Response:
    """PUT /api/v2/mail/outlook-app {client_id}: the Logos app registration, once, by whoever manages Logos."""
    if _user(request).get("role") != "admin":
        return web.json_response({"error": "forbidden"}, status=403)
    b = await _body(request)
    try:
        await asyncio.to_thread(set_outlook_app, _env(request), str(b.get("client_id") or "").strip())
    except Refused as e:
        return _refused(e)
    return web.json_response({"outlook_ready": True})


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/mail", handle_get)
    app.router.add_post("/api/v2/mail/gmail", require_csrf(handle_gmail))
    app.router.add_post("/api/v2/mail/outlook", require_csrf(handle_outlook))
    app.router.add_post("/api/v2/mail/exclude", require_csrf(handle_exclude))
    app.router.add_put("/api/v2/mail/outlook-app", require_csrf(handle_outlook_app))
    app.router.add_delete("/api/v2/mail", require_csrf(handle_disconnect))
