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
