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
import os
import re
import logging
import time
import secrets
import subprocess
import urllib.parse
from pathlib import Path
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


LOOKS = ("glass", "hud", "warm", "paper", "pop")
MODES = ("dark", "light", "auto")


def look_of(user_id: str) -> Optional[Dict[str, str]]:
    """The person's chosen look ("glass" | "hud" | "warm" | "paper" | "pop") and light/dark mode, kept with their login
    (user_settings.ui_theme as "look:mode"), so it follows them from one device to another. None until
    they choose."""
    getter = getattr(auth_db, "get_user_settings", None)
    row = getter(user_id) if (user_id and getter) else None
    look, _, mode = str((row or {}).get("ui_theme") or "").partition(":")
    if look not in LOOKS:
        return None
    return {"look": look, "mode": mode if mode in MODES else "dark"}


# ST1: the dashboard's options each person sets in Settings, kept with their login (user_settings.ui_prefs).
PREFS = {"panel": ("slide", "open"), "thinking": (True, False), "jobs": (True, False),
         # ST3: how much the agents move, and when their names show on their marks
         "motion": ("full", "calm", "still"), "names": ("hover", "always"),
         # Home's widgets were rearranged once the calendar became one (22 Nov): an older choice gets it added
         "widgets_v2": (True, False),
         # Animations of the screens (slides, swipes, fades, loading), apart from the agents' movement (motion)
         "animations": ("on", "off"),
         # What a phone alert shows (Greg, 23 Nov): who wrote it, or nothing but "Logos · New message"
         "alerts": ("who", "private")}
# ST2: your own colours, any "#rrggbb" (the page keeps text readable whatever is picked); "" = the look's own
COLOUR_PREFS = ("primary", "attention")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# Home's widgets, each person's own choice (Greg, 22 Nov); the calendar is always there.
WIDGETS = ("next", "calendar", "messages", "lists", "lights", "weather", "coming")
# Widgets that come in more than one size, each person's own choice in Edit (Greg, 23 Nov: his To-do compact,
# Béré's full). The first is the default.
WIDGET_SIZES = {"lists": ("compact", "full")}
# F41: the screens whose first-visit tips someone has seen (gateway/ui/tips.js)
TIP_SCREENS = ("home", "agents", "chat", "notifications", "routines")


def _place_ok(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"name", "lat", "lon"}:
        return False
    try:
        return (isinstance(value["name"], str) and 0 < len(value["name"]) <= 120
                and -90 <= float(value["lat"]) <= 90 and -180 <= float(value["lon"]) <= 180)
    except (TypeError, ValueError):
        return False


def _pref_ok(key: str, value: Any) -> bool:
    if key == "widgets":
        from gateway.widgets import KINDS          # F42: the homelab widgets too
        return isinstance(value, list) and len(value) == len(set(value)) and all(w in WIDGETS or w in KINDS for w in value)
    if key == "tips_seen":
        return isinstance(value, list) and len(value) == len(set(value)) and all(t in TIP_SCREENS for t in value)
    if key == "weather_place":
        return _place_ok(value)
    if key == "widget_sizes":
        return isinstance(value, dict) and all(w in WIDGET_SIZES and v in WIDGET_SIZES[w] for w, v in value.items())
    if key == "widget_grid":
        # F49: the desktop Home layout: {widget: {x, y, w, h}} on a 12-column grid (h 0: as tall as its content)
        from gateway.widgets import KINDS
        def cell(c: Any) -> bool:
            return (isinstance(c, dict) and set(c) == {"x", "y", "w", "h"} and all(isinstance(c[k], int) and not isinstance(c[k], bool) for k in c)
                    and 0 <= c["x"] <= 11 and 1 <= c["w"] <= 12 and c["x"] + c["w"] <= 12 and 0 <= c["y"] <= 1_000_000 and 0 <= c["h"] <= 1_000_000)      # no practical limit: a widget may go as far down as you like
        return (isinstance(value, dict) and len(value) <= 40 and all((w in WIDGETS or w in KINDS) and cell(c) for w, c in value.items()))
    if key in COLOUR_PREFS:
        return isinstance(value, str) and (value == "" or bool(_HEX.match(value)))
    return key in PREFS and value in PREFS[key]


def prefs_of(user_id: str) -> Dict[str, Any]:
    getter = getattr(auth_db, "get_user_settings", None)
    row = getter(user_id) if (user_id and getter) else None
    try:
        saved = json.loads((row or {}).get("ui_prefs") or "{}")
    except ValueError:
        saved = {}
    return {k: v for k, v in (saved if isinstance(saved, dict) else {}).items() if _pref_ok(k, v)}


def agent_colours() -> Dict[str, str]:
    getter = getattr(auth_db, "get_agent_colours", None)
    return getter() if getter else {}


def matrix_id_of(request: web.Request, username: str) -> Optional[str]:
    """The person's Matrix ID (@username:server), for connecting their browser (M1); None without Matrix."""
    if "matrix_server_name" not in request.app:
        name = None
        runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
        if runtime is not None:
            try:
                from gateway.matrix_host import MatrixHost
                name = MatrixHost(runtime).settings()["MATRIX_SERVER_NAME"]
            except Exception:
                name = None
        request.app["matrix_server_name"] = name
    name = request.app["matrix_server_name"]
    return f"@{username}:{name}" if name and username else None


_VERSION: Optional[dict] = None


def version() -> dict:
    """What this Logos runs: the commit and its date (Settings shows it at the end of the list, like
    Element X's version line). Read once per start; every deploy restarts the dashboard."""
    global _VERSION
    if _VERSION is None:
        try:
            out = subprocess.run(["git", "log", "-1", "--format=%h %ct"], cwd=Path(__file__).resolve().parent.parent,
                                 capture_output=True, text=True, timeout=5).stdout.split()
            _VERSION = {"commit": out[0], "date": int(out[1])} if len(out) == 2 else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            _VERSION = {}
    return _VERSION


async def handle_me(request: web.Request) -> web.Response:
    user = _user(request)
    look, prefs, colours = await asyncio.gather(asyncio.to_thread(look_of, user["id"]), asyncio.to_thread(prefs_of, user["id"]),
                                                asyncio.to_thread(agent_colours))
    matrix = await asyncio.to_thread(matrix_id_of, request, user["username"])
    return web.json_response({**user, "look": look, "prefs": prefs, "agent_colours": colours, "matrix_id": matrix,
                              "version": version()})


