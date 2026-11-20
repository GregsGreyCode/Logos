"""JSON endpoints for the v2 dashboard (the pages under ``gateway/ui/``).

Everything here answers for the signed-in user only:

* an agent is visible when its definition's ``owners`` allows the user
  (:func:`gateway.agent_chat.may_use`); anything else is a 404;
* a conversation is visible when it is one of the user's own web
  conversations, or, for an agent that belongs to that one person alone, any
  of its Matrix (or old Telegram) conversations and scheduled jobs. On a shared
  agent (the household one) a person sees their own Matrix conversations and
  the scheduled jobs, never anyone else's conversations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

import gateway.auth.db as auth_db
from gateway import agent_chat
from gateway.agent_links import AgentLinkError
from gateway.hermes_client import HermesError
from gateway.lib.redact import redact_sensitive_text

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


# Where a hermes session came from, as the dashboard names it.
CHAT_SOURCES = {"matrix": "matrix", "telegram": "telegram"}
SCHEDULED_SOURCE = "cron"
MAX_REASONING = 8000       # characters of one step's reasoning shown in the viewer
MAX_TOOL_TEXT = 4000       # characters of one tool call's arguments or result


def _matrix_localpart(user_id: Any) -> str:
    """``@greg:example.org`` -> ``greg``; anything else -> ``""``."""
    text = str(user_id or "")
    return text[1:].split(":", 1)[0] if text.startswith("@") and ":" in text else ""


def visible_conversation(session: Mapping[str, Any], user_id: str, sole_owner: bool,
                         username: str = "") -> Optional[str]:
    """Where the session happened (``web``, ``matrix``, ``telegram``, ``scheduled``) when the user
    may see it, else ``None``.

    * Web conversations: only the person who had them.
    * Matrix and Telegram: everything on an agent that is the user's alone. On a shared agent, only
      the user's own (hermes keeps one session per person, in direct chats and in the Household room
      alike), matched by Matrix id; Telegram has no such match, so none.
    * Scheduled jobs (hermes cron): visible to everyone the agent belongs to.
    """
    sid = str(session.get("id") or "")
    if sid.startswith(conversation_prefix(user_id)):
        return "web"
    source = str(session.get("source") or "")
    if source in CHAT_SOURCES:
        if sole_owner:
            return CHAT_SOURCES[source]
        if source == "matrix" and username and _matrix_localpart(session.get("user_id")) == username:
            return "matrix"
        return None
    if source == SCHEDULED_SOURCE:
        return "scheduled"
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


MODEL_CHECK_SECONDS = 20


async def model_server_up(request: web.Request, definition: Mapping[str, Any]) -> Optional[bool]:
    """Whether this agent's local model server is answering: True, False, or None when the
    agent has no local model. Checked at most every 20 seconds per server."""
    from gateway import local_models
    base = local_models.server(definition.get("model") or {})
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not base or runtime is None:
        return None
    cache = request.app.setdefault("model_server_up", {})
    since = request.app.setdefault("model_server_off_since", {})
    now = asyncio.get_running_loop().time()
    if base not in cache or now - cache[base][0] > MODEL_CHECK_SECONDS:
        up = await asyncio.to_thread(local_models.reachable, runtime.runner.http, base)
        cache[base] = (now, up)
        if up:
            since.pop(base, None)
        else:
            since.setdefault(base, time.time())         # first seen off (story D2: "offline since 19:40")
    return cache[base][1]


def model_off_since(request: web.Request, definition: Mapping[str, Any]) -> Optional[float]:
    """When this agent's model server was first seen off (epoch seconds), or None while it is on."""
    from gateway import local_models
    base = local_models.server(definition.get("model") or {})
    return (request.app.get("model_server_off_since") or {}).get(base)


