"""Runs before the morning briefing (story C4). What it prints goes into the agent's prompt:
today's date, today's weather (from the web reader, which asks Open-Meteo; the sandbox has no
internet) and today's events (from the calendar service, the same list_events the agent has).
Gathering the facts here, not leaving it to the model, means the briefing never says "nothing
on" because a tool call went wrong. ``briefing_check.py`` uses the same facts for the plain
version it sends when the model could not write the briefing. Standard library only; never fails."""
import datetime
import json
import urllib.request


def env():
    try:
        return dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def get_json(url, token, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def weather(e):
    """Today's weather in one line, or None."""
    try:
        return get_json(e["LOGOS_WEATHER_URL"], e["LOGOS_WEB_TOKEN"])["today"]
    except Exception:
        return None


def events(e, today):
    """Today's events, all-day first, or None when the calendar cannot be read."""
    try:
        out = get_json(e["LOGOS_CALENDAR_URL"], e["LOGOS_CALENDAR_TOKEN"], {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_events", "arguments": {"start": today.isoformat()}}})
        result = out["result"]
        if result.get("isError"):
            return None
        found = json.loads(result["content"][0]["text"]).get("events", [])
    except Exception:
        return None
    return sorted(found, key=lambda ev: (not ev.get("all_day"), ev.get("start", "")))


def when(ev):
    return "all day" if ev.get("all_day") else f"{ev['start'][-5:]} to {ev['end'][-5:]}"


def facts(today=None):
    """The lines given to the model."""
    today = today or datetime.date.today()
    e = env()
    sky, evs = weather(e), events(e, today)
    lines = [f"Today: {today:%A} {today.day} {today:%B %Y}.",
             f"Weather: {sky}." if sky else "Weather: not available this morning."]
    if evs is None:
        return lines + ["Calendar: could not be read this morning."]
    mine = [ev for ev in evs if not ev.get("read_only")]
    others = [ev for ev in evs if ev.get("read_only")]
    lines.append(f"Your person's events today ({len(mine)}):" if mine else "Your person's events today: none.")
    for ev in mine:
        where = f", at {ev['location']}" if ev.get("location") else ""
        lines.append(f"- {when(ev)}: {ev['title']}{where} ({ev['calendar']})")
    if others:
        lines.append("Other people's events today (read only; mention one only if it matters to your person's day):")
        lines += [f"- {when(ev)}: {ev['title']} ({ev['calendar']})" for ev in others]
    return lines


if __name__ == "__main__":
    print("\n".join(facts()))