async def handle_agent_colour(request: web.Request) -> web.Response:
    """PUT /api/v2/agents/{name}/colour {colour}: an agent's colour, the same for everyone. Only the
    people who may use the agent can set it: your own agent, and the household agent by either of you."""
    name, _agent, user = await _agent_or_404(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    colour = str((body or {}).get("colour") or "")
    if not _HEX.match(colour):
        return web.json_response({"error": "bad_colour"}, status=400)
    await asyncio.to_thread(auth_db.set_agent_colour, name, colour.lower(), user["username"])
    return web.json_response({"name": name, "colour": colour.lower()})


async def handle_set_prefs(request: web.Request) -> web.Response:
    """PUT /api/v2/me/prefs {key: value, ...}: change some of your own options; unknown ones are refused."""
    user = _user(request)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not body or not all(_pref_ok(k, v) for k, v in body.items()):
        return web.json_response({"error": "bad_prefs", "allowed": {**{k: list(v) for k, v in PREFS.items()},
                                                                   **{k: "#rrggbb" for k in COLOUR_PREFS},
                                                                   "widgets": list(WIDGETS), "tips_seen": list(TIP_SCREENS), "weather_place": "{name, lat, lon}",
                                                                   "widget_sizes": {w: list(v) for w, v in WIDGET_SIZES.items()}}},
                                 status=400)
    merged = {**await asyncio.to_thread(prefs_of, user["id"]), **body}
    seen = await asyncio.to_thread(_seen_until, user["id"])           # kept alongside, not settings
    read = await asyncio.to_thread(_read_ids, user["id"])
    stored = {**merged, **({"seen_until": seen} if seen else {}), **({"read_ids": read} if read else {})}
    await asyncio.to_thread(auth_db.update_user_settings, user["id"], ui_prefs=json.dumps(stored, sort_keys=True))
    return web.json_response(merged)


async def handle_set_look(request: web.Request) -> web.Response:
    """PUT /api/v2/me/look {look, mode}: the signed-in person's own look; nobody sets another's."""
    user = _user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    look, mode = str((body or {}).get("look") or ""), str((body or {}).get("mode") or "dark")
    if look not in LOOKS or mode not in MODES:
        return web.json_response({"error": "bad_look", "looks": list(LOOKS), "modes": list(MODES)}, status=400)
    if look == "hud":
        mode = "dark"                                   # the HUD is dark only (decided 20 Nov 2026)
    await asyncio.to_thread(auth_db.update_user_settings, user["id"], ui_theme=f"{look}:{mode}")
    return web.json_response({"look": look, "mode": mode})


MODEL_CHECK_SECONDS = 20


async def model_server_up(request: Any, definition: Mapping[str, Any]) -> Optional[bool]:
    """Whether this agent's local model server is answering: True, False, or None when the
    agent has no local model. Checked at most every 20 seconds per server. ``request`` may be the
    app itself (the background watch below)."""
    from gateway import local_models
    app = getattr(request, "app", request)
    base = local_models.server(definition.get("model") or {})
    runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
    if not base or runtime is None:
        return None
    cache = app.setdefault("model_server_up", {})
    since = app.setdefault("model_server_off_since", {})
    now = asyncio.get_running_loop().time()
    if base not in cache or now - cache[base][0] > MODEL_CHECK_SECONDS:
        up = await asyncio.to_thread(local_models.reachable, runtime.runner.http, base)
        cache[base] = (now, up)
        note_ai(app, bool(up))
        if up:
            since.pop(base, None)
        else:
            since.setdefault(base, time.time())         # first seen off (story D2: "offline since 19:40")
    return cache[base][1]


async def model_loaded(request: web.Request, definition: Mapping[str, Any]) -> Optional[bool]:
    """Whether this agent's model is loaded in LM Studio right now: True, False, or None when that
    cannot be told (no local model, or the server is off). Drives the floating agents' moods: asleep
    (on, nothing loaded) or awake (loaded). Checked at most every 20 seconds per server."""
    from gateway import local_models
    base = local_models.server(definition.get("model") or {})
    model_id = str((definition.get("model") or {}).get("default") or "")
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not base or not model_id or runtime is None:
        return None
    if await model_server_up(request, definition) is not True:
        return None
    cache = request.app.setdefault("model_loaded_ids", {})
    now = asyncio.get_running_loop().time()
    if base not in cache or now - cache[base][0] > MODEL_CHECK_SECONDS:
        try:
            ids = set(await asyncio.to_thread(local_models.loaded, runtime.runner.http, base))
        except Exception:                                   # answered a moment ago, not now: unknown
            ids = None
        cache[base] = (now, ids)
    ids = cache[base][1]
    return None if ids is None else model_id in ids


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
            "model_loaded": await model_loaded(request, definition),
            "has_backup": await asyncio.to_thread(agent_chat.has_backup, definition),
            # M2: the agent's own Matrix account (@<name>-agent:server), for finding its room
            "matrix_id": (await asyncio.to_thread(matrix_id_of, request, f"{name}-agent"))
            if (definition.get("matrix") or {}).get("enabled") else None,
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


def _plain(text: str) -> str:
    return " ".join(str(text or "").split())[:240]


async def handle_reply_steps(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/steps?text=... — M2: the thinking behind one of the agent's Matrix
    replies (its reasoning and each tool call), found by the reply's text among the newest of your
    own Matrix conversations with it. The same rules as the conversation list say whose you may see."""
    name, info, user = await _agent_or_404(request)
    want = _plain(request.query.get("text", ""))
    if len(want) < 2:
        return web.json_response({"error": "text_required"}, status=400)
    sole_owner = info["owners"] is not None and len(info["owners"]) == 1
    client = await _client(request, name)
    try:
        sessions = (await client.list_sessions(limit=20, source="matrix")).get("data") or []
        mine = [s for s in sessions if visible_conversation(s, user["id"], sole_owner, user["username"]) == "matrix"]
        mine.sort(key=lambda s: s.get("last_active") or s.get("started_at") or 0, reverse=True)
        for s in mine[:6]:
            raw = await client.get_session_messages(str(s.get("id")))
            for turn in reversed(fold_messages(raw.get("data") or [])):
                if turn["role"] == "assistant" and turn["text"] and (_plain(turn["text"]) == want
                                                                       or _plain(turn["text"]).startswith(want[:120])):
                    return web.json_response({"steps": turn["steps"], "tools": turn["tools"]})
    except HermesError:
        return web.json_response({"error": "agent_unreachable"}, status=503)
    return web.json_response({"steps": [], "tools": [], "found": False})


# ── Home (S6.11) ─────────────────────────────────────────────────────────────

HOME_CACHE_SECONDS = 60


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
                                     "obsidian_vault": str(definition.get("obsidian_vault") or f"brain-{name}"),
                                     "matrix": bool((definition.get("matrix") or {}).get("enabled")),
                                     "voice": bool(definition.get("voice"))},
                           "model_up": await model_server_up(request, definition)}
    out["model_off_since"] = model_off_since(request, definition)
    if runtime is None:
        out.update(today={"error": "no_backend"}, brain_changes=[])
        return web.json_response(out)
    # Both are read inside the agent's sandbox (about 1.5 s together), so they are kept for a minute.
    cache = request.app.setdefault("home_cache", {})
    key, now = (name, user["username"]), time.time()
    if key in cache and now - cache[key][0] < HOME_CACHE_SECONDS:
        today, listing = cache[key][1]
    else:
        today, listing = await asyncio.gather(
            asyncio.to_thread(home.calendar_today, runtime, name, definition, user["username"]),
            asyncio.to_thread(brain_reader.list_pages, runtime, name), return_exceptions=True)
        if isinstance(today, dict) and isinstance(listing, dict):
            cache[key] = (now, (today, listing))
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
    """GET /api/v2/activity — {"working": [names], "model_up": bool|None}: the user's agents that are
    working right now, and whether the AI computer is on (any of their agents' model servers answering;
    None when none uses a local model). The logo shows both (Greg, 21 Nov)."""
    import time as _time
    user = _user(request)
    links = request.app.get("agent_links")
    visible = await _visible_agents(request, user["username"])
    ups = [await model_server_up(request, a["definition"]) for a in visible.values()]
    model_up = True if any(u is True for u in ups) else (False if any(u is False for u in ups) else None)
    if links is None:
        return web.json_response({"working": [], "model_up": model_up})
    cache = request.app.setdefault("activity_cache", {})
    now = _time.time()
    backend = request.app.get("sandbox_backend")
    try:
        states = {st.name: st for st in await asyncio.to_thread(backend.list)} if backend else {}
    except Exception:
        states = {}
    working = []
    for name in visible:
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
    return web.json_response({"working": sorted(working), "model_up": model_up})


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


# ── Routines (Greg, 22 Nov): the built-in jobs people may change, and the ones they asked for ─────

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def cron_at(hhmm: str, dow: str = "*") -> str:
    """"07:30", "1-5" -> "30 7 * * 1-5"."""
    m = _HHMM.match(hhmm or "")
    if not m:
        raise ValueError("bad time")
    return f"{int(m.group(2))} {int(m.group(1))} * * {dow}"


def time_of(cron: str) -> str:
    """"30 7 * * 1-5" -> "07:30" ("" for anything else)."""
    parts = str(cron or "").split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    return ""


def routines_view(runtime: Any, name: str) -> dict:
    """What the Routines page shows for one agent's built-in jobs: only those it can have."""
    import yaml
    from gateway import agent_runtime as ar
    agent = runtime.load(name)
    plain = yaml.safe_load(runtime.definition_path(name).read_text()) or {}   # before anyone's choices
    out: Dict[str, Any] = {}
    if ar.delivery(agent) == "local":
        return out
    cal_person = (agent.get("calendar") or {}).get("person")
    if agent.get("role") != "household" and ((plain.get("briefing") or {}).get("enabled", bool(cal_person))):
        cfg = agent.get("briefing") or {}
        out["briefing"] = {"enabled": cfg.get("enabled", True), "weekdays": time_of(cfg.get("weekdays", "0 7 * * 1-5")),
                           "weekends": time_of(cfg.get("weekends", "0 9 * * 0,6"))}
    dg = agent.get("digest") or {}
    sched = str(dg.get("schedule", "0 18 * * 0")).split()
    out["digest"] = {"enabled": dg.get("enabled", True), "time": time_of(dg.get("schedule", "0 18 * * 0")),
                     "day": int(sched[4]) % 7 if len(sched) == 5 and sched[4].isdigit() else 0}
    if (plain.get("changes") or {}).get("enabled"):
        ch = agent.get("changes") or {}
        out["changes"] = {"enabled": ch.get("enabled", True), "time": time_of(ch.get("daily", "0 16 * * *"))}
    return out


def routines_choice(body: Mapping[str, Any], allowed: Mapping[str, Any]) -> dict:
    """The dashboard's change, checked and turned into the definition's own keys."""
    out: Dict[str, Any] = {}
    b = body.get("briefing")
    if isinstance(b, dict) and "briefing" in allowed:
        out["briefing"] = {"enabled": bool(b.get("enabled")), "weekdays": cron_at(b["weekdays"], "1-5"),
                           "weekends": cron_at(b["weekends"], "0,6")}
    d = body.get("digest")
    if isinstance(d, dict) and "digest" in allowed:
        day = int(d.get("day", 0))
        if not 0 <= day <= 6:
            raise ValueError("bad day")
        out["digest"] = {"enabled": bool(d.get("enabled")), "schedule": cron_at(d["time"], str(day))}
    c = body.get("changes")
    if isinstance(c, dict) and "changes" in allowed:
        daily = cron_at(c["time"])
        h, m = int(c["time"][:2]), int(c["time"][3:])
        later = lambda mins: f"{(h * 60 + m + mins) % 60} {min((h * 60 + m + mins) // 60, 23)}"
        # The week's roll-up ten minutes later on Sundays, and the plain fallback twenty minutes later.
        out["changes"] = {"enabled": bool(c.get("enabled")), "daily": daily,
                          "week": f"{later(10)} * * 0", "check": f"{later(20)} * * *"}
    if not out:
        raise ValueError("nothing to change")
    return out


async def handle_routines(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/routines — the built-in jobs this agent has, as people may set them.
    PUT with {briefing|digest|changes: {...}} keeps the choice and puts those jobs back at once."""
    from gateway import agent_runtime as ar
    name, _info, user = await _agent_or_404(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return web.json_response({"error": "no_runtime"}, status=503)
    allowed = await asyncio.to_thread(routines_view, runtime, name)
    if request.method == "GET":
        return web.json_response({"routines": allowed})
    try:
        choice = routines_choice(await request.json(), allowed)
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request", "message": "That time or day could not be used."}, status=400)
    await asyncio.to_thread(ar.save_routines, runtime.env, name, choice)
    try:
        await asyncio.to_thread(runtime.refresh_routines, name)
    except Exception as e:
        logger.warning("routines: %s: %s", name, e)
        return web.json_response({"error": "agent_unreachable", "saved": True,
                                  "message": "Saved; the agent will use it when it next starts."}, status=202)
    auth_db.write_audit_log(user["id"], "agent_routines", target_type="agent", target_id=name, ip_address=request.remote)
    return web.json_response({"routines": await asyncio.to_thread(routines_view, runtime, name)})


async def handle_job_action(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/jobs/{job}/{pause|resume|remove} — for jobs people asked their agent
    for. Logos's own jobs are changed on the Routines page, not here."""
    import re as _re
    name, _info, user = await _agent_or_404(request)
    job_id, action = request.match_info["job"], request.match_info["action"]
    if not _re.fullmatch(r"[0-9a-f]{8,32}", job_id) or action not in ("pause", "resume", "remove"):
        return web.json_response({"error": "invalid_request"}, status=400)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    hermes = "export HERMES_HOME=/opt/data; hermes cron"
    try:
        jobs = parse_jobs(await asyncio.to_thread(runtime.sbx, name, f"{hermes} list"))
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            return web.json_response({"error": "not_found"}, status=404)
        if job["managed"]:
            return web.json_response({"error": "managed", "message": "That one is part of how the agent works."}, status=400)
        await asyncio.to_thread(runtime.sbx, name, f"{hermes} {action} {job_id}")
    except Exception as e:
        logger.warning("job %s: %s: %s", action, name, e)
        return web.json_response({"error": "agent_unreachable", "message": "Could not reach the agent."}, status=503)
    auth_db.write_audit_log(user["id"], f"agent_job_{action}", target_type="agent_job",
                            target_id=f"{name}:{job['name']}", ip_address=request.remote)
    return web.json_response({"ok": True})


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


# A voice note may be long (Greg, 23 Nov: "the maximum transcription time is quite low"): the page records up to
# 30 minutes at 32 kbit/s (about 7 MB), so the limit here is 100 MB, and the voice service accepts the same.
# On this server speech takes about a fifth of its length to transcribe: 30 minutes is about 6.
VOICE_MAX_BYTES = 100 * 1024 * 1024
VOICE_TIMEOUT_S = 1800
VOICE_JOB_KEEP_S = 1800
_VOICE_SUFFIXES = {"audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
                   "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac"}


def audio_suffix(content_type: str) -> Optional[str]:
    """File suffix for a browser recording's content type, or None when it is not audio we take."""
    return _VOICE_SUFFIXES.get((content_type or "").split(";", 1)[0].strip().lower())


class VoiceError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


async def _transcribe(app: web.Application, name: str, audio: bytes, suffix: str) -> str:
    """The recording's words from the voice service (with the agent's own token), or a VoiceError."""
    import aiohttp
    from gateway import voice_host
    runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
    host = voice_host.VoiceHost(runtime)
    tokens = app.setdefault("voice_tokens", {})
    if name not in tokens:
        tokens[name] = await asyncio.to_thread(host.token_for, name)
    if not tokens[name]:
        tokens.pop(name)
        raise VoiceError(503, "no_voice", f"Voice is not set up for this agent yet (scripts/logos-agent up {name}).")
    base = app.get("voice_base_url") or await asyncio.to_thread(host.base_url)
    app["voice_base_url"] = base
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="recording" + suffix, content_type="application/octet-stream")
    form.add_field("model", "whisper-1")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=VOICE_TIMEOUT_S)) as s:
            async with s.post(base + "/audio/transcriptions", data=form,
                              headers={"Authorization": f"Bearer {tokens[name]}"}) as r:
                if r.status == 401:
                    tokens.pop(name, None)          # the agent was set up again; read its token afresh next time
                if r.status == 422:
                    raise VoiceError(422, "unreadable", "That recording could not be read. Try again.")
                r.raise_for_status()
                return str((await r.json()).get("text") or "").strip()
    except VoiceError:
        raise
    except Exception as e:
        logger.warning("voice: transcription for %s failed: %s", name, type(e).__name__)
        raise VoiceError(503, "voice_unreachable", "The voice service did not answer.")