async def handle_model(request: web.Request) -> web.Response:
    """GET /api/v2/model?agent=<name> — is this agent's model server answering? Polled by web chat
    while a message waits for LM Studio (story D2). Checked at most every 20 seconds."""
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    name = request.query.get("agent", "")
    if name not in agents:
        return web.json_response({"error": "not_found"}, status=404)
    definition = agents[name]["definition"]
    up = await model_server_up(request, definition)
    return web.json_response({"model_up": up, "off_since": model_off_since(request, definition)})


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
            "voice": bool(definition.get("voice")),
            # The vault's name in Obsidian on the phones, for "Open in Obsidian" links. Syncthing
            # shares the brain as the folder brain-<agent>; a different name goes in the definition.
            "obsidian_vault": str(definition.get("obsidian_vault") or f"brain-{name}"),
            "model_up": await model_server_up(request, definition),
            "has_backup": await asyncio.to_thread(agent_chat.has_backup, definition),
        })
    # Agents this person cannot use are shown by name and up/down only.
    others = []
    runtime = agent_chat.runtime_of(backend)
    if runtime is not None:
        shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
        for name in sorted(set(shown) - set(agents)):
            definition = await asyncio.to_thread(runtime.load, name)
            st = states.get(name)
            others.append({"name": name, "display_name": str(definition.get("display_name") or name),
                           "running": bool(st and st.running)})
    return web.json_response({"agents": out, "others": others})


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
    """GET /api/v2/agents/{name}/conversations — newest first, chats and scheduled jobs."""
    name, info, user = await _agent_or_404(request)
    sole_owner = info["owners"] is not None and len(info["owners"]) == 1
    client = await _client(request, name)
    sessions: List[dict] = []
    try:
        # One listing per kind: hundreds of scheduled-job sessions would otherwise push the
        # chats out of a single page of 200.
        for source, limit in (("api_server", 200), ("matrix", 200), ("telegram", 100),
                              (SCHEDULED_SOURCE, 100)):
            sessions += (await client.list_sessions(limit=limit, source=source)).get("data") or []
    except HermesError as e:
        logger.warning("conversations: %s unreachable: %s", name, e)
        return web.json_response({"error": "agent_unreachable"}, status=503)
    prefix = conversation_prefix(user["id"])
    out, seen = [], set()
    for s in sessions:
        sid = str(s.get("id") or "")
        where = visible_conversation(s, user["id"], sole_owner, user["username"])
        if not where or not s.get("message_count") or sid in seen:
            continue
        seen.add(sid)
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


def _clip(text: str, limit: int) -> str:
    text = redact_sensitive_text(text)
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _tool_calls(raw: Any) -> List[dict]:
    """hermes stores an assistant message's tool calls as a JSON list (OpenAI shape)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    calls = []
    for c in raw if isinstance(raw, list) else []:
        fn = (c or {}).get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False) if args is not None else ""
        calls.append({"id": str(c.get("id") or c.get("call_id") or ""),
                      "name": str(fn.get("name") or c.get("name") or "tool"), "args": args})
    return calls


def fold_messages(raw: List[Mapping[str, Any]]) -> List[dict]:
    """hermes messages -> the turns of a conversation.

    Each assistant turn keeps what it said (``text``), the names of the tools it used (``tools``) and,
    for the viewer, its ``steps``: per model call, the reasoning and each tool call with its
    arguments and result. Secrets that look like keys or tokens are masked."""
    out: List[dict] = []
    by_call: Dict[str, dict] = {}
    for m in sorted(raw, key=lambda m: float(m.get("timestamp") or 0)):
        role = m.get("role")
        if role == "tool":
            result = _clip(_text_of(m.get("content")), MAX_TOOL_TEXT)
            call = by_call.get(str(m.get("tool_call_id") or ""))
            if call is not None:
                call["result"] = result
            elif out and out[-1]["role"] == "assistant":      # no id to match: keep it with its turn
                out[-1]["steps"].append({"reasoning": "", "calls": [
                    {"name": str(m.get("tool_name") or "tool"), "args": "", "result": result}]})
            if out and out[-1]["role"] == "assistant":
                out[-1]["tools"].append(str(m.get("tool_name") or "tool"))
            continue
        text = _text_of(m.get("content")).strip()
        if role == "assistant":
            reasoning = str(m.get("reasoning") or m.get("reasoning_content") or "").strip()
            step = {"reasoning": _clip(reasoning, MAX_REASONING) if reasoning else "", "calls": []}
            for c in _tool_calls(m.get("tool_calls")):
                call = {"name": c["name"], "args": _clip(c["args"], MAX_TOOL_TEXT), "result": None}
                step["calls"].append(call)
                if c["id"]:
                    by_call[c["id"]] = call
            if out and out[-1]["role"] == "assistant" and not out[-1]["text"]:
                turn = out[-1]                 # the turn's tool calls came first, then its words
                turn["text"] = text
            else:
                turn = {"role": "assistant", "text": text, "tools": [], "steps": []}
                out.append(turn)
            if step["reasoning"] or step["calls"]:
                turn["steps"].append(step)
        elif role == "user" and text:
            out.append({"role": "user", "text": text, "tools": [], "steps": []})
    return [m for m in out if m["text"] or m["tools"] or m["steps"]]


async def handle_conversation_messages(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/conversations/{sid} — what was said, which tools ran, and how the
    agent got there (its reasoning and each tool call)."""
    name, info, user = await _agent_or_404(request)
    sid = request.match_info["sid"]
    sole_owner = info["owners"] is not None and len(info["owners"]) == 1
    client = await _client(request, name)
    try:
        session = await client.get_session(sid)
        if isinstance(session.get("session"), dict):
            session = session["session"]
        where = visible_conversation({**session, "id": sid}, user["id"], sole_owner, user["username"])
        if not where:
            raise web.HTTPNotFound(text='{"error":"not_found"}', content_type="application/json")
        raw = await client.get_session_messages(sid)
    except HermesError as e:
        status = 404 if getattr(e, "status", None) == 404 else 503
        return web.json_response({"error": "not_found" if status == 404 else "agent_unreachable"}, status=status)
    usage = {k: session.get(k) for k in ("model", "input_tokens", "output_tokens", "reasoning_tokens",
                                          "tool_call_count", "api_call_count") if session.get(k) is not None}
    return web.json_response({"where": where, "usage": usage,
                              "messages": fold_messages(raw.get("data") or [])})


