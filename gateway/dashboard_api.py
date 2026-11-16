"""JSON endpoints for the v2 dashboard (the pages under ``gateway/ui/``).

Everything here answers for the signed-in user only:

* an agent is visible when its definition's ``owners`` allows the user
  (:func:`gateway.agent_chat.may_use`); anything else is a 404;
* a conversation is visible when it is one of the user's own web
  conversations, or, for an agent that belongs to that one person alone, one
  of its Telegram conversations. On a shared agent (the household one) other
  people's direct conversations are never listed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

import gateway.auth.db as auth_db
from gateway import agent_chat
from gateway.agent_links import AgentLinkError
from gateway.hermes_client import HermesError

logger = logging.getLogger(__name__)

WEB_PREFIX = "web-"


def _user(request: web.Request) -> Dict[str, str]:
    user = request.get("current_user") or {}
    uid = str(user.get("sub") or user.get("id") or "")
    row = (auth_db.get_user_by_id(uid) if uid else None) or {}
    return {"id": uid, "username": str(row.get("username") or ""),
            "display_name": str(row.get("display_name") or row.get("username") or ""),
            "role": str(user.get("role") or row.get("role") or "user")}


def conversation_prefix(user_id: str) -> str:
    """Prefix of the hermes session ids of this user's web conversations."""
    return agent_chat.session_id_for(user_id, "")[: -len("default")]


def visible_conversation(session: Mapping[str, Any], user_id: str, sole_owner: bool) -> Optional[str]:
    """``"web"`` / ``"telegram"`` when the user may see this hermes session, else ``None``."""
    sid = str(session.get("id") or "")
    if sid.startswith(conversation_prefix(user_id)):
        return "web"
    if sole_owner and session.get("source") == "telegram":
        return "telegram"
    return None


async def _visible_agents(request: web.Request, username: str) -> Dict[str, dict]:
    """``{name: {"definition", "owners"}}`` for the agents this user may use."""
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return {}
    shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
    out = {}
    for name, owners in shown.items():
        if agent_chat.may_use(owners, username):
            out[name] = {"owners": owners, "definition": await asyncio.to_thread(runtime.load, name)}
    return out


async def _agent_or_404(request: web.Request) -> tuple[str, dict, Dict[str, str]]:
    user = _user(request)
    name = request.match_info["name"]
    agents = await _visible_agents(request, user["username"])
    if name not in agents:
        raise web.HTTPNotFound(text='{"error":"not_found"}', content_type="application/json")
    return name, agents[name], user


async def handle_me(request: web.Request) -> web.Response:
    return web.json_response(_user(request))


async def handle_agents(request: web.Request) -> web.Response:
    """GET /api/v2/agents — the agents this user can talk to."""
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    backend = request.app.get("sandbox_backend")
    try:
        states = {st.name: st for st in await asyncio.to_thread(backend.list)} if backend else {}
    except Exception:
        states = {}
    out: List[dict] = []
    for name, info in sorted(agents.items()):
        row = auth_db.get_agent_by_name(name) or {}
        definition = info["definition"]
        owners = info["owners"]
        st = states.get(name)
        out.append({
            "name": name, "id": row.get("id"),
            "display_name": str(definition.get("display_name") or name),
            "running": bool(st and st.running), "state": st.state if st else "missing",
            "shared": owners is None or len(owners) > 1,
            "model": str((definition.get("model") or {}).get("default") or ""),
        })
    return web.json_response({"agents": out})


async def _client(request: web.Request, name: str):
    links = request.app.get("agent_links")
    if links is None:
        raise web.HTTPServiceUnavailable(text='{"error":"no_agents_running"}', content_type="application/json")
    try:
        return await links.client(name)
    except AgentLinkError as e:
        raise web.HTTPServiceUnavailable(text='{"error":"agent_unreachable"}',
                                         content_type="application/json") from e


async def handle_conversations(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/conversations — newest first."""
    name, info, user = await _agent_or_404(request)
    sole_owner = info["owners"] is not None and len(info["owners"]) == 1
    client = await _client(request, name)
    try:
        listing = await client.list_sessions(limit=200)
    except HermesError as e:
        logger.warning("conversations: %s unreachable: %s", name, e)
        return web.json_response({"error": "agent_unreachable"}, status=503)
    prefix = conversation_prefix(user["id"])
    out = []
    for s in listing.get("data") or []:
        where = visible_conversation(s, user["id"], sole_owner)
        if not where or not s.get("message_count"):
            continue
        sid = str(s["id"])
        out.append({
            "id": sid, "where": where,
            # A web conversation can be continued: this is the id the page sends back to /chat.
            "chat_id": sid[len(prefix):] if where == "web" else None,
            "title": str(s.get("title") or s.get("preview") or "Conversation")[:120],
            "last_active": s.get("last_active") or s.get("started_at"),
            "messages": s.get("message_count") or 0,
        })
    out.sort(key=lambda c: c["last_active"] or 0, reverse=True)
    return web.json_response({"conversations": out})


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):      # multi-part content: keep the text parts
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


async def handle_conversation_messages(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/conversations/{sid} — what was said, plus which tools ran."""
    name, info, user = await _agent_or_404(request)
    sid = request.match_info["sid"]
    sole_owner = info["owners"] is not None and len(info["owners"]) == 1
    client = await _client(request, name)
    try:
        session = await client.get_session(sid)
        if isinstance(session.get("session"), dict):
            session = session["session"]
        if not visible_conversation({**session, "id": sid}, user["id"], sole_owner):
            raise web.HTTPNotFound(text='{"error":"not_found"}', content_type="application/json")
        raw = await client.get_session_messages(sid)
    except HermesError as e:
        status = 404 if getattr(e, "status", None) == 404 else 503
        return web.json_response({"error": "not_found" if status == 404 else "agent_unreachable"}, status=status)
    out: List[dict] = []
    for m in sorted(raw.get("data") or [], key=lambda m: float(m.get("timestamp") or 0)):
        role = m.get("role")
        if role == "tool":
            if out and out[-1]["role"] == "assistant":
                out[-1]["tools"].append(str(m.get("tool_name") or "tool"))
            continue
        text = _text_of(m.get("content")).strip()
        if role == "assistant":
            if out and out[-1]["role"] == "assistant" and not out[-1]["text"]:
                out[-1]["text"] = text          # the turn's tool calls came first, then its words
            else:
                out.append({"role": "assistant", "text": text, "tools": []})
        elif role == "user" and text:
            out.append({"role": "user", "text": text, "tools": []})
    return web.json_response({"messages": [m for m in out if m["text"] or m["tools"]]})


def register(app: web.Application) -> None:
    app.router.add_get("/api/v2/me", handle_me)
    app.router.add_get("/api/v2/agents", handle_agents)
    app.router.add_get("/api/v2/agents/{name}/conversations", handle_conversations)
    app.router.add_get("/api/v2/agents/{name}/conversations/{sid}", handle_conversation_messages)