async def _read_recording(request: web.Request) -> tuple:
    """(bytes, suffix) of the recording sent, or a VoiceError."""
    suffix = audio_suffix(request.headers.get("Content-Type", ""))
    if suffix is None:
        raise VoiceError(415, "not_audio", "That is not a recording.")
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(65536):
        size += len(chunk)
        if size > VOICE_MAX_BYTES:
            raise VoiceError(413, "too_large", "That recording is too long.")
        chunks.append(chunk)
    if not size:
        raise VoiceError(400, "empty", "Nothing was recorded.")
    return b"".join(chunks), suffix


def _voice_failed(e: VoiceError) -> web.Response:
    return web.json_response({"error": e.error, "message": e.message}, status=e.status)


async def handle_voice(request: web.Request) -> web.Response:
    """POST /api/v2/agents/{name}/voice  (body: the recording)  ->  {"text": "..."}

    The microphone button in web chat. The recording goes to the voice service with the
    agent's own token, exactly as a voice note in chat does, and only its words come back;
    the person sees them in the message box before anything is sent to the agent.

    With ``?job=1`` it answers at once with {"job": id} and works in the background, and the words are
    fetched with GET .../voice/{id}: a long recording does not hold one connection open for minutes
    (a proxy may cut a quiet one), and the words are still there if the page was reloaded meanwhile.
    """
    from gateway import voice_host
    name, info, user = await _agent_or_404(request)
    if not voice_host.uses_voice(info["definition"]):
        return web.json_response({"error": "no_voice", "message": "Voice is not switched on for this agent."},
                                 status=400)
    try:
        audio, suffix = await _read_recording(request)
        if not request.query.get("job"):
            return web.json_response({"text": await _transcribe(request.app, name, audio, suffix)})
    except VoiceError as e:
        return _voice_failed(e)
    jobs = request.app.setdefault("voice_jobs", {})
    now = time.time()
    for k in [k for k, j in jobs.items() if now - j["at"] > VOICE_JOB_KEEP_S]:
        jobs.pop(k, None)
    job = secrets.token_urlsafe(12)
    jobs[job] = {"user": user["id"], "agent": name, "state": "working", "at": now, "seconds": None}

    async def work():
        began = time.time()
        try:
            jobs[job].update(text=await _transcribe(request.app, name, audio, suffix), state="done")
        except VoiceError as e:
            jobs[job].update(state="failed", message=e.message)
        except Exception:
            logger.exception("voice: job for %s failed", name)
            jobs[job].update(state="failed", message="That did not work. Try again.")
        jobs[job]["seconds"] = round(time.time() - began, 1)
    request.app.setdefault("voice_tasks", set())
    task = asyncio.create_task(work())
    request.app["voice_tasks"].add(task)
    task.add_done_callback(request.app["voice_tasks"].discard)
    return web.json_response({"job": job}, status=202)


