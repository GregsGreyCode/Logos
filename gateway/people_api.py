"""The People screen's endpoints (backlog F38, docs/people.md): add a person by invitation, their page
(what was set up, who can see what), removing and restoring them, and the list of admin actions.

Admin only, with the CSRF check, except two things the invited person uses before they have an
account: ``/invite`` (the page) and ``/auth/invite/{check,accept}``. Those carry the invitation's
secret in a JSON body (never in an address, so it is not in any log or Referer) and are rate limited.
The blocking work is in :mod:`gateway.people`; here it runs off the event loop, one job at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aiohttp import web

import gateway.auth.db as auth_db
from gateway import agent_chat
from gateway import people as pp
from gateway.auth.middleware import check_rate_limit, require_csrf, require_permission
from gateway.auth.password import hash_password
from gateway.auth.tokens import REFRESH_TOKEN_TTL, issue_access_token, issue_refresh_token, set_auth_cookies

logger = logging.getLogger(__name__)

JSON_ONLY = "application/json"
PURGE_EVERY_S = 3600


def _runtime(request_or_app: Any):
    app = request_or_app.app if isinstance(request_or_app, web.Request) else request_or_app
    return agent_chat.runtime_of(app.get("sandbox_backend"))


def _people(request_or_app: Any) -> Optional[pp.People]:
    app = request_or_app.app if isinstance(request_or_app, web.Request) else request_or_app
    if app.get("people_factory"):                    # tests
        return app["people_factory"]()
    rt = _runtime(app)
    return pp.People(rt, auth_db, log=logger.info) if rt is not None else None


def _me(request: web.Request) -> Dict[str, str]:
    from gateway.dashboard_api import _user
    return _user(request)


def _refuse(message: str, status: int = 400, error: str = "refused") -> web.Response:
    return web.json_response({"error": error, "message": message}, status=status)


async def _body(request: web.Request) -> Dict[str, Any]:
    if request.content_type != JSON_ONLY:
        raise web.HTTPUnsupportedMediaType(text='{"error":"json_only"}', content_type=JSON_ONLY)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid_json"}', content_type=JSON_ONLY)
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error":"invalid_json"}', content_type=JSON_ONLY)
    return body


def _lock(app: web.Application) -> asyncio.Lock:
    if "people_lock" not in app:
        app["people_lock"] = asyncio.Lock()
    return app["people_lock"]


def _busy(app: web.Application, username: str) -> Optional[str]:
    job = (app.get("people_jobs") or {}).get(username)
    return job[0] if job and not job[1].done() else None


def _start(app: web.Application, username: str, kind: str, fn) -> None:
    """Run ``fn`` (blocking) in the background, after any other People job (they share the services)."""
    jobs = app.setdefault("people_jobs", {})

    async def run():
        async with _lock(app):
            try:
                result = await asyncio.to_thread(fn)
                logger.info("people: %s for %s done", kind, username)
                return result
            except Exception as e:                  # the page shows the steps; the log the reason
                logger.warning("people: %s for %s failed: %s", kind, username, pp._plain(e))
    jobs[username] = (kind, asyncio.ensure_future(run()))


def _audit(request: web.Request, action: str, target: Optional[dict] = None, **meta: Any) -> None:
    by = (request.get("current_user") or {}).get("sub")
    auth_db.write_audit_log(by, action, target_type="user" if target else None,
                            target_id=target["id"] if target else None,
                            metadata={k: v for k, v in meta.items() if v is not None} or None,
                            ip_address=request.remote)


def _expiry(days: Any) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = pp.DEFAULT_INVITE_DAYS
    if days not in pp.INVITE_DAYS:
        days = pp.DEFAULT_INVITE_DAYS
    return int(time.time()) + days * 86400


def _state(user: Mapping[str, Any], invite: Optional[Mapping[str, Any]], grave: Optional[Mapping[str, Any]],
           steps: list, busy: Optional[str]) -> str:
    if busy == "remove":
        return "removing"
    if grave or user.get("status") == "suspended":
        return "removed"
    if user.get("status") == "pending":
        if invite and not invite.get("used_at") and not invite.get("revoked_at") and invite["expires_at"] > time.time():
            return "invited"
        return "invite_expired"
    if busy or any(s.get("state") in ("running", "waiting") for s in steps):
        return "setting_up"
    if any(s.get("state") == "failed" for s in steps):
        return "needs_attention"
    return "active"


# ── the list ───────────────────────────────────────────────────────────────────────────────────

async def handle_people(request: web.Request) -> web.Response:
    """GET /api/v2/people — who can sign in, and which agents each person can use. An admin also sees
    the people invited and removed, and what this Logos can set up for a new person."""
    me = _me(request)
    is_admin = me["role"] == "admin"
    runtime = _runtime(request)
    shown: Dict[str, Any] = {}
    names: Dict[str, str] = {}
    if runtime is not None:
        shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
        for name in shown:
            names[name] = str((await asyncio.to_thread(runtime.load, name)).get("display_name") or name)
    users, _total = auth_db.list_users(page=1, limit=100)
    graves = {}
    if is_admin and runtime is not None:
        graves = {g["username"]: g for g in pp.Graveyard(runtime.env).list()}
    people, invited, removed = [], [], []
    for u in users:
        active = u.get("status") in (None, "active")
        if not active and not is_admin:
            continue
        name = u.get("display_name") or u["username"]
        if not active:
            invite = auth_db.latest_invite(u["id"])
            state = _state(u, invite, graves.get(u["username"]), [], _busy(request.app, u["username"]))
            if u.get("status") == "pending":
                invited.append({"username": u["username"], "display_name": name, "state": state,
                                "invite_expires": invite["expires_at"] if invite else None})
            else:
                grave = graves.get(u["username"]) or {}
                removed.append({"username": u["username"], "display_name": name, "state": state,
                                "delete_after": grave.get("delete_after")})
            continue
        row = {
            "username": u["username"], "display_name": name,
            "is_admin": u.get("role") == "admin", "is_you": u["id"] == me["id"],
            "signed_in_before": bool(u.get("last_login")),
            "agents": {n: agent_chat.may_use(owners, u["username"]) for n, owners in shown.items()},
        }
        if is_admin:
            row["state"] = _state(u, None, None, auth_db.get_people_setup(u["username"]), _busy(request.app, u["username"]))
        people.append(row)
    people.sort(key=lambda p: (not p["is_you"], p["display_name"].lower()))
    out: Dict[str, Any] = {"people": people, "is_admin": is_admin,
                           "agents": [{"name": n, "display_name": names[n]} for n in sorted(shown)]}
    if is_admin:
        mgr = _people(request)
        out["invited"] = sorted(invited, key=lambda p: p["display_name"].lower())
        out["removed"] = sorted(removed, key=lambda p: p["display_name"].lower())
        out["offers"] = await asyncio.to_thread(mgr.offers) if mgr else None
    return web.json_response(out)


# ── inviting ───────────────────────────────────────────────────────────────────────────────────

async def handle_invite(request: web.Request) -> web.Response:
    """POST /api/v2/people {display_name, username, email?, days?, chat, calendar, household, email_read}
    — make their (pending) account and a one-time link. The link's secret is in this answer only."""
    mgr = _people(request)
    if mgr is None:
        return _refuse("Agents are not available on this Logos, so people cannot be added here.", 409)
    b = await _body(request)
    display = str(b.get("display_name") or "").strip()
    username = str(b.get("username") or "").strip().lower()
    email = str(b.get("email") or "").strip().lower()
    offers = await asyncio.to_thread(mgr.offers)
    options = {"chat": bool(b.get("chat", True)) and offers["chat"],
               "calendar": bool(b.get("calendar", True)) and offers["calendar"],
               "household": bool(b.get("household", True)) and offers["household"],
               "email": bool(b.get("email_read", True))}
    why = await asyncio.to_thread(pp.check_new_person, username, display, email, mgr.rt, auth_db, mgr.graveyard)
    if not why and options["chat"]:
        try:
            if not await asyncio.to_thread(mgr.matrix().available, username):
                why = f"Someone is already called @{username} on the chat server. Choose another username, or untick Chat."
        except Exception as e:
            why = f"The chat server did not answer ({pp._plain(e)}). Try again, or untick Chat."
    if why:
        return _refuse(why)
    # A password nobody knows: the account cannot sign in until the person chooses theirs.
    user = auth_db.create_user(email=email or pp.placeholder_email(username), username=username,
                               password_hash=hash_password(pp.new_token()), role="user",
                               display_name=display, status="pending")
    token = pp.new_token()
    expires = _expiry(b.get("days"))
    auth_db.create_invite(user["id"], pp.hash_token(token), request["current_user"].get("sub"), expires, options)
    auth_db.set_people_setup(username, [])
    _audit(request, "person_invited", user, username=username, display_name=display, options=options, expires_at=expires)
    return web.json_response({"username": username, "token": token, "expires_at": expires, "options": options}, status=201)


