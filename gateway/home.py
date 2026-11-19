"""What the dashboard's Home screen shows (story S6.11), apart from the noticeboard.

* Today: the events of the person's own agent's calendars (their own and Household), read
  through the calendar service with that agent's own token, so Home shows exactly what the
  agent sees and nothing more.
* Brain changes in the last day, from the brain's git history.

Every function blocks (it runs docker or HTTP); call it with ``asyncio.to_thread``.
"""
from __future__ import annotations

import datetime as dt
import json
import time
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


def calendar_today(runtime: Any, agent: str, definition: Mapping[str, Any], post=None) -> Dict[str, Any]:
    """``{"date", "events", "busy"}`` for today and tomorrow in the agent's timezone, or ``{"error"}``."""
    from gateway import calendar_host
    if not calendar_host.uses_calendar(definition):
        return {"error": "no_calendar"}
    host = calendar_host.CalendarHost(runtime)
    token = host.token_for(agent)
    if not token:
        return {"error": "no_calendar"}
    day = today_in(str(definition.get("timezone") or "UTC"))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "list_events", "arguments": {"start": day.isoformat(),
                                              "end": (day + dt.timedelta(days=1)).isoformat()}}}).encode()
    post = post or _post
    try:
        out = rpc_result(post(host.service_url(), body, token))
    except Exception as e:                       # the calendar service is down or refused: say so
        return {"error": "calendar_unavailable", "message": str(e)[:200]}
    return {"date": day.isoformat(), "events": out.get("events") or [], "busy": out.get("busy") or []}


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