async def handle_voice_job(request: web.Request) -> web.Response:
    """GET /api/v2/agents/{name}/voice/{job} -> {"state": "working"|"done"|"failed", "text"?, "message"?}"""
    name, _info, user = await _agent_or_404(request)
    j = request.app.get("voice_jobs", {}).get(request.match_info["job"])
    if not j or j["user"] != user["id"] or j["agent"] != name:
        return web.json_response({"error": "unknown_job", "message": "That recording is no longer kept."}, status=404)
    out = {"state": j["state"]}
    if j["state"] == "done":
        out["text"] = j["text"]
    if j["state"] == "failed":
        out["message"] = j["message"]
    return web.json_response(out)


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
    ("logos-mail", "Mail service", "how agents read their person's email", "http://{bind}:8099/health"),
    ("logos-advisor", "Claude Advisor", "questions to Claude, approved first", "http://{bind}:8100/health"),
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


def model_progress(runtime: Any) -> dict:
    """Replies the model is writing now, from the model guard (F33): numbers only, never text."""
    from gateway import model_gateway
    gw = model_gateway.ModelGateway(runtime)
    try:
        h = json.loads(runtime.runner.http(f"http://{gw.bind_address()}:{model_gateway.GUARD_PORT}/guard/health", timeout=3))
    except Exception:
        return {"live": None}
    keys = ("seconds", "who", "thinking_chars", "answer_chars")
    return {"live": [{k: r.get(k) for k in keys} for r in (h.get("live") or []) if isinstance(r, dict)],
            "model_server_off": bool(h.get("model_server_off"))}