def _target(username: str) -> dict:
    user = auth_db.get_user_by_username(username or "")
    if not user:
        raise web.HTTPNotFound(text='{"error":"not_found"}', content_type=JSON_ONLY)
    return user


async def handle_renew(request: web.Request) -> web.Response:
    """POST /api/v2/people/{username}/invite — a new link (the old one stops working)."""
    user = _target(request.match_info["username"])
    if user.get("status") != "pending":
        return _refuse("They have already accepted their invitation.", 409)
    old = auth_db.latest_invite(user["id"]) or {}
    token = pp.new_token()
    b = await _body(request) if request.can_read_body else {}
    expires = _expiry(b.get("days"))
    auth_db.create_invite(user["id"], pp.hash_token(token), request["current_user"].get("sub"), expires,
                          old.get("options") or {})
    _audit(request, "invite_renewed", user, username=user["username"], expires_at=expires)
    return web.json_response({"username": user["username"], "token": token, "expires_at": expires})


async def handle_cancel(request: web.Request) -> web.Response:
    """DELETE /api/v2/people/{username}/invite — they were never set up: forget the account."""
    user = _target(request.match_info["username"])
    if user.get("status") != "pending":
        return _refuse("They have already accepted; remove them from their page instead.", 409)
    auth_db.revoke_invites(user["id"])
    auth_db.delete_user(user["id"])
    auth_db.set_people_setup(user["username"], None)
    _audit(request, "invite_cancelled", None, username=user["username"])
    return web.json_response({"ok": True})