# ── Home (S6.11) ─────────────────────────────────────────────────────────────

async def handle_home(request: web.Request) -> web.Response:
    """GET /api/v2/home — for the person's own agent: today's calendar, brain pages changed in
    the last day, and whether the model server is answering. The noticeboard has its own call."""
    from gateway import brain_reader, home
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    name = home.own_agent(agents, user["username"])
    if not name:
        return web.json_response({"agent": None})
    definition = agents[name]["definition"]
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    out: Dict[str, Any] = {"me": user["username"], "agent": {"name": name, "display_name": str(definition.get("display_name") or name),
                                     "obsidian_vault": str(definition.get("obsidian_vault") or f"brain-{name}")},
                           "model_up": await model_server_up(request, definition)}
    out["model_off_since"] = model_off_since(request, definition)
    if runtime is None:
        out.update(today={"error": "no_backend"}, brain_changes=[])
        return web.json_response(out)
    today, listing = await asyncio.gather(
        asyncio.to_thread(home.calendar_today, runtime, name, definition, user["username"]),
        asyncio.to_thread(brain_reader.list_pages, runtime, name), return_exceptions=True)
    out["today"] = today if isinstance(today, dict) else {"error": "calendar_unavailable"}
    out["brain_changes"] = home.recent_brain_changes(listing) if isinstance(listing, dict) else []
    return web.json_response(out)


# ── which agents are working right now (the logo cycles while one is) ────────

ACTIVITY_CACHE_SECONDS = 8
ACTIVITY_WINDOW_SECONDS = 15 * 60     # an unanswered message older than this is not "working"


def owes_a_reply(last: Optional[Mapping[str, Any]]) -> bool:
    """True when a conversation's newest message means the agent is still at it: the person (or
    a scheduled job's prompt) spoke last, a tool just returned, or the agent asked for a tool."""
    if not last:
        return False
    role = last.get("role")
    if role in ("user", "tool"):
        return True
    return role == "assistant" and bool(last.get("tool_calls")) and not _text_of(last.get("content")).strip()


async def agent_is_working(client: Any, now: float) -> bool:
    """Is this agent in the middle of something (a chat on any channel, or a scheduled job)?

    hermes's own count of active work is not reliable for this (its heartbeat file can stay at 1
    while idle), so look at what it does keep exactly: the newest message of the most recently
    active conversation."""
    sessions = (await client.list_sessions(limit=20)).get("data") or []
    latest = max(sessions, key=lambda s: float(s.get("last_active") or s.get("started_at") or 0), default=None)
    if not latest or now - float(latest.get("last_active") or latest.get("started_at") or 0) > ACTIVITY_WINDOW_SECONDS:
        return False
    msgs = (await client.get_session_messages(str(latest["id"]), limit=1, order="latest")).get("data") or []
    return owes_a_reply(msgs[-1] if msgs else None)