async def handle_model_progress(request: web.Request) -> web.Response:
    """GET /api/v2/models/progress — for the chat's "still thinking" line. Any signed-in person."""
    _user(request)
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return web.json_response({"live": None})
    return web.json_response(await asyncio.to_thread(model_progress, runtime))


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


# ── the advisor: each person approves their own agent's questions to Claude (story A1) ───────

def _advisor(request: web.Request):
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is None:
        return None, None
    from gateway import advisor_host
    return advisor_host.AdvisorHost(runtime), advisor_host.dashboard_token(runtime.env)


def _advisor_call(host, token: str, method: str, path: str, body: Optional[dict] = None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(host.base() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except OSError:
        return 503, {"error": "The advisor is not running."}


async def handle_advisor(request: web.Request) -> web.Response:
    """GET /api/v2/advisor[?person=] - the signed-in person's waiting questions, log and budget.
    Greg (admin) may also read Béré's log; approving is always the person's own."""
    user = _user(request)
    person = request.query.get("person") or user["username"]
    if person != user["username"] and user["role"] != "admin":
        return web.json_response({"error": "forbidden"}, status=403)
    host, token = _advisor(request)
    if host is None:
        return web.json_response({"error": "no_backend"}, status=503)
    status, body = await asyncio.to_thread(_advisor_call, host, token, "GET", f"/approvals/{person}")
    if status == 200:
        body["can_approve"] = person == user["username"]
        body["person"] = person
    return web.json_response(body, status=status)


async def handle_advisor_decide(request: web.Request) -> web.Response:
    """POST /api/v2/advisor/{id}/{send|decline} - only for the person the question belongs to."""
    user = _user(request)
    action = request.match_info["action"]
    if action not in ("send", "decline"):
        return web.json_response({"error": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    host, token = _advisor(request)
    if host is None:
        return web.json_response({"error": "no_backend"}, status=503)
    status, out = await asyncio.to_thread(
        _advisor_call, host, token, "POST", f"/approvals/{user['username']}/{request.match_info['rid']}/{action}",
        {"approver": user["username"], "model": str((body or {}).get("model") or "")})
    logger.info("advisor %s by %s: %s", action, user["username"], status)
    return web.json_response(out, status=status)


async def handle_advisor_budget(request: web.Request) -> web.Response:
    """POST /api/v2/advisor-budget/ask {reason}: the signed-in person asks the admin for more.
    POST /api/v2/advisor-budget/{person}/{raise|reset|no} {dollars}: the admin answers (the advisor
    itself checks that the signed-in user is its admin)."""
    user = _user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    host, token = _advisor(request)
    if host is None:
        return web.json_response({"error": "no_backend"}, status=503)
    person, action = request.match_info.get("person", ""), request.match_info["action"]
    if action == "ask":
        status, out = await asyncio.to_thread(_advisor_call, host, token, "POST",
                                              f"/approvals/{user['username']}/budget/ask",
                                              {"reason": str(body.get("reason") or "")})
    elif action in ("raise", "reset", "no") and person:
        if user["role"] != "admin":
            return web.json_response({"error": "forbidden"}, status=403)
        status, out = await asyncio.to_thread(_advisor_call, host, token, "POST", f"/approvals/{person}/budget/{action}",
                                              {"admin": user["username"], "dollars": body.get("dollars") or 0})
    else:
        return web.json_response({"error": "not_found"}, status=404)
    logger.info("advisor budget %s %s by %s: %s", action, person, user["username"], status)
    return web.json_response(out, status=status)


# ── notifications (Greg, 21 Nov) ─────────────────────────────────────────
# The Notifications tab: what changed in the brains of the agents you use, and the AI computer going off
# and back on, newest first, for a week. The page adds what lives in the browser (your agents' messages
# you have not read, from Matrix) and the noticeboard. "New" is anything after the last time you opened
# it (seen_until, kept with your login, so the phone and the desktop agree).
NOTIFY_DAYS = 7
SYNC_CHECK_SECONDS = 300           # Syncthing is asked at most every five minutes
AI_EVENTS_MAX = 60


def _ai_events_path(app: Any) -> Optional[Path]:
    runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
    if runtime is None:
        return None
    env = getattr(runtime, "env", None) or os.environ
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos" / "ai-events.json"


def ai_events(app: Any) -> List[Dict[str, Any]]:
    if "ai_events" not in app:
        events: List[Dict[str, Any]] = []
        path = _ai_events_path(app)
        try:
            loaded = json.loads(path.read_text()) if path and path.exists() else []
            events = [e for e in loaded if isinstance(e, dict) and "ts" in e and "up" in e]
        except (OSError, ValueError):
            events = []
        app["ai_events"] = events
    return app["ai_events"]


def note_ai(app: Any, up: bool) -> None:
    """Record the AI computer going off or coming back on (a change only; the first look is not one)."""
    events = ai_events(app)
    last = app.get("ai_last_up")
    app["ai_last_up"] = up
    if last is None:
        if not events or events[-1]["up"] == up:
            return
    elif last == up:
        return
    if events and events[-1]["up"] == up:
        return
    events.append({"ts": time.time(), "up": up})
    del events[:-AI_EVENTS_MAX]
    path = _ai_events_path(app)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(events))
        except OSError:
            logger.warning("notifications: could not save the AI computer's on/off record")


def _seen_until(user_id: str) -> float:
    row = auth_db.get_user_settings(user_id) if user_id else None
    try:
        raw = json.loads((row or {}).get("ui_prefs") or "{}")
        return float(raw.get("seen_until") or 0) if isinstance(raw, dict) else 0.0
    except (ValueError, TypeError):
        return 0.0


READ_KEEP = 400                    # notifications read one by one, remembered (the feed holds a week)


def _raw_ui(user_id: str) -> Dict[str, Any]:
    row = auth_db.get_user_settings(user_id) if user_id else None
    try:
        raw = json.loads((row or {}).get("ui_prefs") or "{}")
    except (ValueError, TypeError):
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _read_ids(user_id: str) -> List[str]:
    ids = _raw_ui(user_id).get("read_ids")
    return [str(i) for i in ids][-READ_KEEP:] if isinstance(ids, list) else []


def _mark_read(user_id: str, ids: List[str], everything: bool = False) -> Dict[str, Any]:
    """Notifications read one by one (a tap), or everything up to now (Mark all read)."""
    raw = _raw_ui(user_id)
    if everything:
        raw["seen_until"] = round(time.time(), 3)
        raw["read_ids"] = []
    else:
        have = [str(i) for i in raw.get("read_ids") or [] if isinstance(i, (str, int))]
        raw["read_ids"] = (have + [i for i in ids if i not in have])[-READ_KEEP:]
    auth_db.update_user_settings(user_id, ui_prefs=json.dumps(raw, sort_keys=True))
    return {"seen_until": float(raw.get("seen_until") or 0), "read_ids": raw["read_ids"]}


def _set_seen_until(user_id: str, ts: float) -> None:
    row = auth_db.get_user_settings(user_id) or {}
    try:
        raw = json.loads(row.get("ui_prefs") or "{}")
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    raw["seen_until"] = round(ts, 3)
    auth_db.update_user_settings(user_id, ui_prefs=json.dumps(raw, sort_keys=True))


async def handle_notifications(request: web.Request) -> web.Response:
    """GET /api/v2/notifications — {"items": [...], "seen_until": ts}.

    Each item has a ``type`` (Greg, 21 Nov), so what needs you stands apart from what merely happened:
      * ``needs``: something to do (a brain page with two copies to merge);
      * ``problem``: something wrong right now (the AI computer off, an agent not running);
      * ``update``: what happened (brain changes, the AI computer back on, an earlier outage).
    The page adds ``needs`` (requests, Advisor questions and budget asks) and ``message`` (unread chat)."""
    from gateway import brain_reader
    user = _user(request)
    now = time.time()
    since = now - NOTIFY_DAYS * 86400
    items: List[Dict[str, Any]] = []
    agents = await _visible_agents(request, user["username"])
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if runtime is not None:
        names = sorted(agents)
        listings = await asyncio.gather(*(asyncio.to_thread(brain_reader.list_pages, runtime, n) for n in names),
                                        return_exceptions=True)
        for name, listing in zip(names, listings):
            if not isinstance(listing, dict):
                continue
            shown = str(agents[name]["definition"].get("display_name") or name)
            for c in listing.get("changes") or []:
                ts = float(c.get("when") or 0)
                if ts < since:
                    continue
                path = str(c.get("path") or "")
                items.append({"id": f"brain:{name}:{path}:{int(ts)}", "kind": "brain", "ts": ts, "agent": name,
                              "who": shown, "title": str(c.get("title") or path),
                              "text": str(c.get("summary") or ""), "added": c.get("added") or 0,
                              "removed": c.get("removed") or 0,
                              "link": f"#/brain/{name}/" + "/".join(urllib.parse.quote(p) for p in path.split("/")),
                              "type": "update"})
            for c in listing.get("conflicts") or []:
                path = str(c.get("path") or "")
                items.append({"id": f"conflict:{name}:{path}", "kind": "conflict", "type": "needs",
                              "ts": float(c.get("modified") or now), "agent": name, "who": shown,
                              "title": f"Two copies of {c.get('title') or c.get('of') or path}",
                              "text": "Changed on a phone and here before they synced. Keep what you want, delete the copy.",
                              "link": f"#/brain/{name}/" + "/".join(urllib.parse.quote(p) for p in path.split("/"))})
    # Agents that should be running and are not: a problem until they are back.
    backend = request.app.get("sandbox_backend")
    try:
        states = {st.name: st for st in await asyncio.to_thread(backend.list)} if backend else {}
    except Exception:
        states = {}
    for name, a in agents.items():
        st = states.get(name)
        if states and not (st and st.running) and a["definition"].get("role") != "test":   # as the agent list says
            items.append({"id": f"down:{name}", "kind": "agent_down", "type": "problem", "ts": now, "agent": name,
                          "title": f"{a['definition'].get('display_name') or name} is not running",
                          "text": "It cannot answer or run its jobs. Greg can start it again.", "link": f"#/agents/{name}"})
    # Phones that have fallen behind on a brain (Syncthing asleep on Android): a problem (Greg, 21 Nov).
    if runtime is not None:
        cache = request.app.setdefault("sync_behind", {})
        if now - cache.get("at", 0) > SYNC_CHECK_SECONDS:
            from gateway import sync_status
            try:
                cache["rows"] = await asyncio.to_thread(sync_status.behind, runtime.runner, now)
            except Exception:
                cache["rows"] = []
            cache["at"] = now
        for b in cache.get("rows") or []:
            if b["agent"] not in agents:
                continue
            shown = str(agents[b["agent"]]["definition"].get("display_name") or b["agent"])
            items.append({"id": f"sync:{b['agent']}:{b['device']}", "kind": "sync", "type": "problem", "ts": now,
                          "agent": b["agent"], "device": b["device"], "last_seen": b["last_seen"], "missing": b["missing"],
                          "title": f"{b['device']} is behind on {shown}'s brain",
                          "text": "Open Syncthing on it, and set its battery use to Unrestricted so Android stops pausing it."})
    events = ai_events(request.app)
    for e in events:
        if e["ts"] >= since:
            current = e is events[-1] and not e["up"]               # still off: a problem, else history
            items.append({"id": f"ai:{int(e['ts'])}", "kind": "ai", "ts": e["ts"], "up": bool(e["up"]),
                          "type": "problem" if current else "update",
                          "title": "AI computer back on" if e["up"] else "AI computer off",
                          "text": "The agents answer again." if e["up"] else
                          ("Messages wait until it is back." if current else "It came back on.")})
    # Someone at the house speaker who said they were you, with a voice that was not yours (impostor.py).
    from gateway import impostor
    items.extend(await asyncio.to_thread(impostor.notifications_for, user["username"], now))
    items.sort(key=lambda i: -i["ts"])
    seen = await asyncio.to_thread(_seen_until, user["id"])
    read = await asyncio.to_thread(_read_ids, user["id"])
    # F42: a widget whose last check needs someone (a service down, a disk full) says so here too.
    w = request.app.get("widgets")
    if w is not None:
        items.extend(w.needs(user.get("role") == "admin"))
        items.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    # Whoever manages Logos is reminded when an update is ready (Greg, 23 Nov); the others cannot install it.
    if user.get("role") == "admin":
        from gateway import updates
        ready = updates.reminder(request.app)
        if ready:
            items.append(ready)
            items.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    return web.json_response({"items": items[:60], "seen_until": seen, "read_ids": read})


async def handle_notifications_seen(request: web.Request) -> web.Response:
    """POST /api/v2/notifications/seen — everything up to now has been seen."""
    user = _user(request)
    ts = time.time()
    await asyncio.to_thread(_set_seen_until, user["id"], ts)
    return web.json_response({"seen_until": ts})


async def handle_notifications_read(request: web.Request) -> web.Response:
    """POST /api/v2/notifications/read {ids: [...]} (one tapped) or {all: true} (Mark all read)."""
    user = _user(request)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "bad_request"}, status=400)
    if body.get("all") is True:
        return web.json_response(await asyncio.to_thread(_mark_read, user["id"], [], True))
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 100 or not all(isinstance(i, str) and 0 < len(i) <= 200 for i in ids):
        return web.json_response({"error": "bad_request"}, status=400)
    return web.json_response(await asyncio.to_thread(_mark_read, user["id"], ids))


async def _watch_ai(app: web.Application) -> None:
    """Check the AI computer every minute, so its going off or on is noticed with no one looking."""
    async def loop() -> None:
        while True:
            try:
                runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
                if runtime is not None:
                    for name in await asyncio.to_thread(runtime.defined):
                        definition = await asyncio.to_thread(runtime.load, name)
                        if await model_server_up(app, definition) is not None:
                            break                        # one server is enough: there is one AI computer
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("notifications: AI computer check failed: %s", exc)
            await asyncio.sleep(60)
    app["ai_watch"] = asyncio.create_task(loop())

    async def stop(_app: web.Application) -> None:
        _app["ai_watch"].cancel()
    app.on_cleanup.append(stop)


# ── Home's widgets (Greg, 22 Nov): Coming up, Lights, Weather ─────────────────────────────────

WEATHER_CACHE_SECONDS = 20 * 60


async def handle_home_coming(request: web.Request) -> web.Response:
    """GET /api/v2/home/coming — your calendar from the day after tomorrow to two weeks ahead."""
    from gateway import home
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    name = home.own_agent(agents, user["username"])
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not name or runtime is None:
        return web.json_response({"error": "no_calendar"})
    out = await asyncio.to_thread(home.calendar_coming, runtime, name, agents[name]["definition"], user["username"])
    return web.json_response(out)


async def handle_home_lights(request: web.Request) -> web.Response:
    """GET /api/v2/home/lights — rooms and how many lights are on; POST {targets, on} switches them.
    Through the house service as the household agent (or your own), with its limits."""
    from gateway import home
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    name = home.house_agent(agents, user["username"])
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not name or runtime is None:
        return web.json_response({"error": "no_house"})
    if request.method == "POST":
        try:
            body = await request.json()
            targets = [str(t)[:60] for t in body["targets"]][:20]
            on = body["on"]
            level = body.get("brightness")
            if not targets or not isinstance(on, bool):
                raise ValueError
            if level is not None and (isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 100):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "bad_request"}, status=400)
        out = await asyncio.to_thread(home.set_lights, runtime, name, targets, on, None, level)
        if out.get("error"):
            return web.json_response(out, status=502)
    return web.json_response(await asyncio.to_thread(home.lights, runtime, name))