# ── the invited person ─────────────────────────────────────────────────────────────────────────

def _limited(request: web.Request) -> bool:
    return not check_rate_limit(f"invite:{request.remote or 'unknown'}", max_requests=20, window=60)


GONE = "This invitation link cannot be used: it was used already, it expired, or a newer one was sent. Ask for a new one."


async def handle_invite_check(request: web.Request) -> web.Response:
    """POST /auth/invite/check {token} — who it is for, so the page can greet them."""
    if _limited(request):
        return _refuse("Too many tries. Wait a minute.", 429, "rate_limited")
    b = await _body(request)
    inv = auth_db.find_open_invite(pp.hash_token(str(b.get("token") or "")))
    user = auth_db.get_user_by_id(inv["user_id"]) if inv else None
    if not inv or not user or user.get("status") != "pending":
        return _refuse(GONE, 410, "invite_unusable")
    by = auth_db.get_user_by_id(inv.get("created_by") or "") or {}
    opts = inv.get("options") or {}
    mgr = _people(request)
    server = ""
    if mgr and opts.get("chat"):
        try:
            server = mgr.matrix().settings()["MATRIX_SERVER_NAME"]
        except Exception:
            server = ""
    return web.json_response({"display_name": user.get("display_name") or user["username"],
                              "username": user["username"], "invited_by": by.get("display_name") or by.get("username") or "",
                              "expires_at": inv["expires_at"], "min_password": pp.MIN_PASSWORD,
                              "chat_id": f"@{user['username']}:{server}" if server else "",
                              "calendar": bool(opts.get("calendar"))})


async def handle_invite_accept(request: web.Request) -> web.Response:
    """POST /auth/invite/accept {token, password} — set their password (once), sign them in, make their
    chat and calendar accounts with it, and start setting up their agent."""
    if _limited(request):
        return _refuse("Too many tries. Wait a minute.", 429, "rate_limited")
    b = await _body(request)
    password = b.get("password")
    problem = pp.password_problem(password if isinstance(password, str) else "")
    if problem:
        return _refuse(problem, 400, "weak_password")
    token_hash = pp.hash_token(str(b.get("token") or ""))
    inv = auth_db.find_open_invite(token_hash)
    user = auth_db.get_user_by_id(inv["user_id"]) if inv else None
    if not inv or not user or user.get("status") != "pending":
        return _refuse(GONE, 410, "invite_unusable")
    inv = auth_db.use_invite(token_hash)                # once: a second tab at the same moment gets 410
    if not inv:
        return _refuse(GONE, 410, "invite_unusable")
    auth_db.update_user(user["id"], password_hash=hash_password(password), status="active")
    auth_db.write_audit_log(user["id"], "invite_accepted", target_type="user", target_id=user["id"],
                            metadata={"username": user["username"]}, ip_address=request.remote)
    options = inv.get("options") or {}
    display = user.get("display_name") or user["username"]
    mgr = _people(request)
    steps: list = []
    if mgr is not None:
        async with _lock(request.app):
            steps = await asyncio.to_thread(mgr.accept_accounts, user["username"], display, password, options)
        _start(request.app, user["username"], "setup", lambda: mgr.set_up_agent(user["username"], display, options))
    password = None                                         # noqa: F841 — not kept
    access = issue_access_token(user["id"], user["email"], user["role"])
    raw_refresh, rtk_hash = issue_refresh_token()
    auth_db.store_refresh_token(user["id"], rtk_hash, expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
                                ip=request.remote, ua=request.headers.get("User-Agent"))
    auth_db.update_last_login(user["id"])
    resp = web.json_response({"ok": True, "username": user["username"], "steps": steps})
    set_auth_cookies(resp, access, raw_refresh)
    return resp


