"""gateway/home.py: what the Home screen shows besides the noticeboard (story S6.11)."""
import json

import pytest

from gateway import home


def test_own_agent_is_the_one_that_is_yours_alone():
    agents = {"household": {"owners": ["greg", "bere"]}, "greg": {"owners": ["greg"]}}
    assert home.own_agent(agents, "greg") == "greg"
    assert home.own_agent({"household": {"owners": ["greg", "bere"]}}, "bere") == "household"
    assert home.own_agent({}, "greg") is None


def test_rpc_result_unwraps_a_tool_answer_and_raises_on_refusal():
    ok = {"result": {"content": [{"type": "text", "text": json.dumps({"events": [1]})}]}}
    assert home.rpc_result(ok) == {"events": [1]}
    with pytest.raises(ValueError):
        home.rpc_result({"result": {"isError": True, "content": [{"type": "text", "text": "Not done"}]}})
    with pytest.raises(ValueError):
        home.rpc_result({"error": {"message": "unknown tool"}})


def test_recent_brain_changes_keeps_the_last_day_newest_first():
    now = 1_000_000.0
    listing = {"changes": [{"path": "a.md", "when": now - 3600}, {"path": "b.md", "when": now - 100},
                           {"path": "old.md", "when": now - 3 * 86400}]}
    assert [c["path"] for c in home.recent_brain_changes(listing, now)] == ["b.md", "a.md"]


