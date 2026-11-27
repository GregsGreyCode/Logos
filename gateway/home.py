"""What the dashboard's Home screen shows (story S6.11), apart from the noticeboard.

* Today: what the person sees on their phone (their own calendar, Household, and the other
  people's), read through the calendar service with the person's own read-only key. Someone
  without a calendar account sees what their agent sees instead.
* Brain changes in the last day, from the brain's git history.

Every function blocks (it runs docker or HTTP); call it with ``asyncio.to_thread``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

RECENT_SECONDS = 24 * 3600


def own_agent(agents: Mapping[str, Mapping[str, Any]], username: str) -> Optional[str]:
    """The agent that is this person's alone (owners == [them]); else the first they may use."""
    for name, info in sorted(agents.items()):
        if (info.get("owners") or []) == [username]:
            return name
    return next(iter(sorted(agents)), None)


def today_in(timezone: str, now: Optional[float] = None) -> dt.date:
    try:
        tz = ZoneInfo(timezone or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.datetime.fromtimestamp(now or time.time(), tz).date()


def rpc_result(reply: Mapping[str, Any]) -> Dict[str, Any]:
    """The JSON a calendar tool returned, out of its MCP reply; raises on a refusal."""
    if reply.get("error"):
        raise ValueError(str(reply["error"].get("message") or "error"))
    result = reply.get("result") or {}
    text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
    if result.get("isError"):
        raise ValueError(text[:200] or "refused")
    return json.loads(text or "{}")


def _call(name: str, arguments: Mapping[str, Any]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": dict(arguments)}}).encode()


def calendar_today(runtime: Any, agent: str, definition: Mapping[str, Any], username: str = "",
                   post=None) -> Dict[str, Any]:
    """``{"date", "events", "busy", "calendars"}`` for today and tomorrow, or ``{"error"}``.

    ``calendars`` maps each calendar's key to the name people know it by."""
    from gateway import calendar_host
    host = calendar_host.CalendarHost(runtime)
    token = host.reader_token(username) if username else ""
    if not token:                                # no calendar account: what their agent sees
        if not calendar_host.uses_calendar(definition):
            return {"error": "no_calendar"}
        token = host.token_for(agent)
    if not token:
        return {"error": "no_calendar"}
    day = today_in(str(definition.get("timezone") or "UTC"))
    post = post or _post
    try:
        out = rpc_result(post(host.service_url(), _call("list_events", {
            "start": day.isoformat(), "end": (day + dt.timedelta(days=1)).isoformat()}), token))
        cals = rpc_result(post(host.service_url(), _call("list_calendars", {}), token))
    except Exception as e:                       # the calendar service is down or refused: say so
        return {"error": "calendar_unavailable", "message": str(e)[:200]}
    return {"date": day.isoformat(), "events": out.get("events") or [], "busy": out.get("busy") or [],
            "calendars": {c["name"]: c.get("shows") or c["name"] for c in cals.get("calendars") or []}}


def calendar_coming(runtime: Any, agent: str, definition: Mapping[str, Any], username: str = "",
                    days: int = 14, post=None) -> Dict[str, Any]:
    """Home's "Coming up" (Greg, 22 Nov): events from the day after tomorrow to ``days`` ahead, or ``{"error"}``."""
    from gateway import calendar_host
    host = calendar_host.CalendarHost(runtime)
    token = host.reader_token(username) if username else ""
    if not token and calendar_host.uses_calendar(definition):
        token = host.token_for(agent)
    if not token:
        return {"error": "no_calendar"}
    day = today_in(str(definition.get("timezone") or "UTC"))
    post = post or _post
    try:
        out = rpc_result(post(host.service_url(), _call("list_events", {
            "start": (day + dt.timedelta(days=2)).isoformat(), "end": (day + dt.timedelta(days=days)).isoformat()}), token))
        cals = rpc_result(post(host.service_url(), _call("list_calendars", {}), token))
    except Exception as e:
        return {"error": "calendar_unavailable", "message": str(e)[:200]}
    return {"date": day.isoformat(), "events": out.get("events") or [],
            "calendars": {c["name"]: c.get("shows") or c["name"] for c in cals.get("calendars") or []}}


# ── The Calendar screen (Béré, 27 Nov): a month or a week at a glance, read the same way as Home ──

RANGE_MAX_DAYS = 62                 # a month grid is six weeks at most; the service allows 92


def parse_range(start: str, end: str) -> Optional[tuple]:
    """``(first, last)`` dates (both included) from ``YYYY-MM-DD`` strings, or None if they are not a sensible range."""
    try:
        a, b = dt.date.fromisoformat(str(start)), dt.date.fromisoformat(str(end or start))
    except ValueError:
        return None
    if b < a or (b - a).days + 1 > RANGE_MAX_DAYS:
        return None
    return a, b


def calendar_order(key: str) -> tuple:
    """Your own first, then Household, then everyone else's by name."""
    return ({"personal": 0, "household": 1}.get(key, 2), key)


def calendar_range(runtime: Any, agent: str, definition: Mapping[str, Any], username: str, start: str, end: str,
                   post=None) -> Dict[str, Any]:
    """Every event from ``start`` to ``end`` (dates, both included) in the calendars this person may read:
    ``{"start", "end", "today", "events", "busy", "calendars": [{"key", "name", "busy_only"}]}`` or ``{"error"}``.

    Exactly what Home reads: the person's own read-only key (their calendar, Household and the other people's),
    else what their agent sees. Nothing here can change an event."""
    from gateway import calendar_host
    span = parse_range(start, end)
    if not span:
        return {"error": "bad_range"}
    host = calendar_host.CalendarHost(runtime)
    token = host.reader_token(username) if username else ""
    if not token and calendar_host.uses_calendar(definition):
        token = host.token_for(agent)
    if not token:
        return {"error": "no_calendar"}
    post = post or _post
    try:
        out = rpc_result(post(host.service_url(), _call("list_events", {
            "start": span[0].isoformat(), "end": span[1].isoformat()}), token))
        cals = rpc_result(post(host.service_url(), _call("list_calendars", {}), token))
    except Exception as e:
        return {"error": "calendar_unavailable", "message": str(e)[:200]}
    listed = []
    for c in cals.get("calendars") or []:
        key = str(c.get("name") or "")
        if key:
            listed.append({"key": key, "name": re.sub(r"\s*\(.*\)\s*$", "", str(c.get("shows") or key)),
                           "busy_only": str(c.get("access") or "").startswith("busy")})
    listed.sort(key=lambda c: calendar_order(c["key"]))
    return {"start": span[0].isoformat(), "end": span[1].isoformat(),
            "today": today_in(str(definition.get("timezone") or "UTC")).isoformat(),
            "events": out.get("events") or [], "busy": out.get("busy") or [], "calendars": listed}


# Adding, changing and cancelling from the Calendar screen (Greg, 27 Nov). The reader key changes nothing, so a
# change goes through the person's own agent (one that is theirs alone and whose calendar is theirs): the same
# rights their agent has when asked in chat, checked again by the calendar service (their own calendar and
# Household only; never someone else's; never a repeating event).
CHANGEABLE = ("personal", "household")
CHANGE_TOOLS = {"add": "add_event", "update": "update_event", "cancel": "cancel_event"}
CHANGE_FIELDS = {"add": ("calendar", "title", "start", "end", "location", "notes"),
                 "update": ("id", "title", "start", "end", "location", "notes"), "cancel": ("id",)}


def calendar_writer(agents: Mapping[str, Mapping[str, Any]], username: str) -> Optional[str]:
    """The agent a person's calendar changes go through: theirs alone, with their own calendar; else None."""
    for name, info in sorted(agents.items()):
        cal = (info.get("definition") or {}).get("calendar") or {}
        if (info.get("owners") or []) == [username] and cal.get("enabled") and cal.get("person") == username:
            return name
    return None


def change_args(action: str, body: Mapping[str, Any]) -> Dict[str, str]:
    """The arguments for one change, checked; raises ValueError in plain words."""
    if action not in CHANGE_TOOLS:
        raise ValueError("Unknown change.")
    args = {k: str(body[k]).strip()[:1000] for k in CHANGE_FIELDS[action] if body.get(k) is not None}
    if action == "add" and args.get("calendar") not in CHANGEABLE:
        raise ValueError("Events can be added to your own calendar or Household.")
    if action == "add" and not args.get("title"):
        raise ValueError("An event needs a title.")
    if action != "add" and args.get("id", "").partition("/")[0] not in CHANGEABLE:
        raise ValueError("Only events in your own calendar or Household can be changed here.")
    return args


def calendar_change(runtime: Any, agent: str, action: str, args: Mapping[str, Any], post=None) -> Dict[str, Any]:
    """Do one change with the agent's key: what the service answered, or ``{"error", "message"}``."""
    from gateway import calendar_host
    host = calendar_host.CalendarHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_calendar", "message": "Your agent has no calendar."}
    try:
        return rpc_result((post or _post)(host.service_url(), _call(CHANGE_TOOLS[action], args), token))
    except ValueError as e:                        # the service said no: its words, without "Not done: "
        return {"error": "refused", "message": re.sub(r"^Not done:\s*", "", str(e))[:300]}
    except Exception:
        return {"error": "calendar_unavailable", "message": "The calendar could not be reached just now."}


# ── Lights (Greg, 22 Nov): Home's Lights widget, through the house service as the household agent ──

def house_agent(agents: Mapping[str, Mapping[str, Any]], username: str) -> Optional[str]:
    """The agent whose house access Home uses: a shared one (the household agent) first, else your own."""
    from gateway import agent_chat, house_host
    usable = [n for n, a in agents.items() if house_host.uses_house(a.get("definition") or {})
              and agent_chat.may_use(a.get("owners"), username)]
    shared = [n for n in usable if (agents[n].get("owners") is None or len(agents[n].get("owners") or []) > 1)]
    return (shared or usable or [None])[0]


def lights(runtime: Any, agent: str, post=None) -> Dict[str, Any]:
    """Rooms with how many lights are on: ``{"rooms": [{"room", "on", "of"}]}`` or ``{"error"}``."""
    from gateway import house_host
    host = house_host.HouseHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_house"}
    try:
        out = rpc_result((post or _post)(host.url(), _call("list_lights", {}), token))
    except Exception as e:
        return {"error": "house_unavailable", "message": str(e)[:200]}
    rooms = []
    for r in out.get("rooms") or []:
        if r.get("kind") != "room":
            continue
        on, _, of = str(r.get("on") or "0 of 0").partition(" of ")
        # How bright the room is: the average of its lit lights (Greg, 22 Nov: brightness from Home).
        lit = [int(l["brightness"]) for l in r.get("lights") or [] if l.get("state") == "on" and l.get("brightness") is not None]
        rooms.append({"room": r.get("room"), "on": int(on or 0), "of": int(of or 0),
                      "brightness": round(sum(lit) / len(lit)) if lit else None})
    return {"rooms": rooms}


def set_lights(runtime: Any, agent: str, targets: List[str], on: bool, post=None, brightness: Optional[int] = None) -> Dict[str, Any]:
    from gateway import house_host
    host = house_host.HouseHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_house"}
    try:
        args: Dict[str, Any] = {"targets": targets, "on": bool(on)}
        if on and brightness is not None:
            args["brightness"] = int(brightness)
        return rpc_result((post or _post)(host.url(), _call("set_lights", args), token))
    except Exception as e:
        return {"error": "house_unavailable", "message": str(e)[:200]}


# ── The shared lists, live (Greg, 23 Nov): Home's Lists widget reads them from Home Assistant through the house service
# and can tick an item off (or put it back), instead of showing a copy refreshed every quarter of an hour.

def _item_of(raw: Any) -> Dict[str, str]:
    if isinstance(raw, dict):
        return {"text": str(raw.get("item") or ""), "due": str(raw.get("due") or ""), "details": str(raw.get("details") or "")}
    return {"text": str(raw), "due": "", "details": ""}


def lists(runtime: Any, agent: str, post=None) -> Dict[str, Any]:
    """``{"lists": [{"name", "id", "items": [{"text", "due", "details"}]}]}`` (what is still to do) or ``{"error"}``."""
    from gateway import house_host
    host = house_host.HouseHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_house"}
    try:
        out = rpc_result((post or _post)(host.url(), _call("list_items", {}), token))
    except Exception as e:
        return {"error": "house_unavailable", "message": str(e)[:200]}
    return {"lists": [{"name": str(l.get("list") or ""), "id": str(l.get("id") or ""),
                       "items": [_item_of(i) for i in l.get("open") or []]} for l in out.get("lists") or []]}


def tick_item(runtime: Any, agent: str, list_name: str, item: str, done: bool = True, post=None) -> Dict[str, Any]:
    """Tick ``item`` off a list, or (``done`` False) put it back: the house service puts a ticked item back on its list
    when it is added again. A refusal (not on the list, or two items alike) comes back in plain words."""
    from gateway import house_host
    host = house_host.HouseHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_house"}
    tool = "tick_items" if done else "add_items"
    try:
        rpc_result((post or _post)(host.url(), _call(tool, {"list": list_name, "items": [item]}), token))
    except Exception as e:
        return {"error": "refused", "message": str(e)[:200]}
    return {"ok": True}


# ── Weather (Greg, 22 Nov): Open-Meteo, no key. Only a place's rounded coordinates (about 1 km) or the
# name typed to find it leave the machine: never who is asking.

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
PLACES_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _get_json(url: str, timeout: float = 8) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "logos-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def weather(lat: float, lon: float, get=None) -> Dict[str, Any]:
    q = urllib.parse.urlencode({
        "latitude": f"{round(float(lat), 2):.2f}", "longitude": f"{round(float(lon), 2):.2f}", "timezone": "auto",
        "current": "temperature_2m,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max", "forecast_days": 2})
    d = (get or _get_json)(f"{WEATHER_URL}?{q}")
    cur, daily = d.get("current") or {}, d.get("daily") or {}
    day = lambda i: {"code": (daily.get("weather_code") or [None, None])[i], "high": (daily.get("temperature_2m_max") or [None, None])[i],
                     "low": (daily.get("temperature_2m_min") or [None, None])[i], "rain": (daily.get("precipitation_probability_max") or [None, None])[i]}
    return {"now": {"temp": cur.get("temperature_2m"), "code": cur.get("weather_code"), "day": bool(cur.get("is_day", 1))},
            "today": day(0), "tomorrow": day(1)}


def places(name: str, get=None) -> List[Dict[str, Any]]:
    q = urllib.parse.urlencode({"name": name[:80], "count": 5, "language": "en", "format": "json"})
    d = (get or _get_json)(f"{PLACES_URL}?{q}")
    return [{"name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x),
             "lat": round(float(r["latitude"]), 2), "lon": round(float(r["longitude"]), 2)}
            for r in d.get("results") or [] if "latitude" in r and "longitude" in r]


def _post(url: str, body: bytes, token: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read() or b"{}")


def recent_brain_changes(listing: Mapping[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Pages changed in the last day, newest first, from :func:`gateway.brain_reader.list_pages`."""
    now = now or time.time()
    return sorted((c for c in listing.get("changes") or [] if now - float(c.get("when") or 0) <= RECENT_SECONDS),
                  key=lambda c: -float(c.get("when") or 0))