async def handle_home_lists(request: web.Request) -> web.Response:
    """GET /api/v2/home/lists — the shared lists as they are now, in Home Assistant."""
    from gateway import home
    user = _user(request)
    agents = await _visible_agents(request, user["username"])
    name = home.house_agent(agents, user["username"])
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not name or runtime is None:
        return web.json_response({"error": "no_house"})
    return web.json_response(await asyncio.to_thread(home.lists, runtime, name))


async def handle_home_lists_tick(request: web.Request) -> web.Response:
    """POST /api/v2/home/lists/tick {list, item, done?} — tick an item off a shared list in Home Assistant, or
    (done: false) put it back. Through the house service as the household agent (or your own), with its limits."""
    from gateway import home
    user = _user(request)
    try:
        body = await request.json()
        list_name, item = str(body["list"]).strip()[:80], str(body["item"]).strip()[:200]
        done = body.get("done", True)
        if not list_name or not item or not isinstance(done, bool):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    agents = await _visible_agents(request, user["username"])
    name = home.house_agent(agents, user["username"])
    runtime = agent_chat.runtime_of(request.app.get("sandbox_backend"))
    if not name or runtime is None:
        return web.json_response({"error": "no_house"}, status=503)
    out = await asyncio.to_thread(home.tick_item, runtime, name, list_name, item, done)
    return web.json_response(out, status=200 if out.get("ok") else 409)