async def handle_invite_page(request: web.Request) -> web.Response:
    html = (Path(__file__).parent / "html" / "invite.html").read_text()
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Frame-Options": "DENY"})


# ── one person ─────────────────────────────────────────────────────────────────────────────────

async def handle_person(request: web.Request) -> web.Response:
    """GET /api/v2/people/{username} — an admin, or the person themselves: their account, what was set
    up, and who can see what."""
    me = _me(request)
    username = request.match_info["username"]
    if me["role"] != "admin" and me["username"] != username:
        raise web.HTTPNotFound(text='{"error":"not_found"}', content_type=JSON_ONLY)
    user = _target(username)
    runtime = _runtime(request)
    agents: Dict[str, Any] = {}
    if runtime is not None:
        shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
        for n in shown:
            agents[n] = await asyncio.to_thread(runtime.load, n)
    users, _ = auth_db.list_users(page=1, limit=100)
    names = {u["username"]: u.get("display_name") or u["username"] for u in users}
    grave = pp.Graveyard(runtime.env).get(username) if runtime is not None else None
    invite = auth_db.latest_invite(user["id"])
    steps = auth_db.get_people_setup(username)
    busy = _busy(request.app, username)
    out: Dict[str, Any] = {
        "username": username, "display_name": user.get("display_name") or username,
        "is_admin": user.get("role") == "admin", "is_you": user["id"] == me["id"],
        "created_at": user.get("created_at"), "last_login": user.get("last_login"),
        "state": _state(user, invite, grave, steps, busy), "busy": busy,
        "visibility": pp.visibility(username, agents, names) if not grave else None,
    }
    if me["role"] == "admin":
        mgr = _people(request)
        out["steps"] = steps
        out["email"] = "" if user["email"].endswith(".logos.invalid") else user["email"]
        if invite and user.get("status") == "pending":
            out["invite"] = {"expires_at": invite["expires_at"], "options": invite.get("options") or {}}
        if grave:
            out["removed"] = {k: grave.get(k) for k in ("removed_at", "delete_after", "notes", "agent")}
            by = auth_db.get_user_by_id(grave.get("removed_by") or "") or {}
            out["removed"]["by"] = by.get("display_name") or by.get("username") or ""
        else:
            why = None
            if user["id"] == me["id"]:
                why = "You cannot remove yourself."
            elif user.get("role") == "admin":
                why = "Make them an ordinary member first: admins are not removed from here."
            elif user.get("status") == "pending":
                why = "Cancel the invitation instead."
            elif mgr is not None:
                why = await asyncio.to_thread(mgr.removable, username)
            out["removable"] = why is None
            out["not_removable_because"] = why
    return web.json_response(out)


async def handle_retry(request: web.Request) -> web.Response:
    """POST /api/v2/people/{username}/setup — run the background setup again (it skips what is done)."""
    mgr = _people(request)
    user = _target(request.match_info["username"])
    if mgr is None or user.get("status") != "active":
        return _refuse("Only someone who accepted their invitation can be set up.", 409)
    if _busy(request.app, user["username"]):
        return _refuse("Something is already being done for them.", 409)
    inv = auth_db.latest_invite(user["id"]) or {}
    opts = inv.get("options") or {}
    display = user.get("display_name") or user["username"]
    _audit(request, "person_setup", user, username=user["username"])
    _start(request.app, user["username"], "setup", lambda: mgr.set_up_agent(user["username"], display, opts))
    return web.json_response({"ok": True}, status=202)


async def handle_remove(request: web.Request) -> web.Response:
    """POST /api/v2/people/{username}/remove {confirm: username} — stop their agent and keep its backup
    for 30 days."""
    mgr = _people(request)
    me = _me(request)
    user = _target(request.match_info["username"])
    b = await _body(request)
    if str(b.get("confirm") or "") != user["username"]:
        return _refuse("Type their username to confirm.")
    if mgr is None:
        return _refuse("Agents are not available on this Logos.", 409)
    if user["id"] == me["id"]:
        return _refuse("You cannot remove yourself.", 409)
    if user.get("role") == "admin":
        return _refuse("Make them an ordinary member first: admins are not removed from here.", 409)
    if user.get("status") == "pending":
        return _refuse("Cancel the invitation instead.", 409)
    why = await asyncio.to_thread(mgr.removable, user["username"])
    if why:
        return _refuse(why, 409)
    if _busy(request.app, user["username"]):
        return _refuse("Something is already being done for them.", 409)
    _audit(request, "person_removed", user, username=user["username"])
    by = me["id"]
    _start(request.app, user["username"], "remove", lambda: mgr.remove(user["username"], by))
    return web.json_response({"ok": True}, status=202)


