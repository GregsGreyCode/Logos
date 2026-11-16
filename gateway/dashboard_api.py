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


# ── brain ────────────────────────────────────────────────────────────────────

async def _brain(request: web.Request, fn, *args):
    from gateway import brain_reader
    name, _info, _user_ = await _agent_or_404(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    try:
        return web.json_response(await asyncio.to_thread(fn, runtime, name, *args))
    except brain_reader.BrainError as e:
        return web.json_response({"error": "brain_unavailable", "message": str(e)}, status=404)


async def handle_brain(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/brain — its pages and what changed this week."""
    from gateway import brain_reader
    return await _brain(request, brain_reader.list_pages)


async def handle_brain_page(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/brain/page?path=projects/Trip.md"""
    from gateway import brain_reader
    return await _brain(request, brain_reader.read_page, request.query.get("path", ""))


async def handle_brain_page_save(request: web.Request) -> web.Response:
    """PUT /api/v2/agents/{name}/brain/page  {"path", "text", "modified"}

    Saves the person's edit of one existing page. ``modified`` is the value the page was
    loaded with; if the page changed since (the agent wrote to it, or an Obsidian edit
    synced in), the save is refused with 409 so nothing is overwritten unseen.
    """
    from gateway import brain_reader
    name, _info, user = await _agent_or_404(request)
    try:
        body = await request.json()
        path, text, modified = str(body["path"]), str(body["text"]), int(body["modified"])
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    try:
        saved = await asyncio.to_thread(brain_reader.write_page, runtime, name, path, text, modified)
    except brain_reader.PageChanged as e:
        return web.json_response({"error": "page_changed", "message": str(e)}, status=409)
    except brain_reader.BrainError as e:
        return web.json_response({"error": "not_saved", "message": str(e)}, status=400)
    auth_db.write_audit_log(user["id"], "brain_page_edit", target_type="brain_page",
                            target_id=f"{name}:{saved['path']}", ip_address=request.remote)
    return web.json_response({"ok": True, **saved})


async def handle_brain_page_create(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/brain/page  {"folder": "projects", "title": "Garden"}"""
    from gateway import brain_reader
    name, _info, user = await _agent_or_404(request)
    try:
        body = await request.json()
        folder, title = str(body["folder"]), str(body["title"])
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    try:
        made = await asyncio.to_thread(brain_reader.create_page, runtime, name, folder, title)
    except brain_reader.BrainError as e:
        return web.json_response({"error": "not_created", "message": str(e)}, status=400)
    auth_db.write_audit_log(user["id"], "brain_page_create", target_type="brain_page",
                            target_id=f"{name}:{made['path']}", ip_address=request.remote)
    return web.json_response({"ok": True, **made})


REFORMAT_MAX_CHARS = 8000
REFORMAT_SYSTEM = (
    "You tidy the formatting of Markdown notes. Keep every fact, name, number and date, and keep the "
    "wording. Only improve the structure: sensible headings, bullet lists for lists, one blank line "
    "between blocks, **bold** for short labels, consistent list markers. Do not add, remove, summarise, "
    "translate or comment. Reply with the tidied Markdown only, with no code fence around it.")


def reformat_is_sane(before: str, after: str) -> bool:
    """A tidy-up may move things around but must not drop or invent much: every number in the
    original must survive, and the length must stay in the same range."""
    if not after.strip():
        return False
    ratio = len(after) / max(len(before), 1)
    if not 0.6 <= ratio <= 1.6:
        return False
    import re as _re
    numbers = lambda t: sorted(_re.findall(r"\d+", t))
    return numbers(before) == numbers(after)


async def handle_brain_reformat(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/brain/reformat  {"text": "..."}  ->  {"text": "..."}

    One model call that tidies Markdown formatting and nothing else. It goes straight to the
    agent's local model server, not through the agent, so it has no tools and no memory and
    cannot change anything: the person sees the result in the editor and decides whether to save.
    """
    import aiohttp
    _name, info, _user_ = await _agent_or_404(request)
    try:
        text = str((await request.json())["text"])
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    if not text.strip():
        return web.json_response({"error": "empty", "message": "There is nothing to tidy."}, status=400)
    if len(text) > REFORMAT_MAX_CHARS:
        return web.json_response({"error": "too_long", "message":
                                  f"Select a shorter part (up to {REFORMAT_MAX_CHARS:,} characters)."}, status=400)
    model = info["definition"].get("model") or {}
    if model.get("provider") != "lmstudio" or not model.get("base_url"):
        return web.json_response({"error": "no_local_model", "message":
                                  "Tidying needs a local model, and this agent has none."}, status=400)
    # reasoning_effort "none": a formatting pass needs no deliberation, and with it a local
    # reasoning model answers in a couple of seconds instead of a minute.
    body = {"model": model["default"], "temperature": 0.1, "max_tokens": 6000, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": REFORMAT_SYSTEM}, {"role": "user", "content": text}]}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
            async with s.post(str(model["base_url"]).rstrip("/") + "/chat/completions", json=body) as r:
                r.raise_for_status()
                out = (await r.json())["choices"][0]["message"].get("content") or ""
    except Exception as e:
        logger.warning("brain reformat: model call failed: %s", e)
        return web.json_response({"error": "model_unreachable", "message": "The model did not answer."}, status=503)
    out = out.strip()
    if out.startswith("```"):                       # a fence around the whole answer, despite the instruction
        out = out.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not reformat_is_sane(text, out):
        return web.json_response({"error": "changed_too_much", "message":
                                  "The model changed more than the formatting, so its result was thrown away."},
                                 status=422)
    return web.json_response({"text": out + ("\n" if text.endswith("\n") else "")})


async def handle_brain_search(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/brain/search?q=boiler"""
    from gateway import brain_reader

    def _search(runtime, name, q):
        return {"hits": brain_reader.search(runtime, name, q)}
    return await _brain(request, _search, request.query.get("q", ""))


# ── noticeboard ──────────────────────────────────────────────────────────────

REQUEST_KINDS = ("question", "shared_change", "reminder")


def _board(request: web.Request):
    client = request.app.get("noticeboard_client")
    if client is None:
        runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
        if runtime is None:
            return None
        from gateway.noticeboard_client import NoticeboardClient
        client = request.app["noticeboard_client"] = NoticeboardClient(runtime)
    return client


async def _noticeboard(request: web.Request) -> tuple[List[dict], List[dict], Dict[str, str]]:
    """``(waiting, log, user)``: requests waiting on this person, and everything their agents
    sent or received (answers folded into the request they answer)."""
    from gateway.noticeboard_client import NoticeboardError
    user = _user(request)
    board = _board(request)
    if board is None:
        return [], [], user
    agents = await _visible_agents(request, user["username"])
    mine = [n for n, info in agents.items() if board.uses(info["definition"])]
    seen: Dict[int, dict] = {}
    for name in mine:
        try:
            for m in await board.history(name):
                seen.setdefault(m["id"], m)
        except NoticeboardError as e:
            logger.warning("noticeboard: %s: %s", name, e)
    log = sorted((m for m in seen.values() if m["kind"] != "answer"), key=lambda m: m["id"], reverse=True)
    for m in log:
        m["to_mine"], m["from_mine"] = m["to"] in mine, m["from"] in mine
    waiting = [m for m in log if m["kind"] in REQUEST_KINDS and m["status"] == "open" and m["to_mine"]]
    return waiting, log, user


async def handle_noticeboard(request: web.Request) -> web.Response:
    """GET /api/v2/noticeboard"""
    waiting, log, _user_ = await _noticeboard(request)
    return web.json_response({"waiting": waiting, "log": log})


def delegation_prompt(m: Mapping[str, Any], person: str) -> str:
    """What the agent is told when its person accepts a request that needs the agent to act."""
    what = ("Create the reminder as your own scheduled job, delivered to your person where they talk to "
            "you, and check that it exists." if m["kind"] == "reminder" else
            "Make the change in the shared brain.")
    return (f"{person} has just accepted noticeboard request {m['id']} from {m['from_name']} using the "
            f"dashboard. The request says: \"{m['text']}\"\n\n{what} Then send exactly one answer on the "
            f"noticeboard (kind \"answer\", reply_to {m['id']}, decision \"accepted\") saying what you did. "
            f"If you could not do it, answer with decision \"declined\" and the reason instead.")


async def handle_noticeboard_answer(request: web.Request) -> web.Response:
    """POST /api/v2/noticeboard/{id}/answer  {"decision": "accepted"|"declined", "text": "..."}

    A decline, or the answer to a question, is passed on as it is. Accepting a reminder or a
    shared change needs the agent to do something first, so the agent is told and answers itself.
    """
    from gateway.noticeboard_client import NoticeboardError
    try:
        body = await request.json()
        mid = int(request.match_info["id"])
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    decision = str(body.get("decision") or "")
    text = " ".join(str(body.get("text") or "").split())[:1500]
    if decision not in ("accepted", "declined"):
        return web.json_response({"error": "invalid_decision"}, status=400)
    waiting, _log, user = await _noticeboard(request)
    m = next((w for w in waiting if w["id"] == mid), None)
    if m is None:
        return web.json_response({"error": "not_found", "message": "That request is no longer waiting."}, status=404)
    person = user["display_name"] or user["username"]
    if decision == "accepted" and m["kind"] == "question" and not text:
        return web.json_response({"error": "answer_required", "message": "Type the answer to send."}, status=400)
    auth_db.write_audit_log(user["id"], "noticeboard_answer", target_type="noticeboard_message",
                            target_id=str(mid), metadata={"decision": decision, "agent": m["to"]},
                            ip_address=request.remote)
    if decision == "accepted" and m["kind"] in ("reminder", "shared_change"):
        try:
            client = await _client(request, m["to"])
            await client.start_run(delegation_prompt(m, person),
                                   session_id=agent_chat.session_id_for(user["id"], f"noticeboard-{mid}"))
        except HermesError as e:
            logger.warning("noticeboard: could not ask %s to act: %s", m["to"], e)
            return web.json_response({"error": "agent_unreachable",
                                      "message": f"Could not reach {m['to_name']} to do this."}, status=503)
        return web.json_response({"ok": True, "delegated": True})
    try:
        await _board(request).answer(m["to"], m["from"], mid, decision,
                                     text or f"{person} declined." if decision == "declined" else text)
    except NoticeboardError as e:
        return web.json_response({"error": "refused", "message": str(e)}, status=409)
    return web.json_response({"ok": True, "delegated": False})


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/agents/{name}/brain", handle_brain)
    app.router.add_get("/api/v2/agents/{name}/brain/page", handle_brain_page)
    app.router.add_get("/api/v2/agents/{name}/brain/search", handle_brain_search)
    app.router.add_put("/api/v2/agents/{name}/brain/page", require_csrf(handle_brain_page_save))
    app.router.add_post("/api/v2/agents/{name}/brain/page", require_csrf(handle_brain_page_create))
    app.router.add_post("/api/v2/agents/{name}/brain/reformat", require_csrf(handle_brain_reformat))
    app.router.add_get("/api/v2/noticeboard", handle_noticeboard)
    app.router.add_post("/api/v2/noticeboard/{id}/answer", require_csrf(handle_noticeboard_answer))
    app.router.add_get("/api/v2/me", handle_me)
    app.router.add_get("/api/v2/agents", handle_agents)
    app.router.add_get("/api/v2/agents/{name}/conversations", handle_conversations)
    app.router.add_get("/api/v2/agents/{name}/conversations/{sid}", handle_conversation_messages)