async def handle_activity(request: web.Request) -> web.Response:
    """GET /api/v2/activity — {"working": [names]}: the user's agents that are working right now."""
    import time as _time
    user = _user(request)
    links = request.app.get("agent_links")
    if links is None:
        return web.json_response({"working": []})
    cache = request.app.setdefault("activity_cache", {})
    now = _time.time()
    backend = request.app.get("sandbox_backend")
    try:
        states = {st.name: st for st in await asyncio.to_thread(backend.list)} if backend else {}
    except Exception:
        states = {}
    working = []
    for name in await _visible_agents(request, user["username"]):
        if not (states.get(name) and states[name].running):
            continue
        hit = cache.get(name)
        if hit and now - hit[0] < ACTIVITY_CACHE_SECONDS:
            busy = hit[1]
        else:
            try:
                busy = await agent_is_working(await links.client(name), now)
            except (HermesError, AgentLinkError, KeyError, ValueError, TypeError):
                busy = False
            cache[name] = (now, busy)
        if busy:
            working.append(name)
    return web.json_response({"working": sorted(working)})


# ── agent status ─────────────────────────────────────────────────────────────

JOB_LABELS = {
    "consolidate": "Tidy the brain (nightly)",
    "brain-commit": "Save brain history",
    "brain-index": "Refresh the list of pages it knows",
    "noticeboard-check": "Check messages from the other agents",
    "noticeboard-urgent": "Watch for urgent messages",
    "weekly-digest": "Sunday summary of the week",
    "morning-briefing": "Morning briefing (weekdays)",
    "morning-briefing-weekend": "Morning briefing (weekends)",
    "morning-briefing-check": "Send a plain briefing if the weekday one failed",
    "morning-briefing-weekend-check": "Send a plain briefing if the weekend one failed",
    "rerun-after-outage": "Catch up on jobs missed while the AI computer was off",
    "model-watch": "Tell the group when the AI computer goes off or comes back",
    "changes-daily": "What changed today (16:00)",
    "changes-week": "What changed this week (Sundays)",
    "changes-check": "Send a plain list if the changes post failed",
    "lists-mirror": "Copy the shared lists into the household notes",
}
#: Jobs Logos installs and replaces on every update. Anything else is a reminder an agent set,
#: which its person may cancel. Kept in step with gateway/agent_runtime.py MANAGED_JOBS (tested).
MANAGED = set(JOB_LABELS) | {"changes-homelab", "changes-logos", "changes-homelab-week", "changes-logos-week"}


def parse_jobs(listing: str) -> List[dict]:
    """Jobs from ``hermes cron list`` output. Jobs Logos installs are ``managed``; anything else
    (a reminder the agent set) can be cancelled by its person."""
    import re as _re
    jobs: List[dict] = []
    job: Optional[dict] = None
    for line in listing.splitlines():
        m = _re.match(r"\s+([0-9a-f]{8,})\s+\[(\w+)\]", line)
        if m:
            job = {"id": m.group(1), "active": m.group(2) == "active", "name": "", "schedule": "",
                   "next_run": None, "last_run": None, "last_ok": None}
            jobs.append(job)
            continue
        m = _re.match(r"\s+(Name|Schedule|Next run|Last run):\s+(.*)$", line)
        if not (m and job):
            continue
        key, value = m.group(1), m.group(2).strip()
        if key == "Name":
            job["name"] = value
        elif key == "Schedule":
            job["schedule"] = value
        elif key == "Next run":
            job["next_run"] = value
        else:
            stamp, _, result = value.partition("  ")
            job["last_run"], job["last_ok"] = stamp.strip(), result.strip() == "ok"
    for j in jobs:
        j["managed"] = j["name"] in MANAGED
        j["label"] = JOB_LABELS.get(j["name"], j["name"])
    jobs.sort(key=lambda j: (j["managed"], j["label"].lower()))
    return jobs


def _sibling(runtime: Any, module: str):
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    name = f"logos_{module}"
    if name not in _sys.modules:
        spec = importlib.util.spec_from_file_location(name, _Path(__file__).with_name(f"{module}.py"))
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return _sys.modules[name]