async def handle_restore(request: web.Request) -> web.Response:
    """POST /api/v2/people/{username}/restore — bring them back (within the 30 days)."""
    mgr = _people(request)
    user = _target(request.match_info["username"])
    if mgr is None or not mgr.graveyard.get(user["username"]):
        return _refuse("There is nothing to restore for them.", 409)
    if _busy(request.app, user["username"]):
        return _refuse("Something is already being done for them.", 409)
    _audit(request, "person_restored", user, username=user["username"])
    _start(request.app, user["username"], "restore", lambda: mgr.restore(user["username"]))
    return web.json_response({"ok": True}, status=202)


async def handle_audit(request: web.Request) -> web.Response:
    """GET /api/v2/people/audit — the newest admin actions about people (never a secret: none is logged)."""
    rows = auth_db.list_audit(list(pp.AUDIT_ACTIONS), limit=100)
    out = []
    for r in rows:
        meta = r.get("metadata") or {}
        out.append({"at": r["created_at"] // 1000, "action": r["action"],
                    "by": r.get("by_display_name") or r.get("by_username") or "",
                    "about": (meta.get("display_name") or meta.get("username") or meta.get("email") or "")
                    if isinstance(meta, dict) else ""})
    return web.json_response({"actions": out})


# ── housekeeping ───────────────────────────────────────────────────────────────────────────────

async def _startup(app: web.Application) -> None:
    """A gateway restart in the middle of a setup leaves steps 'running': say so, so they can be retried;
    then, every hour, delete those removed more than 30 days ago."""
    try:
        with auth_db._conn() as conn:
            rows = conn.execute("SELECT username FROM people_setup").fetchall()
        for r in rows:
            steps = auth_db.get_people_setup(r["username"])
            if any(s.get("state") == "running" for s in steps):
                for s in steps:
                    if s.get("state") == "running":
                        s.update(state="failed", detail="Interrupted by a restart of Logos; run the setup again")
                auth_db.set_people_setup(r["username"], steps)
    except Exception as e:
        logger.warning("people: could not check unfinished setups: %s", e)

    async def loop() -> None:
        while True:
            try:
                mgr = _people(app)
            except Exception as e:
                logger.warning("people: no graveyard clean-up: %s", pp._plain(e))
                mgr = None
            if mgr is not None:
                try:
                    async with _lock(app):
                        done = await asyncio.to_thread(mgr.purge_due)
                    for d in done:
                        auth_db.write_audit_log(None, "person_deleted", metadata={"username": d["username"],
                                                                                  "notes": d["notes"]})
                        logger.info("people: %s deleted for good (removed over %d days ago)", d["username"], pp.GRAVE_DAYS)
                except Exception as e:
                    logger.warning("people: graveyard clean-up failed: %s", pp._plain(e))
            await asyncio.sleep(PURGE_EVERY_S)
    app["people_purge"] = asyncio.ensure_future(loop())

    async def stop(_app: web.Application) -> None:
        app["people_purge"].cancel()
    app.on_cleanup.append(stop)


def register(app: web.Application) -> None:
    admin = lambda h: require_permission("manage_users")(require_csrf(h))        # noqa: E731
    admin_get = require_permission("manage_users")
    app.router.add_get("/api/v2/people", handle_people)
    app.router.add_post("/api/v2/people", admin(handle_invite))
    app.router.add_get("/api/v2/people/audit", admin_get(handle_audit))
    app.router.add_get("/api/v2/people/{username}", handle_person)
    app.router.add_post("/api/v2/people/{username}/invite", admin(handle_renew))
    app.router.add_delete("/api/v2/people/{username}/invite", admin(handle_cancel))
    app.router.add_post("/api/v2/people/{username}/setup", admin(handle_retry))
    app.router.add_post("/api/v2/people/{username}/remove", admin(handle_remove))
    app.router.add_post("/api/v2/people/{username}/restore", admin(handle_restore))
    app.router.add_get("/invite", handle_invite_page)
    app.router.add_post("/auth/invite/check", handle_invite_check)
    app.router.add_post("/auth/invite/accept", handle_invite_accept)
    app.on_startup.append(_startup)
