"""The Logos calendar service: how agents use the household's calendars (story C3).

Agents never hold a calendar password. They call this service (MCP over HTTP, one bearer token
per agent), and it reads and writes Radicale with its own account. What each agent may see is
decided here, from the agent definitions, not left to the agent:

  * ``full`` calendars: the agent sees event details and may add, move and cancel events.
    A person's agent has its person's own calendar and the shared Household calendar.
  * ``busy`` calendars: the agent sees only when someone is busy, never what or where.
    The household agent has this for each person, so it can plan without reading their diaries.

Repeating events can be read but not changed here (they are changed on the phone), so an agent
can never move or cancel a whole series by mistake.

It runs in the Radicale image (which has vobject and time zone data), on a private Docker network
with the Radicale container. Run:  python calendar_service.py  (config from $CALENDAR_CONFIG)
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

PROTOCOL_VERSION = "2025-06-18"
MAX_RANGE_DAYS = 92
MAX_TEXT = 1000
DAV, CALDAV = "DAV:", "urn:ietf:params:xml:ns:caldav"
NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,200}\.ics$")


class Refused(Exception):
    """Something the service will not do. ``str()`` is written for the agent to read."""


# ── times ────────────────────────────────────────────────────────────────────

def parse_when(text: str, tz: ZoneInfo) -> Tuple[Any, bool]:
    """'2026-11-21' -> (date, True); '2026-11-21 19:00' or ISO with offset -> (aware datetime, False)."""
    s = str(text or "").strip().replace("T", " ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return dt.date.fromisoformat(s), True
    try:
        when = dt.datetime.fromisoformat(s)
    except ValueError:
        raise Refused(f"'{text}' is not a date or time. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when, False


def as_utc(when: Any, tz: ZoneInfo) -> dt.datetime:
    if isinstance(when, dt.datetime):
        return (when if when.tzinfo else when.replace(tzinfo=tz)).astimezone(dt.timezone.utc)
    return dt.datetime.combine(when, dt.time(), tz).astimezone(dt.timezone.utc)


def show(when: Any, tz: ZoneInfo) -> str:
    if isinstance(when, dt.datetime):
        return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return when.isoformat()


def vutc(when: dt.datetime) -> dt.datetime:
    """The same instant in vobject's own UTC, which it writes as ...Z (it cannot name Python's)."""
    from vobject.icalendar import utc
    return when.astimezone(dt.timezone.utc).replace(tzinfo=utc)


def stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def window(start: str, end: str, tz: ZoneInfo) -> Tuple[dt.datetime, dt.datetime]:
    a, a_day = parse_when(start, tz)
    b, b_day = parse_when(end or start, tz)
    a, b = as_utc(a, tz), as_utc(b, tz)
    if b_day:
        b += dt.timedelta(days=1)                       # an end date includes that whole day
    if b <= a:
        raise Refused("The end must be after the start.")
    if b - a > dt.timedelta(days=MAX_RANGE_DAYS):
        raise Refused(f"Ask for at most {MAX_RANGE_DAYS} days at a time.")
    return a, b


# ── iCalendar ────────────────────────────────────────────────────────────────

def esc(text: str) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    out, cur = [], line.encode()
    while len(cur) > 75:
        cut = 75
        while (cur[cut] & 0xC0) == 0x80:                # never split a UTF-8 character
            cut -= 1
        out.append(cur[:cut].decode())
        cur = b" " + cur[cut:]
    out.append(cur.decode())
    return "\r\n".join(out)


def build_event(uid: str, title: str, start: Any, end: Any, all_day: bool, location: str = "",
                notes: str = "", now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Logos//calendar service//EN", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{stamp(now)}"]
    if all_day:
        lines += [f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"]
    else:
        lines += [f"DTSTART:{stamp(start)}", f"DTEND:{stamp(end)}"]
    lines.append(f"SUMMARY:{esc(title)}")
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if notes:
        lines.append(f"DESCRIPTION:{esc(notes)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def events_in(ics: str) -> List[Dict[str, Any]]:
    """The VEVENTs in a calendar object (expanded instances included), as plain dicts."""
    import vobject
    out = []
    for cal in vobject.readComponents(ics):
        for ev in cal.contents.get("vevent", []):
            def val(name, default=None):
                return ev.contents[name][0].value if name in ev.contents else default
            start = val("dtstart")
            end = val("dtend")
            if end is None:
                dur = val("duration")
                end = start + dur if dur else (start + dt.timedelta(days=1) if not isinstance(start, dt.datetime) else start)
            out.append({"uid": val("uid", ""), "title": val("summary", "") or "", "start": start, "end": end,
                        "all_day": not isinstance(start, dt.datetime),
                        "location": val("location", "") or "", "notes": val("description", "") or "",
                        "repeats": "rrule" in ev.contents or "recurrence_id" in ev.contents,
                        "transparent": str(val("transp", "")).upper() == "TRANSPARENT",
                        "cancelled": str(val("status", "")).upper() == "CANCELLED"})
    return out


# ── CalDAV ───────────────────────────────────────────────────────────────────

class CalDAV:
    def __init__(self, base: str, user: str, password: str, http: Optional[Callable] = None):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.http = http or self._urllib

    @staticmethod
    def _urllib(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def call(self, method: str, path: str, body: str = "", **headers: str) -> Tuple[int, Dict[str, str], bytes]:
        h = {"Authorization": self.auth, **{k.replace("_", "-"): v for k, v in headers.items()}}
        return self.http(method, self.base + path, h, body.encode() if body else None)

    def query(self, path: str, start: dt.datetime, end: dt.datetime) -> List[Tuple[str, str]]:
        """(name, calendar data) of every event in ``path`` overlapping the window, repeats expanded."""
        s, e = stamp(start), stamp(end)
        body = (f'<?xml version="1.0" encoding="utf-8"?><C:calendar-query xmlns:D="DAV:" xmlns:C="{CALDAV}">'
                f'<D:prop><D:getetag/><C:calendar-data><C:expand start="{s}" end="{e}"/></C:calendar-data></D:prop>'
                f'<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
                f'<C:time-range start="{s}" end="{e}"/></C:comp-filter></C:comp-filter></C:filter></C:calendar-query>')
        status, _, raw = self.call("REPORT", path, body, Depth="1", Content_Type="application/xml; charset=utf-8")
        if status != 207:
            raise Refused(f"The calendar server did not answer ({status}).")
        found = []
        for resp in ET.fromstring(raw).iter(f"{{{DAV}}}response"):
            href = resp.findtext(f"{{{DAV}}}href") or ""
            data = resp.find(f".//{{{CALDAV}}}calendar-data")
            if data is not None and data.text:
                found.append((urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]), data.text))
        return found

    def get(self, path: str) -> Tuple[str, str]:
        status, headers, raw = self.call("GET", path)
        if status == 404:
            raise Refused("There is no such event (it may have been changed or deleted on a phone).")
        if status != 200:
            raise Refused(f"The calendar server did not answer ({status}).")
        etag = next((v for k, v in headers.items() if k.lower() == "etag"), "")
        return raw.decode(), etag

    def put(self, path: str, ics: str, etag: str = "") -> None:
        cond = {"If_Match": etag} if etag else {"If_None_Match": "*"}
        status, _, _ = self.call("PUT", path, ics, Content_Type="text/calendar; charset=utf-8", **cond)
        if status == 412:
            raise Refused("Someone changed this event meanwhile. Read it again, then retry.")
        if status not in (200, 201, 204):
            raise Refused(f"The calendar server refused the change ({status}).")

    def delete(self, path: str, etag: str) -> None:
        status, _, _ = self.call("DELETE", path, **({"If_Match": etag} if etag else {}))
        if status == 412:
            raise Refused("Someone changed this event meanwhile. Read it again, then retry.")
        if status not in (200, 204, 404):
            raise Refused(f"The calendar server refused the change ({status}).")


# ── the service ──────────────────────────────────────────────────────────────

class Service:
    """``agents``: name -> {"token", "timezone", "full": {key: {"path", "label"}}, "busy": {key: {"paths", "label"}}}."""

    def __init__(self, dav: CalDAV, agents: Dict[str, Dict[str, Any]], log: Callable[[str], None] = print,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        self.dav, self.agents, self.log, self.clock = dav, agents, log, clock

    def agent_for_token(self, token: str) -> Optional[str]:
        for name, a in self.agents.items():
            if token and a.get("token") and hmac.compare_digest(token.encode(), a["token"].encode()):
                return name
        return None

    def tz(self, agent: str) -> ZoneInfo:
        return ZoneInfo(self.agents[agent].get("timezone") or "UTC")

    def _full(self, agent: str, key: str) -> Dict[str, str]:
        # Every change goes through here: a person's reader key (the dashboard's Home) changes nothing.
        if self.agents[agent].get("read_only"):
            raise Refused("This is a read-only view of the calendars: nothing can be changed with it.")
        full = self.agents[agent].get("full") or {}
        if key not in full:
            raise Refused(f"'{key}' is not a calendar you can change. Yours: {', '.join(full) or 'none'}.")
        return full[key]

    def _target(self, agent: str, event_id: str) -> Tuple[str, str]:
        key, _, name = str(event_id or "").partition("/")
        cal = self._full(agent, key)
        if not NAME_RE.match(name):
            raise Refused(f"'{event_id}' is not an event id. Use the id list_events gives.")
        return key, cal["path"] + urllib.parse.quote(name)

    # tools

    def calendars(self, agent: str) -> Dict[str, Any]:
        a = self.agents[agent]
        access = "read only" if a.get("read_only") else "read and change"
        return {"calendars": [{"name": k, "shows": v["label"], "access": access} for k, v in (a.get("full") or {}).items()]
                + [{"name": k, "shows": v["label"], "access": "busy times only"} for k, v in (a.get("busy") or {}).items()],
                "timezone": a.get("timezone") or "UTC"}

    def list_events(self, agent: str, start: str, end: str = "") -> Dict[str, Any]:
        tz = self.tz(agent)
        a, b = window(start, end, tz)
        events, busy = [], []
        for key, cal in (self.agents[agent].get("full") or {}).items():
            for name, ics in self.dav.query(cal["path"], a, b):
                for ev in events_in(ics):
                    if ev["cancelled"]:
                        continue
                    events.append({"id": f"{key}/{name}", "calendar": key, "title": ev["title"],
                                   "start": show(ev["start"], tz), "end": show(ev["end"], tz),
                                   "all_day": ev["all_day"], "location": ev["location"],
                                   "notes": ev["notes"][:300], "repeats": ev["repeats"]})
        for key, cal in (self.agents[agent].get("busy") or {}).items():
            for start_, end_ in self._busy(cal["paths"], a, b):
                busy.append({"who": cal["label"], "busy_from": show(start_, tz), "busy_until": show(end_, tz)})
        events.sort(key=lambda e: (e["start"], e["title"]))
        busy.sort(key=lambda x: (x["busy_from"], x["who"]))
        self.log(f"{agent} list_events {a:%Y-%m-%d}..{b:%Y-%m-%d}: {len(events)} events, {len(busy)} busy")
        out: Dict[str, Any] = {"from": show(a, tz), "until": show(b, tz), "events": events}
        if self.agents[agent].get("busy"):
            out["busy"] = busy
        return out

    def _busy(self, paths: List[str], a: dt.datetime, b: dt.datetime) -> List[Tuple[dt.datetime, dt.datetime]]:
        spans = []
        for path in paths:
            for _, ics in self.dav.query(path, a, b):
                for ev in events_in(ics):
                    if ev["cancelled"] or ev["transparent"]:
                        continue
                    if ev["all_day"]:
                        continue                    # an all-day entry (a birthday, "WFH") is not "busy"
                    spans.append((as_utc(ev["start"], dt.timezone.utc), as_utc(ev["end"], dt.timezone.utc)))
        return merge(spans)

    def add_event(self, agent: str, calendar: str, title: str, start: str, end: str = "",
                  location: str = "", notes: str = "") -> Dict[str, Any]:
        cal = self._full(agent, calendar)
        title = str(title or "").strip()
        if not title:
            raise Refused("An event needs a title.")
        tz = self.tz(agent)
        s, s_day = parse_when(start, tz)
        if end:
            e, e_day = parse_when(end, tz)
            if e_day != s_day:
                raise Refused("Give the start and end both as dates (all day) or both as times.")
        else:
            e = s + dt.timedelta(hours=1) if not s_day else s
        if s_day:
            e = e + dt.timedelta(days=1)            # all-day: the end date is included
        if e <= s:
            raise Refused("The end must be after the start.")
        uid = f"{uuid.uuid4()}@logos"
        name = f"{uid}.ics"
        self.dav.put(cal["path"] + urllib.parse.quote(name),
                     build_event(uid, title[:MAX_TEXT], s, e, s_day, str(location or "")[:MAX_TEXT],
                                 str(notes or "")[:MAX_TEXT], self.clock()))
        self.log(f"{agent} add_event {calendar}/{name}")
        return {"added": {"id": f"{calendar}/{name}", "calendar": calendar, "title": title,
                          "start": show(s, tz), "end": show(e - dt.timedelta(days=1) if s_day else e, tz),
                          "all_day": s_day}}

    def _load_single(self, path: str):
        import vobject
        ics, etag = self.dav.get(path)
        cal = vobject.readOne(ics)
        events = cal.contents.get("vevent", [])
        if len(events) != 1 or "rrule" in events[0].contents or "recurrence_id" in events[0].contents:
            raise Refused("This is a repeating event. Change or cancel it on the phone; tell your person so.")
        return cal, events[0], etag

    def update_event(self, agent: str, event_id: str, title: str = "", start: str = "", end: str = "",
                     location: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
        key, path = self._target(agent, event_id)
        cal, ev, etag = self._load_single(path)
        tz = self.tz(agent)
        old_s, old_e = ev.dtstart.value, (ev.dtend.value if "dtend" in ev.contents else None)
        all_day = not isinstance(old_s, dt.datetime)
        length = (old_e - old_s) if old_e is not None else (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1))
        if start:
            s, s_day = parse_when(start, tz)
            if s_day != all_day:
                raise Refused("Keep the event all-day or timed as it is; to switch, cancel it and add a new one.")
        else:
            s = old_s
        if end:
            e, e_day = parse_when(end, tz)
            if e_day != all_day:
                raise Refused("Give the end the same way as the start (a date, or a date and time).")
            e = e + dt.timedelta(days=1) if all_day else e
        else:
            e = s + length
        if e <= s:
            raise Refused("The end must be after the start.")
        if "dtend" not in ev.contents:
            ev.contents.pop("duration", None)
            ev.add("dtend")
        for prop, value in ((ev.dtstart, s), (ev.dtend, e)):
            if all_day:
                prop.value = value
                prop.params["VALUE"] = ["DATE"]
            else:
                prop.params.pop("TZID", None)       # written in UTC from now on
                prop.value = vutc(as_utc(value, tz))
        if title:
            if "summary" in ev.contents:
                ev.summary.value = str(title)[:MAX_TEXT]
            else:
                ev.add("summary").value = str(title)[:MAX_TEXT]
        for field, value in (("location", location), ("description", notes)):
            if value is None:
                continue
            if value == "":
                ev.contents.pop(field, None)
            elif field in ev.contents:
                ev.contents[field][0].value = str(value)[:MAX_TEXT]
            else:
                ev.add(field).value = str(value)[:MAX_TEXT]
        if "dtstamp" in ev.contents:
            ev.dtstamp.value = vutc(self.clock())
        if "sequence" in ev.contents:
            ev.sequence.value = str(int(ev.sequence.value or 0) + 1)
        self.dav.put(path, cal.serialize(), etag)
        self.log(f"{agent} update_event {event_id}")
        return {"updated": {"id": event_id, "title": str(ev.summary.value) if "summary" in ev.contents else "",
                            "start": show(s, tz), "end": show(e - dt.timedelta(days=1) if all_day else e, tz),
                            "all_day": all_day}}

    def cancel_event(self, agent: str, event_id: str) -> Dict[str, Any]:
        key, path = self._target(agent, event_id)
        _, ev, etag = self._load_single(path)
        title = str(ev.summary.value) if "summary" in ev.contents else ""
        self.dav.delete(path, etag)
        self.log(f"{agent} cancel_event {event_id}")
        return {"cancelled": {"id": event_id, "title": title}}

    def find_free_time(self, agent: str, start: str, end: str, minutes: int = 60,
                       earliest: str = "08:00", latest: str = "21:00") -> Dict[str, Any]:
        """Times in the window when everyone this agent can see is free, within earliest..latest each day."""
        tz = self.tz(agent)
        a, b = window(start, end, tz)
        try:
            minutes = max(15, min(int(minutes or 60), 12 * 60))
            lo, hi = dt.time.fromisoformat(earliest or "08:00"), dt.time.fromisoformat(latest or "21:00")
        except ValueError:
            raise Refused("Give minutes as a number and earliest/latest as HH:MM.")
        busy: List[Tuple[dt.datetime, dt.datetime]] = []
        for cal in (self.agents[agent].get("full") or {}).values():
            busy += self._busy([cal["path"]], a, b)
        for cal in (self.agents[agent].get("busy") or {}).values():
            busy += self._busy(cal["paths"], a, b)
        busy = merge(busy)
        slots, day = [], a.astimezone(tz).date()
        while dt.datetime.combine(day, dt.time(), tz) < b and len(slots) < 12:
            s = max(as_utc(dt.datetime.combine(day, lo, tz), tz), a, self.clock())
            e = min(as_utc(dt.datetime.combine(day, hi, tz), tz), b)
            for free_s, free_e in gaps(busy, s, e):
                if free_e - free_s >= dt.timedelta(minutes=minutes):
                    slots.append({"from": show(free_s, tz), "until": show(free_e, tz)})
            day += dt.timedelta(days=1)
        who = [v["label"] for v in (self.agents[agent].get("busy") or {}).values()]
        self.log(f"{agent} find_free_time {a:%Y-%m-%d}..{b:%Y-%m-%d}: {len(slots)} slots")
        return {"free": slots[:12], "checked": [v["label"] for v in (self.agents[agent].get("full") or {}).values()] + who,
                "minutes": minutes}


def merge(spans: List[Tuple[dt.datetime, dt.datetime]]) -> List[Tuple[dt.datetime, dt.datetime]]:
    out: List[List[dt.datetime]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def gaps(busy: List[Tuple[dt.datetime, dt.datetime]], start: dt.datetime, end: dt.datetime):
    cur = start
    for s, e in busy:
        if e <= cur:
            continue
        if s >= end:
            break
        if s > cur:
            yield cur, min(s, end)
        cur = max(cur, e)
    if cur < end:
        yield cur, end


# ── MCP over HTTP ────────────────────────────────────────────────────────────

WHEN = "A date 'YYYY-MM-DD' (all day) or a local time 'YYYY-MM-DD HH:MM'."
TOOLS = [
    {"name": "list_calendars",
     "description": "Which calendars you can use, and whether you may change them or only see busy times.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_events",
     "description": ("Events between two dates (repeating events included), with their ids. Where you may only "
                     "see busy times, you get when someone is busy, never what or where."),
     "inputSchema": {"type": "object", "properties": {
         "start": {"type": "string", "description": "First day or time, e.g. '2026-11-21'."},
         "end": {"type": "string", "description": "Last day (included) or end time. Default: the start day."}},
         "required": ["start"]}},
    {"name": "add_event",
     "description": "Add an event to a calendar you may change ('personal' or 'household').",
     "inputSchema": {"type": "object", "properties": {
         "calendar": {"type": "string", "description": "A calendar from list_calendars that you may change."},
         "title": {"type": "string"},
         "start": {"type": "string", "description": WHEN},
         "end": {"type": "string", "description": WHEN + " Default: one hour after the start, or the same day."},
         "location": {"type": "string"}, "notes": {"type": "string"}},
         "required": ["calendar", "title", "start"]}},
    {"name": "update_event",
     "description": ("Move or rename an event, by the id from list_events. Leave out what stays the same. "
                     "Moving only the start keeps its length. Repeating events cannot be changed here."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "title": {"type": "string"},
         "start": {"type": "string", "description": WHEN}, "end": {"type": "string", "description": WHEN},
         "location": {"type": "string", "description": "'' removes it."},
         "notes": {"type": "string", "description": "'' removes them."}},
         "required": ["id"]}},
    {"name": "cancel_event",
     "description": "Delete an event, by the id from list_events. Repeating events cannot be cancelled here.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "find_free_time",
     "description": ("Times when every calendar you can see is free, for at least `minutes`, between "
                     "`earliest` and `latest` each day. Use it to suggest times; it books nothing."),
     "inputSchema": {"type": "object", "properties": {
         "start": {"type": "string", "description": "First day, e.g. '2026-11-21'."},
         "end": {"type": "string", "description": "Last day (included)."},
         "minutes": {"type": "integer", "description": "How long, in minutes. Default 60."},
         "earliest": {"type": "string", "description": "HH:MM, default 08:00."},
         "latest": {"type": "string", "description": "HH:MM, default 21:00."}},
         "required": ["start", "end"]}},
]


def handle_rpc(svc: Service, agent: str, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        return None
    if method == "initialize":
        version = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result: Any = {"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "logos-calendar", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = req.get("params") or {}
        args = params.get("arguments") or {}
        name = params.get("name")
        calls = {
            "list_calendars": lambda: svc.calendars(agent),
            "list_events": lambda: svc.list_events(agent, args.get("start"), args.get("end") or ""),
            "add_event": lambda: svc.add_event(agent, args.get("calendar"), args.get("title"), args.get("start"),
                                               args.get("end") or "", args.get("location") or "", args.get("notes") or ""),
            "update_event": lambda: svc.update_event(agent, args.get("id"), args.get("title") or "",
                                                     args.get("start") or "", args.get("end") or "",
                                                     args.get("location"), args.get("notes")),
            "cancel_event": lambda: svc.cancel_event(agent, args.get("id")),
            "find_free_time": lambda: svc.find_free_time(agent, args.get("start"), args.get("end"),
                                                         args.get("minutes") or 60, args.get("earliest") or "08:00",
                                                         args.get("latest") or "21:00"),
        }
        if name not in calls:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            payload = calls[name]()
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}
        except Refused as e:
            svc.log(f"{agent} {name} refused: {e}")
            result = {"content": [{"type": "text", "text": f"Not done: {e}"}], "isError": True}
        except Exception as e:                       # never leak a traceback to the agent
            svc.log(f"{agent} {name} FAILED: {type(e).__name__}: {e}")
            result = {"content": [{"type": "text", "text": "Not done: the calendar service hit an error."}],
                      "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: Any = None, headers: Tuple[Tuple[str, str], ...] = ()):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok", "agents": sorted(svc.agents)})
            self._send(405, {"error": "use POST"}, (("Allow", "POST"),))

        def do_DELETE(self):
            self._send(200, {})

        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                return self._send(404, {"error": "not found"})
            auth = self.headers.get("Authorization", "")
            agent = svc.agent_for_token(auth[7:].strip() if auth.lower().startswith("bearer ") else "")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
            if agent is None:
                return self._send(401, {"error": "unknown or missing token"})
            try:
                req = json.loads(raw)
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            if isinstance(req, list):
                answers = [x for x in (handle_rpc(svc, agent, r) for r in req if isinstance(r, dict)) if x]
                return self._send(200, answers) if answers else self._send(202)
            answer = handle_rpc(svc, agent, req) if isinstance(req, dict) else None
            return self._send(200, answer) if answer else self._send(202)

    return Handler


def main() -> None:
    config = json.load(open(os.environ.get("CALENDAR_CONFIG", "/data/config.json")))
    dav = CalDAV(config["radicale"], config["user"], config["password"])
    svc = Service(dav, config["agents"], log=lambda m: print(m, flush=True))
    port = int(os.environ.get("CALENDAR_PORT", "8095"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc))
    print(f"calendar service: {len(config['agents'])} agents, MCP on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
