"""Network approvals on the dashboard (story E6; the logic is in gateway/network_approvals.py).

* A background look every :data:`network_approvals.POLL_S` seconds at every running agent's draft
  rules; a new question goes to the agent's people as a phone alert too.
* ``GET  /api/v2/network``: the questions waiting for you, and what your agents may reach (an admin
  sees every agent's list: who can reach what is the admin's to know, like who can use which agent).
* ``POST /api/v2/network/{rid}/{once|always|deny}``: only one of the agent's own people. ``rid`` may be
  a group (one site asked by several agents at once): each of this person's agents in it is decided.
* ``POST /api/v2/network-sites/{agent}/remove`` ``{host, port}``: its people, or an admin.
* ``POST /api/v2/network-sites/{agent}/add`` ``{host, port}``: always allow a site, by hand (its people
  only; the same checks as answering a question).
* ``POST /api/v2/network-sites/{agent}/undeny`` ``{host, port}``: end a Deny early (its people only).
* Notifications: :func:`notification_items` (called by dashboard_api.handle_notifications).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

import gateway.auth.db as auth_db
from gateway import agent_chat
from gateway import network_approvals as na

logger = logging.getLogger(__name__)
DOING_WINDOW_S = 180              # a conversation active this recently is what the agent was doing


def approvals_of(app: Any) -> Optional[na.Approvals]:
    got = app.get("network_approvals") if hasattr(app, "get") else None
    if got is not None:
        return got
    runtime = agent_chat.runtime_of(app.get("sandbox_backend")) if hasattr(app, "get") else None
    if runtime is None or not hasattr(runtime, "env") or not hasattr(runtime, "runner"):
        return None
    got = na.Approvals(runtime, audit=lambda uid, action, ttype, tid, meta: auth_db.write_audit_log(
        uid, action, target_type=ttype, target_id=tid, metadata=meta))
    app["network_approvals"] = got
    return got


def _shown(runtime: Any, name: str) -> str:
    try:
        return str(runtime.load(name).get("display_name") or name)
    except Exception:
        return name


def job_about(title: str) -> str:
    """A scheduled job's name and what it does, from its session title (hermes: ``<name> · Nov 28 14:02``,
    blank while it runs). Logos's own jobs have a plain description (dashboard_api.JOB_LABELS)."""
    name = str(title or "").split(" · ")[0].strip()
    if not name or name.startswith("cron "):
        return "A scheduled job"
    from gateway.dashboard_api import JOB_LABELS
    what = JOB_LABELS.get(name)
    return f"Scheduled job “{name}”: {what}" if what else f"Scheduled job “{name}” (one the agent set up)"