async def handle_weather(request: web.Request) -> web.Response:
    """GET /api/v2/weather — today and tomorrow where you chose (Settings: weather_place)."""
    from gateway import home
    user = _user(request)
    place = (await asyncio.to_thread(prefs_of, user["id"])).get("weather_place")
    if not place:
        return web.json_response({"error": "no_place"})
    key = (round(float(place["lat"]), 2), round(float(place["lon"]), 2))
    cache = request.app.setdefault("weather_cache", {})
    if key in cache and time.time() - cache[key][0] < WEATHER_CACHE_SECONDS:
        return web.json_response({**cache[key][1], "place": place["name"]})
    try:
        out = await asyncio.to_thread(home.weather, *key)
    except Exception:
        return web.json_response({"error": "weather_unavailable", "place": place["name"]})
    cache[key] = (time.time(), out)
    return web.json_response({**out, "place": place["name"]})


async def handle_weather_places(request: web.Request) -> web.Response:
    """GET /api/v2/weather/places?q=Brighton — places to pick from (name, rounded lat/lon)."""
    from gateway import home
    _user(request)
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"places": []})
    try:
        return web.json_response({"places": await asyncio.to_thread(home.places, q)})
    except Exception:
        return web.json_response({"error": "places_unavailable", "places": []})


def register(app: web.Application) -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_get("/api/v2/notifications", handle_notifications)
    app.router.add_post("/api/v2/notifications/seen", require_csrf(handle_notifications_seen))
    app.router.add_post("/api/v2/notifications/read", require_csrf(handle_notifications_read))
    app.on_startup.append(_watch_ai)
    app.router.add_post("/api/v2/agents/{name}/voice", require_csrf(handle_voice))
    app.router.add_get("/api/v2/agents/{name}/voice/{job}", handle_voice_job)
    app.router.add_get("/api/v2/agents/{name}/status", handle_agent_status)
    app.router.add_post("/api/v2/agents/{name}/jobs/{job}/cancel", require_csrf(handle_job_cancel))
    app.router.add_post("/api/v2/agents/{name}/jobs/{job}/{action}", require_csrf(handle_job_action))
    app.router.add_get("/api/v2/agents/{name}/routines", handle_routines)
    app.router.add_put("/api/v2/agents/{name}/routines", require_csrf(handle_routines))
    app.router.add_get("/api/v2/people", handle_people)
    app.router.add_get("/api/v2/activity", handle_activity)
    app.router.add_get("/api/v2/home", handle_home)
    app.router.add_get("/api/v2/home/coming", handle_home_coming)
    app.router.add_get("/api/v2/home/lights", handle_home_lights)
    app.router.add_post("/api/v2/home/lights", require_csrf(handle_home_lights))
    app.router.add_get("/api/v2/home/lists", handle_home_lists)
    app.router.add_post("/api/v2/home/lists/tick", require_csrf(handle_home_lists_tick))
    app.router.add_get("/api/v2/weather", handle_weather)
    app.router.add_get("/api/v2/weather/places", handle_weather_places)
    app.router.add_get("/api/v2/model", handle_model)
    app.router.add_get("/api/v2/models", handle_models)
    app.router.add_get("/api/v2/models/local", handle_local_model)
    app.router.add_get("/api/v2/models/progress", handle_model_progress)
    app.router.add_get("/api/v2/advisor", handle_advisor)
    app.router.add_post("/api/v2/advisor/{rid}/{action}", require_csrf(handle_advisor_decide))
    app.router.add_post("/api/v2/advisor-budget/{action}", require_csrf(handle_advisor_budget))
    app.router.add_post("/api/v2/advisor-budget/{person}/{action}", require_csrf(handle_advisor_budget))
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
    app.router.add_put("/api/v2/me/look", require_csrf(handle_set_look))
    app.router.add_put("/api/v2/me/prefs", require_csrf(handle_set_prefs))
    app.router.add_put("/api/v2/agents/{name}/colour", require_csrf(handle_agent_colour))
    app.router.add_get("/api/v2/agents", handle_agents)
    app.router.add_get("/api/v2/agents/{name}/conversations", handle_conversations)
    app.router.add_get("/api/v2/agents/{name}/steps", handle_reply_steps)
    app.router.add_get("/api/v2/agents/{name}/conversations/{sid}", handle_conversation_messages)