def test_calendar_today_asks_for_today_and_tomorrow_with_the_agents_token(monkeypatch):
    from gateway import calendar_host

    class Host:
        def __init__(self, rt):
            pass

        def token_for(self, name):
            return "tok-agent"

        def reader_token(self, name):
            return "tok-reader" if name == "greg" else ""

        def service_url(self):
            return "http://svc/mcp"

    monkeypatch.setattr(calendar_host, "CalendarHost", Host)
    monkeypatch.setattr(calendar_host, "uses_calendar", lambda d: True)
    monkeypatch.setattr(home, "today_in", lambda tz, now=None: __import__("datetime").date(2026, 11, 19))
    seen = []

    def post(url, body, token):
        req = json.loads(body)
        seen.append((token, req["params"]["name"], req["params"]["arguments"]))
        answer = ({"events": [{"title": "Dentist", "calendar": "bere"}]} if req["params"]["name"] == "list_events"
                  else {"calendars": [{"name": "personal", "shows": "Your own calendar"}, {"name": "bere", "shows": "Béré"}]})
        return {"result": {"content": [{"type": "text", "text": json.dumps(answer)}]}}

    out = home.calendar_today(object(), "greg", {"timezone": "Europe/London"}, "greg", post=post)
    assert out == {"date": "2026-11-19", "events": [{"title": "Dentist", "calendar": "bere"}], "busy": [],
                   "calendars": {"personal": "Your own calendar", "bere": "Béré"}}
    assert seen[0] == ("tok-reader", "list_events", {"start": "2026-11-19", "end": "2026-11-20"})   # the person's key
    seen.clear()
    home.calendar_today(object(), "dev", {"timezone": "UTC"}, "review", post=post)
    assert seen[0][0] == "tok-agent"             # no calendar account: what the agent sees
    failing = home.calendar_today(object(), "greg", {}, "greg", post=lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert failing["error"] == "calendar_unavailable"


# ── Home's widgets (Greg, 22 Nov) ────────────────────────────────────────────────────────────

def tool(answer):
    return {"result": {"content": [{"type": "text", "text": json.dumps(answer)}]}}


def test_coming_up_asks_from_the_day_after_tomorrow_for_two_weeks(monkeypatch):
    from gateway import calendar_host

    class Host:
        def __init__(self, rt): pass
        def token_for(self, name): return "tok-agent"
        def reader_token(self, name): return "tok-reader"
        def service_url(self): return "http://svc/mcp"
    monkeypatch.setattr(calendar_host, "CalendarHost", Host)
    monkeypatch.setattr(calendar_host, "uses_calendar", lambda d: True)
    monkeypatch.setattr(home, "today_in", lambda tz, now=None: __import__("datetime").date(2026, 11, 22))
    seen = []

    def post(url, body, token):
        req = json.loads(body)
        seen.append(req["params"]["arguments"])
        return tool({"events": [{"title": "Deborah's birthday"}]} if req["params"]["name"] == "list_events" else {"calendars": []})
    out = home.calendar_coming(object(), "greg", {}, "greg", post=post)
    assert seen[0] == {"start": "2026-11-24", "end": "2026-12-06"} and out["events"][0]["title"] == "Deborah's birthday"


def test_lights_go_through_the_shared_agent_and_list_rooms_only(monkeypatch):
    from gateway import house_host
    agents = {"household": {"owners": ["greg", "bere"], "definition": {"house": {"enabled": True}}},
              "greg": {"owners": ["greg"], "definition": {"house": {"enabled": True}}},
              "bere": {"owners": ["bere"], "definition": {}}}
    assert home.house_agent(agents, "greg") == "household" and home.house_agent(agents, "bere") == "household"
    assert home.house_agent({"greg": agents["greg"]}, "greg") == "greg" and home.house_agent({"greg": agents["greg"]}, "bere") is None

    class Host:
        def __init__(self, rt): pass
        def token_for(self, name): return "tok" if name == "household" else ""
        def url(self): return "http://house/mcp"
    monkeypatch.setattr(house_host, "HouseHost", Host)
    calls = []

    def post(url, body, token):
        req = json.loads(body)
        calls.append((token, req["params"]["name"], req["params"]["arguments"]))
        if req["params"]["name"] == "list_lights":
            return tool({"rooms": [{"room": "Lounge", "kind": "room", "on": "2 of 3", "lights": [
                {"state": "on", "brightness": 40}, {"state": "on", "brightness": 61}, {"state": "off"}]},
                {"room": "TV Wall", "kind": "zone", "on": "1 of 1"}]})
        return tool({"done": True})
    assert home.lights(object(), "household", post=post) == {"rooms": [{"room": "Lounge", "on": 2, "of": 3, "brightness": 50}]}
    home.set_lights(object(), "household", ["everything"], False, post=post)
    assert calls[-1] == ("tok", "set_lights", {"targets": ["everything"], "on": False})
    home.set_lights(object(), "household", ["Lounge"], True, post=post, brightness=30)
    assert calls[-1] == ("tok", "set_lights", {"targets": ["Lounge"], "on": True, "brightness": 30})
    assert home.lights(object(), "greg", post=post) == {"error": "no_house"}


def test_the_shared_lists_are_read_live_and_ticked_off_or_put_back(monkeypatch):
    from gateway import house_host

    class Host:
        def __init__(self, rt): pass
        def token_for(self, name): return "tok" if name == "household" else ""
        def url(self): return "http://house/mcp"
    monkeypatch.setattr(house_host, "HouseHost", Host)
    calls = []

    def post(url, body, token):
        req = json.loads(body)
        calls.append((token, req["params"]["name"], req["params"]["arguments"]))
        name = req["params"]["name"]
        if name == "list_items":
            return tool({"lists": [{"list": "Shopping", "id": "todo.shopping", "open": ["pasta", {"item": "oat milk", "due": "2026-11-25", "details": "the barista one"}]},
                                   {"list": "To-do", "id": "todo.todo", "open": []}]})
        if name == "tick_items" and req["params"]["arguments"]["items"] == ["two things"]:
            return {"result": {"isError": True, "content": [{"type": "text", "text": "'two things' could be A, B. Say which."}]}}
        return tool({"ok": True})
    got = home.lists(object(), "household", post=post)
    assert got == {"lists": [
        {"name": "Shopping", "id": "todo.shopping", "items": [{"text": "pasta", "due": "", "details": ""},
                                                               {"text": "oat milk", "due": "2026-11-25", "details": "the barista one"}]},
        {"name": "To-do", "id": "todo.todo", "items": []}]}
    assert home.tick_item(object(), "household", "Shopping", "pasta", post=post) == {"ok": True}
    assert calls[-1] == ("tok", "tick_items", {"list": "Shopping", "items": ["pasta"]})
    assert home.tick_item(object(), "household", "Shopping", "pasta", done=False, post=post) == {"ok": True}
    assert calls[-1] == ("tok", "add_items", {"list": "Shopping", "items": ["pasta"]})        # added again: it goes back on
    refused = home.tick_item(object(), "household", "Shopping", "two things", post=post)
    assert refused["error"] == "refused" and "Say which" in refused["message"]
    assert home.lists(object(), "greg", post=post) == {"error": "no_house"}
    assert home.tick_item(object(), "greg", "Shopping", "pasta", post=post) == {"error": "no_house"}


def test_weather_sends_only_rounded_coordinates():
    asked = []

    def get(url):
        asked.append(url)
        return {"current": {"temperature_2m": 9.4, "weather_code": 61, "is_day": 0},
                "daily": {"weather_code": [61, 3], "temperature_2m_max": [11, 12], "temperature_2m_min": [4, 6],
                          "precipitation_probability_max": [80, 20]}}
    out = home.weather(51.52345, -1.03789, get=get)
    assert "latitude=51.52" in asked[0] and "longitude=-1.04" in asked[0] and "51.523" not in asked[0]
    assert out == {"now": {"temp": 9.4, "code": 61, "day": False}, "today": {"code": 61, "high": 11, "low": 4, "rain": 80},
                   "tomorrow": {"code": 3, "high": 12, "low": 6, "rain": 20}}
    found = home.places("Exampleton", get=lambda url: {"results": [{"name": "Exampleton", "admin1": "England", "country": "United Kingdom",
                                                                  "latitude": 51.50238, "longitude": -0.99947}]})
    assert found == [{"name": "Exampleton, England, United Kingdom", "lat": 51.5, "lon": -1.0}]


def test_widget_and_place_preferences_are_checked():
    from gateway import dashboard_api as d
    assert d._pref_ok("widgets", ["next", "weather"]) and d._pref_ok("widgets", [])
    assert not d._pref_ok("widgets", ["next", "next"]) and not d._pref_ok("widgets", ["tiles"]) and not d._pref_ok("widgets", "next")
    assert d._pref_ok("weather_place", {"name": "Exampleton", "lat": 51.5, "lon": -1.0}) and d._pref_ok("weather_place", None)
    assert not d._pref_ok("weather_place", {"name": "x", "lat": 91, "lon": 0})
    assert not d._pref_ok("weather_place", {"name": "x", "lat": 1, "lon": 2, "extra": 1})


def test_weather_has_a_list_of_places_in_your_order():
    import asyncio
    from gateway import dashboard_api as d
    b, p = {"name": "Exampleton", "lat": 51.5, "lon": -1.0}, {"name": "Testburg", "lat": 40.0, "lon": 10.0}
    assert d._pref_ok("weather_places", [b, p]) and d._pref_ok("weather_places", [])
    assert not d._pref_ok("weather_places", [b, dict(b)])                   # the same place twice
    assert not d._pref_ok("weather_places", [b, None]) and not d._pref_ok("weather_places", b)
    assert not d._pref_ok("weather_places", [{"name": f"P{i}", "lat": i, "lon": i} for i in range(d.MAX_WEATHER_PLACES + 1)])
    # the one place chosen before there could be more still shows; the list wins once there is one, even empty
    assert d.weather_places_of({"weather_place": b}) == [b]
    assert d.weather_places_of({"weather_place": b, "weather_places": [p, b]}) == [p, b]
    assert d.weather_places_of({"weather_place": b, "weather_places": []}) == []
    asked = []

    def fetch(lat, lon):
        asked.append((lat, lon))
        if lat > 50:
            raise OSError("no answer")
        return {"now": {"temp": 3}, "today": {}, "tomorrow": {}}
    cache = {}
    out = asyncio.run(d.weather_for([p, b], cache, fetch=fetch))
    assert [o["place"] for o in out] == ["Testburg", "Exampleton"]                 # in your order
    assert out[0]["now"]["temp"] == 3 and out[1]["error"] == "weather_unavailable"  # one that fails does not hide the others
    asyncio.run(d.weather_for([p], cache, fetch=fetch))
    assert asked.count((40.0, 10.0)) == 1                                    # kept 20 minutes


def test_notifications_are_read_one_by_one_and_kept_through_other_settings(monkeypatch):
    from gateway import dashboard_api as d
    store = {}
    monkeypatch.setattr(d.auth_db, "get_user_settings", lambda uid: {"ui_prefs": store.get(uid, "{}")}, raising=False)
    monkeypatch.setattr(d.auth_db, "update_user_settings", lambda uid, ui_prefs: store.__setitem__(uid, ui_prefs), raising=False)
    d._mark_read("u1", ["brain:Sun Nov 22 2026", "mx:$e1"])
    d._mark_read("u1", ["mx:$e1", "ai:123"])
    assert d._read_ids("u1") == ["brain:Sun Nov 22 2026", "mx:$e1", "ai:123"]
    d._mark_read("u1", [f"x{i}" for i in range(500)])
    assert len(d._read_ids("u1")) == d.READ_KEEP and d._read_ids("u1")[-1] == "x499"
    out = d._mark_read("u1", [], everything=True)
    assert out["read_ids"] == [] and out["seen_until"] > 0 and d._seen_until("u1") == out["seen_until"]


# ── Routines (Greg, 22 Nov) ────────────────────────────────────────────────────────────────

def test_routine_times_and_days_become_the_definition_keys():
    from gateway import dashboard_api as d
    assert d.cron_at("07:30", "1-5") == "30 7 * * 1-5" and d.time_of("30 7 * * 1-5") == "07:30" and d.time_of("*/10 * * * *") == ""
    allowed = {"briefing": {}, "digest": {}, "changes": {}}
    got = d.routines_choice({"briefing": {"enabled": True, "weekdays": "06:45", "weekends": "09:30"},
                             "digest": {"enabled": False, "day": 5, "time": "19:00"},
                             "changes": {"enabled": True, "time": "09:55"}}, allowed)
    assert got["briefing"] == {"enabled": True, "weekdays": "45 6 * * 1-5", "weekends": "30 9 * * 0,6"}
    assert got["digest"] == {"enabled": False, "schedule": "0 19 * * 5"}
    assert got["changes"] == {"enabled": True, "daily": "55 9 * * *", "week": "5 10 * * 0", "check": "15 10 * * *"}
    for bad in ({"digest": {"enabled": True, "day": 9, "time": "19:00"}}, {"briefing": {"enabled": True, "weekdays": "25:00", "weekends": "09:00"}},
                {"changes": {"enabled": True, "time": "10:00"}}):
        with pytest.raises((ValueError, KeyError)):
            d.routines_choice(bad, {"digest": {}, "briefing": {}})   # an agent without changes cannot get them


def test_choices_are_kept_and_laid_over_the_definition(tmp_path):
    from gateway import agent_runtime as ar
    env = {"LOGOS_HOME": str(tmp_path)}
    ar.save_routines(env, "bere", {"briefing": {"weekdays": "30 6 * * 1-5"}, "nonsense": {"x": 1}})
    ar.save_routines(env, "bere", {"digest": {"enabled": False}})
    assert ar.load_routines(env) == {"bere": {"briefing": {"weekdays": "30 6 * * 1-5"}, "digest": {"enabled": False}}}
    agent = {"briefing": {"weekends": "0 9 * * 0,6"}, "calendar": {"person": "bere"}, "matrix": {"enabled": True}}
    ar.merge(agent, {k: v for k, v in ar.load_routines(env)["bere"].items() if k in ar.ROUTINE_KEYS})
    assert ar.briefing(agent) == {"weekdays": "30 6 * * 1-5", "weekends": "0 9 * * 0,6", "speaker": False}
    assert agent["digest"] == {"enabled": False}


def test_tips_seen_is_a_list_of_known_screens():
    from gateway.dashboard_api import _pref_ok
    assert _pref_ok("tips_seen", []) and _pref_ok("tips_seen", ["home", "routines"])
    assert not _pref_ok("tips_seen", ["home", "home"]) and not _pref_ok("tips_seen", ["elsewhere"])
    assert not _pref_ok("tips_seen", "home")


# ── The Calendar screen (Béré, 27 Nov) ─────────────────────────────────────────────────────────

class _CalHost:
    def __init__(self, rt): pass
    def token_for(self, name): return "tok-agent"
    def reader_token(self, name): return "tok-reader" if name in ("greg", "bere") else ""
    def service_url(self): return "http://svc/mcp"


def _cal_post(seen):
    def post(url, body, token):
        req = json.loads(body)
        seen.append((token, req["params"]["name"], req["params"]["arguments"]))
        if req["params"]["name"] == "list_events":
            return tool({"events": [{"id": "household/a.ics", "calendar": "household", "title": "Bins", "start": "2026-11-30", "all_day": True}]})
        return tool({"calendars": [{"name": "bere", "shows": "Béré"}, {"name": "household", "shows": "Household (shared by everyone)"},
                                   {"name": "personal", "shows": "Your own calendar"}]})
    return post


def test_calendar_range_reads_with_the_persons_own_key_in_one_call(monkeypatch):
    from gateway import calendar_host
    monkeypatch.setattr(calendar_host, "CalendarHost", _CalHost)
    monkeypatch.setattr(calendar_host, "uses_calendar", lambda d: True)
    monkeypatch.setattr(home, "today_in", lambda tz, now=None: __import__("datetime").date(2026, 11, 27))
    seen = []
    out = home.calendar_range(object(), "greg", {}, "greg", "2026-10-26", "2026-12-06", post=_cal_post(seen))
    assert seen[0] == ("tok-reader", "list_events", {"start": "2026-10-26", "end": "2026-12-06"})
    assert [s[1] for s in seen] == ["list_events", "list_calendars"]
    assert out["today"] == "2026-11-27" and out["start"] == "2026-10-26" and out["end"] == "2026-12-06"
    assert out["events"][0]["title"] == "Bins" and out["busy"] == []
    # yours first, then Household (its "(shared by everyone)" dropped), then the others
    assert [(c["key"], c["name"]) for c in out["calendars"]] == [("personal", "Your own calendar"), ("household", "Household"), ("bere", "Béré")]


def test_calendar_range_falls_back_to_what_the_agent_sees_and_says_when_there_is_none(monkeypatch):
    from gateway import calendar_host
    monkeypatch.setattr(calendar_host, "CalendarHost", _CalHost)
    uses = {"v": True}
    monkeypatch.setattr(calendar_host, "uses_calendar", lambda d: uses["v"])
    seen = []
    home.calendar_range(object(), "household", {}, "guest", "2026-11-01", "2026-11-30", post=_cal_post(seen))
    assert seen[0][0] == "tok-agent"             # no calendar account of their own
    uses["v"] = False
    assert home.calendar_range(object(), "household", {}, "guest", "2026-11-01", "2026-11-30", post=_cal_post([])) == {"error": "no_calendar"}
    down = home.calendar_range(object(), "greg", {}, "greg", "2026-11-01", "2026-11-30", post=lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert down["error"] == "calendar_unavailable"


def test_calendar_range_refuses_odd_ranges_before_asking_anyone(monkeypatch):
    from gateway import calendar_host
    monkeypatch.setattr(calendar_host, "CalendarHost", _CalHost)
    asked = []
    for start, end in (("2026-11-30", "2026-11-01"), ("nope", "2026-11-01"), ("2026-01-01", "2026-12-31"), ("", "")):
        assert home.calendar_range(object(), "greg", {}, "greg", start, end, post=lambda *a: asked.append(a)) == {"error": "bad_range"}
    assert asked == []
    assert home.parse_range("2026-11-01", "2026-11-01") and home.parse_range("2026-10-26", "2026-12-26")   # 62 days is fine


def test_calendar_endpoint_answers_once_per_range_and_keeps_it_a_minute(monkeypatch):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda r: {"id": "u1", "username": "bere", "display_name": "Béré", "role": "user"})

    async def visible(request, username):
        return {"bere": {"owners": ["bere"], "definition": {"timezone": "Europe/Paris"}},
                "household": {"owners": ["greg", "bere"], "definition": {}}}
    monkeypatch.setattr(d, "_visible_agents", visible)
    monkeypatch.setattr(d.agent_chat, "runtime_of", lambda backend: object())
    calls = []

    def fake_range(runtime, agent, definition, username, start, end):
        calls.append((agent, username, start, end))
        return {"start": start, "end": end, "today": "2026-11-27", "events": [], "busy": [], "calendars": []}
    monkeypatch.setattr(home, "calendar_range", fake_range)

    async def run():
        app = web.Application()
        app.router.add_get("/api/v2/calendar", d.handle_calendar)
        async with TestClient(TestServer(app)) as c:
            bad = await c.get("/api/v2/calendar?start=2026-12-01&end=2026-11-01")
            first = await (await c.get("/api/v2/calendar?start=2026-10-26&end=2026-12-06")).json()
            again = await (await c.get("/api/v2/calendar?start=2026-10-26&end=2026-12-06")).json()
            week = await (await c.get("/api/v2/calendar?start=2026-11-23&end=2026-11-29")).json()
            return bad.status, first, again, week
    status, first, again, week = asyncio.run(run())
    assert status == 400
    assert first == again and first["agent"] == "bere"           # "Add" goes to her own agent
    assert calls == [("bere", "bere", "2026-10-26", "2026-12-06"), ("bere", "bere", "2026-11-23", "2026-11-29")]
    assert week["start"] == "2026-11-23"
    assert first["can_change"] == []                             # her agent has no calendar of hers in this setup


def test_changes_go_through_your_own_agent_with_your_own_calendar_only():
    mine = {"owners": ["bere"], "definition": {"calendar": {"enabled": True, "person": "bere"}}}
    shared = {"owners": ["greg", "bere"], "definition": {"calendar": {"enabled": True, "free_busy": {"bere": "Béré"}}}}
    gregs = {"owners": ["greg"], "definition": {"calendar": {"enabled": True, "person": "greg"}}}
    assert home.calendar_writer({"bere": mine, "household": shared, "greg": gregs}, "bere") == "bere"
    assert home.calendar_writer({"household": shared, "greg": gregs}, "bere") is None      # never the shared one, nor Greg's
    odd = {"owners": ["bere"], "definition": {"calendar": {"enabled": True, "person": "greg"}}}
    assert home.calendar_writer({"x": odd}, "bere") is None                                   # her agent, but Greg's calendar


def test_change_arguments_are_checked_before_anything_is_sent():
    assert home.change_args("add", {"calendar": "household", "title": " Bins ", "start": "2026-11-30", "x": 1}) == \
        {"calendar": "household", "title": "Bins", "start": "2026-11-30"}
    assert home.change_args("update", {"id": "personal/a.ics", "title": "Dentist", "location": ""}) == \
        {"id": "personal/a.ics", "title": "Dentist", "location": ""}
    assert home.change_args("cancel", {"id": "household/a.ics", "title": "ignored"}) == {"id": "household/a.ics"}
    for action, body in (("add", {"calendar": "bere", "title": "x", "start": "2026-11-30"}), ("add", {"calendar": "personal", "title": " "}),
                         ("update", {"id": "bere/a.ics", "title": "mine now"}), ("cancel", {"id": "a.ics"}), ("delete_all", {})):
        with pytest.raises(ValueError):
            home.change_args(action, body)


def test_a_change_uses_the_agents_key_and_passes_a_refusal_on_in_plain_words(monkeypatch):
    from gateway import calendar_host
    monkeypatch.setattr(calendar_host, "CalendarHost", _CalHost)
    seen = []

    def post(url, body, token):
        req = json.loads(body)
        seen.append((token, req["params"]["name"], req["params"]["arguments"]))
        if req["params"]["name"] == "cancel_event":
            return {"result": {"isError": True, "content": [{"type": "text", "text": "Not done: This is a repeating event."}]}}
        return tool({"added": {"id": "personal/n.ics"}})
    out = home.calendar_change(object(), "bere", "add", {"calendar": "personal", "title": "Yoga", "start": "2026-12-01 18:00"}, post=post)
    assert out == {"added": {"id": "personal/n.ics"}} and seen[0][:2] == ("tok-agent", "add_event")
    out = home.calendar_change(object(), "bere", "cancel", {"id": "personal/r.ics"}, post=post)
    assert out == {"error": "refused", "message": "This is a repeating event."}


def test_calendar_change_endpoint_refuses_without_your_own_agent_and_forgets_what_it_kept(monkeypatch):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda r: {"id": "u1", "username": "bere", "display_name": "Béré", "role": "user"})
    agents = {"household": {"owners": ["greg", "bere"], "definition": {"calendar": {"enabled": True}}}}

    async def visible(request, username):
        return agents
    monkeypatch.setattr(d, "_visible_agents", visible)
    monkeypatch.setattr(d.agent_chat, "runtime_of", lambda backend: object())
    done = []
    monkeypatch.setattr(home, "calendar_change", lambda rt, agent, action, args: done.append((agent, action, args)) or {"added": {}})

    async def run():
        app = web.Application()
        app.router.add_post("/api/v2/calendar/{action}", d.handle_calendar_change)
        app["calendar_cache"] = {("greg", "a", "b"): (0, {})}
        async with TestClient(TestServer(app)) as c:
            none = await c.post("/api/v2/calendar/add", json={"calendar": "personal", "title": "Yoga", "start": "2026-12-01"})
            agents["bere"] = {"owners": ["bere"], "definition": {"calendar": {"enabled": True, "person": "bere"}}}
            bad = await c.post("/api/v2/calendar/add", json={"calendar": "greg", "title": "Yoga", "start": "2026-12-01"})
            good = await c.post("/api/v2/calendar/add", json={"calendar": "personal", "title": "Yoga", "start": "2026-12-01"})
            return none.status, bad.status, good.status, app["calendar_cache"]
    s_none, s_bad, s_good, cache = asyncio.run(run())
    assert (s_none, s_bad, s_good) == (403, 400, 200)
    assert done == [("bere", "add", {"calendar": "personal", "title": "Yoga", "start": "2026-12-01"})] and cache == {}