async def handle_agent_status(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/status — what it does on its own, its model and budget, its reach."""
    name, info, _user_ = await _agent_or_404(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    definition = info["definition"]
    try:
        listing = await asyncio.to_thread(runtime.sbx, name, "export HERMES_HOME=/opt/data; hermes cron list")
        jobs: Optional[List[dict]] = parse_jobs(listing)
    except Exception as e:
        logger.info("agent status: could not list jobs for %s: %s", name, e)
        jobs = None
    mg = _sibling(runtime, "model_gateway")
    nb = _sibling(runtime, "noticeboard_host")
    model = definition.get("model") or {}
    via_gateway = mg.uses_gateway(definition)
    paid: List[str] = []
    if via_gateway:
        path = mg.keys_file(runtime.env)
        attached = {} if mg.BRAIN_STAYS_LOCAL else mg.paid_providers(path.read_text() if path.exists() else "")
        paid = [p for p in (model.get("allow") or []) if p in attached]
    allowed = []
    if (definition.get("matrix") or {}).get("enabled"):
        allowed.append("The Matrix server (chat)")
    allowed.append("The model gateway" if via_gateway else "Its model server")
    if nb.uses_noticeboard(definition):
        allowed.append("The noticeboard")
    if definition.get("voice"):
        allowed.append("The voice service (voice notes to text)")
    if _sibling(runtime, "calendar_host").uses_calendar(definition):
        allowed.append("The calendar service")
    return web.json_response({
        "jobs": jobs,
        "model": {"name": str(model.get("default") or ""), "local": model.get("provider") == "lmstudio",
                  "via_gateway": via_gateway, "paid_providers": paid,
                  "up": await model_server_up(request, definition)},
        "budget": {"monthly_usd": float((definition.get("budget") or {}).get("monthly_usd", mg.DEFAULT_MONTHLY_USD))
                   if via_gateway else None},
        "reach": {"allowed": allowed, "blocked": ["Your home network", "The internet", "The other agents"]},
    })


async def handle_job_cancel(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/jobs/{job}/cancel — remove a reminder the agent set. The jobs
    Logos installs cannot be removed here."""
    import re as _re
    name, _info, user = await _agent_or_404(request)
    job_id = request.match_info["job"]
    if not _re.fullmatch(r"[0-9a-f]{8,32}", job_id):
        return web.json_response({"error": "invalid_job"}, status=400)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    hermes = "export HERMES_HOME=/opt/data; hermes cron"
    try:
        jobs = parse_jobs(await asyncio.to_thread(runtime.sbx, name, f"{hermes} list"))
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            return web.json_response({"error": "not_found"}, status=404)
        if job["managed"]:
            return web.json_response({"error": "managed", "message": "That job is part of how the agent works."},
                                     status=400)
        await asyncio.to_thread(runtime.sbx, name, f"{hermes} remove {job_id}")
    except Exception as e:
        logger.warning("job cancel: %s: %s", name, e)
        return web.json_response({"error": "agent_unreachable", "message": "Could not reach the agent."}, status=503)
    auth_db.write_audit_log(user["id"], "agent_job_cancel", target_type="agent_job",
                            target_id=f"{name}:{job['name']}", ip_address=request.remote)
    return web.json_response({"ok": True})


# ── people ───────────────────────────────────────────────────────────────────

async def handle_people(request: web.Request) -> web.Response:
    """GET /api/v2/people — who can sign in, and which agents each person can use."""
    me = _user(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    shown: Dict[str, Any] = {}
    names: Dict[str, str] = {}
    if runtime is not None:
        shown = await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)
        for name in shown:
            names[name] = str((await asyncio.to_thread(runtime.load, name)).get("display_name") or name)
    users, _total = auth_db.list_users(page=1, limit=50)
    people = []
    for u in users:
        if u.get("status") not in (None, "active"):
            continue
        people.append({
            "username": u["username"], "display_name": u.get("display_name") or u["username"],
            "is_admin": u.get("role") == "admin", "is_you": u["id"] == me["id"],
            "signed_in_before": bool(u.get("last_login")),
            "agents": {n: agent_chat.may_use(owners, u["username"]) for n, owners in shown.items()},
        })
    people.sort(key=lambda p: (not p["is_you"], p["display_name"].lower()))
    return web.json_response({"people": people, "is_admin": me["role"] == "admin",
                              "agents": [{"name": n, "display_name": names[n]} for n in sorted(shown)]})


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


VOICE_MAX_BYTES = 25 * 1024 * 1024
_VOICE_SUFFIXES = {"audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
                   "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac"}


def audio_suffix(content_type: str) -> Optional[str]:
    """File suffix for a browser recording's content type, or None when it is not audio we take."""
    return _VOICE_SUFFIXES.get((content_type or "").split(";", 1)[0].strip().lower())


async def handle_voice(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/voice  (body: the recording)  ->  {"text": "..."}

    The microphone button in web chat. The recording goes to the voice service with the
    agent's own token, exactly as a voice note in chat does, and only its words come back;
    the person sees them in the message box before anything is sent to the agent.
    """
    import aiohttp
    from gateway import voice_host
    name, info, _user_ = await _agent_or_404(request)
    if not voice_host.uses_voice(info["definition"]):
        return web.json_response({"error": "no_voice", "message": "Voice is not switched on for this agent."},
                                 status=400)
    suffix = audio_suffix(request.headers.get("Content-Type", ""))
    if suffix is None:
        return web.json_response({"error": "not_audio", "message": "That is not a recording."}, status=415)
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(65536):
        size += len(chunk)
        if size > VOICE_MAX_BYTES:
            return web.json_response({"error": "too_large", "message": "That recording is too long."}, status=413)
        chunks.append(chunk)
    if not size:
        return web.json_response({"error": "empty", "message": "Nothing was recorded."}, status=400)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    host = voice_host.VoiceHost(runtime)
    tokens = request.app.setdefault("voice_tokens", {})
    if name not in tokens:
        tokens[name] = await asyncio.to_thread(host.token_for, name)
    if not tokens[name]:
        tokens.pop(name)
        return web.json_response({"error": "no_voice", "message":
                                  f"Voice is not set up for this agent yet (scripts/logos-agent up {name})."}, status=503)
    base = request.app.get("voice_base_url") or await asyncio.to_thread(host.base_url)
    request.app["voice_base_url"] = base
    form = aiohttp.FormData()
    form.add_field("file", b"".join(chunks), filename="recording" + suffix, content_type="application/octet-stream")
    form.add_field("model", "whisper-1")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            async with s.post(base + "/audio/transcriptions", data=form,
                              headers={"Authorization": f"Bearer {tokens[name]}"}) as r:
                if r.status == 401:
                    tokens.pop(name, None)          # the agent was set up again; read its token afresh next time
                if r.status == 422:
                    return web.json_response({"error": "unreadable", "message":
                                              "That recording could not be read. Try again."}, status=422)
                r.raise_for_status()
                text = str((await r.json()).get("text") or "").strip()
    except Exception as e:
        logger.warning("voice: transcription for %s failed: %s", name, type(e).__name__)
        return web.json_response({"error": "voice_unreachable", "message": "The voice service did not answer."},
                                 status=503)
    return web.json_response({"text": text})


MODELS_CACHE_SECONDS = 30


def _alive(http, url: str) -> bool:
    """Does anything answer at ``url``? An HTTP error status still means the service is up."""
    import urllib.error
    try:
        http(url, timeout=3)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def models_status(runtime: Any, definitions: Mapping[str, Mapping[str, Any]]) -> dict:
    """Health of everything a model call passes through. Blocking; run in a thread."""
    from gateway import local_models, model_gateway, noticeboard_host, voice_host
    http = runtime.runner.http
    gw = model_gateway.ModelGateway(runtime)
    bind = gw.bind_address()
    servers = []
    for base, wanted in local_models.wanted(definitions).items():
        up = local_models.reachable(http, base)
        loaded: Dict[str, int] = {}
        if up:
            try:
                loaded = local_models.loaded(http, base)
            except Exception:
                up = False
        servers.append({"up": up, "wanted": sorted(wanted),
                        "loaded": [{"model": m, "context": c} for m, c in sorted(loaded.items())],
                        "ready": bool(up and all(local_models.is_ready(loaded, m, c) for m, c in wanted.items()))})
    return {
        "model_server": servers[0] if servers else None,
        "services": [
            {"name": "Model gateway", "what": "passes every model call on, enforces limits", "up": gw.running()},
            {"name": "Model guard", "what": "refuses at once when the model server is off",
             "up": _alive(http, f"http://{bind}:{model_gateway.GUARD_PORT}/guard/health")},
            {"name": "Voice", "what": "voice notes to text",
             "up": _alive(http, f"http://{bind}:{voice_host.PORT}/health")},
            {"name": "Noticeboard", "what": "messages between agents",
             "up": _alive(http, f"http://{bind}:{noticeboard_host.PORT}/health")},
            {"name": "Dashboard", "what": "this page and web chat", "up": True},
        ] + _house_services(runtime, http, bind),
        # Which agents' sandboxes are Ready: an agent whose sandbox is gone answers nobody.
        "sandboxes": _sandboxes(runtime, definitions),
    }


#: Services checked only where their container exists (dev may not run them all).
HOUSE_SERVICES = (
    ("logos-matrix", "Matrix", "chat with the agents (Element X)", "http://127.0.0.1:6167/_matrix/client/versions"),
    ("logos-calendar", "Calendar server", "the calendars the phones sync", "http://127.0.0.1:5232/"),
    ("logos-calendar-service", "Calendar service", "how agents use the calendars", "http://{bind}:8095/health"),
    ("logos-house", "House service", "how the household agent works the lights", "http://{bind}:8098/health"),
    ("logos-sync", "Syncthing", "brains to the phones for Obsidian", "http://127.0.0.1:8384/rest/noauth/health"),
)


def _house_services(runtime: Any, http, bind: str) -> List[dict]:
    names = set(runtime.run(["docker", "ps", "-a", "--format", "{{.Names}}"], check=False).split())
    return [{"name": label, "what": what, "up": _alive(http, url.format(bind=bind))}
            for container, label, what, url in HOUSE_SERVICES if container in names]


def _sandboxes(runtime: Any, definitions: Mapping[str, Any]) -> Dict[str, bool]:
    try:
        phases = runtime.phases()
    except Exception:
        phases = {}
    return {name: phases.get(name) == "Ready" for name in sorted(definitions)}


def answering_now(agent: dict, server_up: Optional[bool]) -> dict:
    """Who would answer this agent's next message: the local model, a paid fallback, or nobody."""
    if server_up:
        return {"kind": "local", "label": "Local model"}
    for share in agent.get("fallbacks") or []:
        if not share["spent_out"]:
            return {"kind": "paid", "label": f"{share['provider']} · {share['model']}"}
    if agent.get("fallbacks"):
        return {"kind": "none", "label": "Nothing: the local model is off and the paid limit is spent"}
    return {"kind": "none", "label": "Nothing: the local model is off and no paid fallback is set up"}


async def models_overview(app: web.Application, runtime: Any) -> dict:
    """Status and spend for every agent, cached for half a minute (it starts a container)."""
    from gateway import model_gateway, model_usage
    loop = asyncio.get_running_loop()
    cached = app.get("models_overview")
    if cached and loop.time() - cached[0] < MODELS_CACHE_SECONDS:
        return cached[1]

    def build() -> dict:
        definitions = {}
        for name in runtime.defined():
            d = runtime.load(name)
            # Agents hidden from the dashboard (a dev agent defined but never started on this
            # machine) stay out of the figures and the alerts too.
            if (d.get("sandbox", True) is not False and d.get("dashboard") is not False
                    and (d.get("model") or {}).get("gateway")):
                definitions[name] = d
        gw = model_gateway.ModelGateway(runtime)
        # The brain stays local: a key in models.env is not attached to anything.
        attached = {} if model_gateway.BRAIN_STAYS_LOCAL else model_gateway.paid_providers(gw._keys_text())
        chains = {n: model_gateway.fallback_chain({**d, "name": n}, attached) for n, d in definitions.items()}
        image = next((str(d["image"]) for d in definitions.values() if d.get("image")), "")
        try:
            raw = model_usage.fetch(runtime, image) if image else {}
            error = None
        except Exception as e:
            logger.warning("models: could not read the gateway's records: %s", type(e).__name__)
            raw, error = {}, "The model gateway's records could not be read."
        out = model_usage.summarise(raw, definitions, chains)
        out["status"] = models_status(runtime, definitions)
        out["paid_providers"] = sorted(attached)
        out["error"] = error
        server = out["status"]["model_server"]
        for agent in out["agents"]:
            agent["now"] = answering_now(agent, server["up"] if server else None)
            agent["test"] = definitions[agent["name"]].get("role") == "test"
        return out

    data = await asyncio.to_thread(build)
    app["models_overview"] = (loop.time(), data)
    return data


async def handle_models(request: web.Request) -> web.Response:
    """GET /api/v2/models — model status for everyone; spend for the agents this person may
    use, or for all of them when they manage Logos."""
    user = _user(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return web.json_response({"error": "no_backend"}, status=503)
    data = dict(await models_overview(request.app, runtime))
    if user["role"] != "admin":
        mine = set(await _visible_agents(request, user["username"]))
        data["agents"] = [a for a in data["agents"] if a["name"] in mine]
        data["providers"], data["daily"], data["total"] = [], [], None      # whole-household figures
    data["all_agents"] = user["role"] == "admin"
    return web.json_response(data)


# ── the local model server (LM Studio) ──────────────────────────────────────

def _local_definitions(runtime: Any) -> Dict[str, Mapping[str, Any]]:
    out = {}
    for name in runtime.defined():
        d = runtime.load(name)
        if d.get("sandbox", True) is not False and d.get("dashboard") is not False:
            out[name] = d
    return out


def local_model_view(runtime: Any) -> dict:
    """Everything the "Local model" section shows, one block per LM Studio server. Blocking."""
    from gateway import model_gateway, model_server
    http = runtime.runner.http
    known = model_server.hardware(runtime.env)
    servers = []
    for base, want in sorted(model_server.wanted(_local_definitions(runtime)).items()):
        key = model_server.server_key(base)
        hw = known.get(key, {})
        view: Dict[str, Any] = {"server": key, "want": want, "up": False, "hardware": hw}
        try:
            listed = [model_server.describe(m) for m in model_server.models(http, base)]
            vram = float(hw.get("vram_gb") or 0)
            ours = next((m for m in listed if m["key"] in want), None)
            view.update(up=True, loaded=[m for m in listed if m["instances"]], downloaded=len(listed),
                        checks=model_server.checks(want, listed, vram), advice=model_server.advice(ours, vram))
        except Exception as e:
            view["error"] = f"LM Studio did not answer ({type(e).__name__})."
            view["advice"] = model_server.advice(None, float(hw.get("vram_gb") or 0))
        servers.append(view)
    gw = model_gateway.ModelGateway(runtime)
    logs = runtime.runner.run(["docker", "logs", "--since", "168h", model_gateway.GUARD_CONTAINER])
    return {"servers": servers,
            "queue": model_server.queue(http, f"http://{gw.bind_address()}:{model_gateway.GUARD_PORT}/guard/health"),
            "speed": model_server.speed(((logs.stdout or "") + (logs.stderr or "")).splitlines())}


async def handle_local_model(request: web.Request) -> web.Response:
    """GET /api/v2/models/local — each LM Studio server: what is loaded, checks, advice; the queue and speed."""
    _user(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return web.json_response({"error": "no_backend"}, status=503)
    view = await asyncio.to_thread(local_model_view, runtime)
    view["can_change"] = _user(request)["role"] == "admin"
    return web.json_response(view)


async def handle_local_model_action(request: web.Request) -> web.Response:
    """POST /api/v2/models/local/{action} with {"server": "host:port", ...}. Admin only.
    ``reload``: the agents' model on that server, with Logos's window. ``unload``: {"instance"} of a
    model no agent uses. ``hardware``: {"gpu", "vram_gb"} for that server (0 forgets it). Only servers
    an agent definition names can be acted on."""
    from gateway import model_server
    if _user(request)["role"] != "admin":
        return web.json_response({"error": "forbidden"}, status=403)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return web.json_response({"error": "no_backend"}, status=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    servers = {model_server.server_key(b): (b, w) for b, w in model_server.wanted(_local_definitions(runtime)).items()}
    key = str(body.get("server") or "")
    if key not in servers:
        return web.json_response({"error": "unknown_server"}, status=400)
    base, want = servers[key]
    action, who = request.match_info["action"], _user(request)["username"]
    try:
        if action == "reload":
            model, w = next(iter(sorted(want.items())))
            config = await asyncio.to_thread(model_server.reload, runtime.runner.http, base, model, w["load"])
            logger.info("local model reloaded by %s: %s on %s", who, model, key)
            return web.json_response({"done": "reloaded", "model": model, "config": config.get("load_config")})
        if action == "unload":
            instance = str(body.get("instance") or "")
            await asyncio.to_thread(model_server.unload, runtime.runner.http, base, instance, sorted(want))
            logger.info("local model unloaded by %s: %s on %s", who, instance, key)
            return web.json_response({"done": "unloaded", "instance": instance})
        if action == "hardware":
            saved = await asyncio.to_thread(model_server.set_hardware, runtime.env, key,
                                            str(body.get("gpu") or ""), body.get("vram_gb") or 0)
            logger.info("model server hardware set by %s for %s", who, key)
            return web.json_response({"done": "saved", "hardware": saved})
    except (ValueError, TypeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"LM Studio refused ({type(e).__name__})."}, status=502)
    return web.json_response({"error": "unknown_action"}, status=404)


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_post("/api/v2/agents/{name}/voice", require_csrf(handle_voice))
    app.router.add_get("/api/v2/agents/{name}/status", handle_agent_status)
    app.router.add_post("/api/v2/agents/{name}/jobs/{job}/cancel", require_csrf(handle_job_cancel))
    app.router.add_get("/api/v2/people", handle_people)
    app.router.add_get("/api/v2/activity", handle_activity)
    app.router.add_get("/api/v2/home", handle_home)
    app.router.add_get("/api/v2/model", handle_model)
    app.router.add_get("/api/v2/models", handle_models)
    app.router.add_get("/api/v2/models/local", handle_local_model)
    app.router.add_post("/api/v2/models/local/{action}", require_csrf(handle_local_model_action))
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