async def doing_for(app: Any, req: Mapping[str, Any], agent: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """What the agent was busy with when it was refused: ``{"kind": "chat"|"job", "text": ...}``, or None.
    A chat shows its title on a private agent only: on a shared one (the household agent) one person's
    conversation is not shown to the other, only that it was a chat. A job shows its name and what it does."""
    links = app.get("agent_links")
    if links is None:
        return None
    try:
        client = await links.client(req["agent"])
        sessions = (await client.list_sessions(limit=10)).get("data") or []
    except Exception:
        return None
    sole = len(na.approvers(agent)) == 1
    near = [s for s in sessions if abs(float(req["ts"]) - float(s.get("last_active") or s.get("started_at") or 0)) < DOING_WINDOW_S]
    near.sort(key=lambda s: -float(s.get("last_active") or 0))
    for s in near:
        if s.get("source") == "cron":
            return {"kind": "job", "text": job_about(str(s.get("title") or ""))}
    for s in near:
        if sole and s.get("title"):
            return {"kind": "chat", "text": f"Your chat “{s['title']}”"}
        return {"kind": "chat", "text": "A chat with one of you (not shown: the agent is shared)"}
    return None


def _alerted(app: Any) -> Dict[str, set]:
    try:
        return app.setdefault("network_alerted", {})
    except AttributeError:
        return {}


async def alert(app: Any, req: Mapping[str, Any], shown: str, count: int = 1,
                people: Optional[List[str]] = None) -> None:
    """A phone alert to each of the agent's people (or only ``people``). Never raises."""
    try:
        from gateway import push
        store, session = app.get("push_store"), app.get("push_session")
        if store is None or session is None:
            return
        key = store.vapid_key()
        for username in (req.get("approvers") or []) if people is None else people:
            message = na.push_message(req, shown, push.alerts_private(username), count)
            for d in store.devices(username):
                sub = store.get(d["pushkey"])
                if sub:
                    await push.send(session, sub, message, key)
    except Exception as e:  # an alert must never stop the polling
        logger.warning("network: phone alert failed: %s", type(e).__name__)


async def poll_once(app: Any) -> List[Dict[str, Any]]:
    net = approvals_of(app)
    if net is None:
        return []
    new = await asyncio.to_thread(net.poll)
    for req in new:
        agent = await asyncio.to_thread(net._definition, req["agent"])
        doing = await doing_for(app, req, agent)
        await asyncio.to_thread(net.set_for, req["id"], (doing or {}).get("text", ""), na.asker(req, doing))
    # One alert per question: several agents asking for the same site at once are one question, and a
    # person already alerted about it is not alerted again when another of their agents joins it.
    alerted = _alerted(app)
    pending = await asyncio.to_thread(net.pending)
    for gid in dict.fromkeys(r.get("group") or r["id"] for r in new):
        group = [r for r in pending if (r.get("group") or r["id"]) == gid]
        done = alerted.setdefault(gid, set())
        for username in sorted({u for r in group for u in r.get("approvers") or []} - done):
            theirs = sorted((r for r in group if username in (r.get("approvers") or [])), key=lambda r: r["agent"])
            names = [_shown(net.runtime, r["agent"]) for r in theirs]
            shown = na.names_list(names) if len(names) <= 3 else f"{len(names)} agents"
            await alert(app, theirs[0], shown, len(names), [username])
            done.add(username)
    live = {r.get("group") or r["id"] for r in pending}
    for gid in [g for g in alerted if g not in live]:
        alerted.pop(gid, None)
    return new


async def _watch(app: web.Application) -> None:
    async def loop() -> None:
        while True:
            try:
                await poll_once(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("network: look at the agents' refused connections failed: %s", exc)
            await asyncio.sleep(na.POLL_S)
    app["network_watch"] = asyncio.create_task(loop())

    async def stop(_app: web.Application) -> None:
        _app["network_watch"].cancel()
    app.on_cleanup.append(stop)


def notification_items(app: Any, username: str, visible: Mapping[str, str], now: float, since: float) -> List[Dict[str, Any]]:
    try:
        return _items(app, username, visible, now, since)
    except Exception as e:          # the rest of the feed still shows
        logger.warning("network: notifications could not be read: %s", e)
        return []


def _items(app: Any, username: str, visible: Mapping[str, str], now: float, since: float) -> List[Dict[str, Any]]:
    net = approvals_of(app)
    if net is None:
        return []
    names = {}
    for l in net.recent(since):
        by = l.get("by")
        if by and by not in names:
            row = auth_db.get_user_by_username(by) or {}
            names[by] = str(row.get("display_name") or by)
    return na.notification_items(net, username, visible, now, since, names)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _me(request: web.Request) -> Dict[str, str]:
    from gateway.dashboard_api import _user
    return _user(request)


async def handle_list(request: web.Request) -> web.Response:
    user = _me(request)
    net = approvals_of(request.app)
    if net is None:
        return web.json_response({"requests": [], "agents": []})
    runtime = net.runtime
    names = await asyncio.to_thread(runtime.defined)
    admin = user["role"] == "admin"
    out_agents, out_requests = [], []
    allowed = await asyncio.to_thread(net.allowed)
    for name in names:
        agent = await asyncio.to_thread(net._definition, name)
        if not agent or agent.get("dashboard") is False and not admin or agent.get("sandbox") is False:
            continue
        mine = user["username"].lower() in na.approvers(agent)
        if not (mine or admin):
            continue
        a = allowed.get(name) or {"always": [], "once": [], "deny": []}
        out_agents.append({"name": name, "display_name": str(agent.get("display_name") or name), "yours": mine,
                           "people": na.approvers(agent), **a})
    for r in await asyncio.to_thread(net.pending):
        if user["username"].lower() in (r.get("approvers") or []):
            out_requests.append({**{k: r.get(k) for k in ("id", "agent", "host", "port", "ts", "for", "binaries")},
                                 "group": r.get("group") or r["id"], "asker": na.asker_of(r),
                                 "site": na.about_site(r["host"])})
    return web.json_response({"requests": out_requests, "agents": out_agents, "is_admin": admin})


async def handle_decide(request: web.Request) -> web.Response:
    user = _me(request)
    net = approvals_of(request.app)
    if net is None:
        return web.json_response({"error": "no_backend"}, status=503)
    # One question's id, or a group's (one site asked by several agents at once): each of this person's
    # agents in it is decided; the other agents' people still decide theirs.
    status, body = await asyncio.to_thread(net.decide_group, request.match_info["rid"], request.match_info["decision"],
                                           user["username"], user["id"] or None)
    return web.json_response(body, status=status)


async def handle_remove(request: web.Request) -> web.Response:
    user = _me(request)
    net = approvals_of(request.app)
    if net is None:
        return web.json_response({"error": "no_backend"}, status=503)
    name = request.match_info["agent"]
    try:
        body = await request.json()
        host, port = str(body["host"]), int(body["port"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    agent = await asyncio.to_thread(net._definition, name)
    if not agent:
        return web.json_response({"error": "not_found"}, status=404)
    if user["username"].lower() not in na.approvers(agent) and user["role"] != "admin":
        return web.json_response({"error": "forbidden"}, status=403)
    status, out = await asyncio.to_thread(net.revoke, name, host, port, user["username"], user["id"] or None)
    return web.json_response(out, status=status)


async def _site_change(request: web.Request, what: str) -> web.Response:
    user = _me(request)
    net = approvals_of(request.app)
    if net is None:
        return web.json_response({"error": "no_backend"}, status=503)
    try:
        body = await request.json()
        host, port = str(body["host"]), int(body.get("port") or 443)
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request", "message": "Give a site name, like example.org."}, status=400)
    fn = net.add_always if what == "add" else net.lift_deny
    status, out = await asyncio.to_thread(fn, request.match_info["agent"], host, port, user["username"], user["id"] or None)
    return web.json_response(out, status=status)


async def handle_add(request: web.Request) -> web.Response:
    return await _site_change(request, "add")


async def handle_undeny(request: web.Request) -> web.Response:
    return await _site_change(request, "undeny")


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/network", handle_list)
    app.router.add_post("/api/v2/network/{rid}/{decision}", require_csrf(handle_decide))
    app.router.add_post("/api/v2/network-sites/{agent}/remove", require_csrf(handle_remove))
    app.router.add_post("/api/v2/network-sites/{agent}/add", require_csrf(handle_add))
    app.router.add_post("/api/v2/network-sites/{agent}/undeny", require_csrf(handle_undeny))
    app.on_startup.append(_watch)
