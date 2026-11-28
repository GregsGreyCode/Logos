"""Network approvals on the dashboard (story E6; the logic is in gateway/network_approvals.py).

* A background look every :data:`network_approvals.POLL_S` seconds at every running agent's draft
  rules; a new question goes to the agent's people as a phone alert too.
* ``GET  /api/v2/network``: the questions waiting for you, and what your agents may reach (an admin
  sees every agent's list: who can reach what is the admin's to know, like who can use which agent).
* ``POST /api/v2/network/{rid}/{once|always|deny}``: only one of the agent's own people.
* ``POST /api/v2/network-sites/{agent}/remove`` ``{host, port}``: its people, or an admin.
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


async def doing_for(app: Any, req: Mapping[str, Any], agent: Mapping[str, Any]) -> str:
    """What the agent was busy with when it was refused: the title of the conversation it was in.
    On a shared agent (the household one) only a scheduled job's name: one person's conversation is
    not shown to the other."""
    links = app.get("agent_links")
    if links is None:
        return ""
    try:
        client = await links.client(req["agent"])
        sessions = (await client.list_sessions(limit=10)).get("data") or []
    except Exception:
        return ""
    sole = len(na.approvers(agent)) == 1
    near = [s for s in sessions if abs(float(req["ts"]) - float(s.get("last_active") or s.get("started_at") or 0)) < DOING_WINDOW_S]
    near.sort(key=lambda s: -float(s.get("last_active") or 0))
    for s in near:
        if s.get("source") == "cron":
            return str(s.get("title") or "a scheduled job")
        if sole and s.get("title"):
            return str(s["title"])
    return ""


async def alert(app: Any, req: Mapping[str, Any], shown: str) -> None:
    """A phone alert to each of the agent's people. Never raises."""
    try:
        from gateway import push
        store, session = app.get("push_store"), app.get("push_session")
        if store is None or session is None:
            return
        key = store.vapid_key()
        for username in req.get("approvers") or []:
            message = na.push_message(req, shown, push.alerts_private(username))
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
        what = await doing_for(app, req, agent)
        if what:
            await asyncio.to_thread(net.set_for, req["id"], what)
        await alert(app, req, _shown(net.runtime, req["agent"]))
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
            out_requests.append({k: r.get(k) for k in ("id", "agent", "host", "port", "ts", "for", "binaries")})
    return web.json_response({"requests": out_requests, "agents": out_agents, "is_admin": admin})


async def handle_decide(request: web.Request) -> web.Response:
    user = _me(request)
    net = approvals_of(request.app)
    if net is None:
        return web.json_response({"error": "no_backend"}, status=503)
    status, body = await asyncio.to_thread(net.decide, request.match_info["rid"], request.match_info["decision"],
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


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/network", handle_list)
    app.router.add_post("/api/v2/network/{rid}/{decision}", require_csrf(handle_decide))
    app.router.add_post("/api/v2/network-sites/{agent}/remove", require_csrf(handle_remove))
    app.on_startup.append(_watch)
