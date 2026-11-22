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
            return tool({"rooms": [{"room": "Lounge", "kind": "room", "on": "2 of 3"}, {"room": "TV Wall", "kind": "zone", "on": "1 of 1"}]})
        return tool({"done": True})
    assert home.lights(object(), "household", post=post) == {"rooms": [{"room": "Lounge", "on": 2, "of": 3}]}
    home.set_lights(object(), "household", ["everything"], False, post=post)
    assert calls[-1] == ("tok", "set_lights", {"targets": ["everything"], "on": False})
    assert home.lights(object(), "greg", post=post) == {"error": "no_house"}


def test_weather_sends_only_rounded_coordinates():
    asked = []

    def get(url):
        asked.append(url)
        return {"current": {"temperature_2m": 9.4, "weather_code": 61, "is_day": 0},
                "daily": {"weather_code": [61, 3], "temperature_2m_max": [11, 12], "temperature_2m_min": [4, 6],
                          "precipitation_probability_max": [80, 20]}}
    out = home.weather(50.82345, -0.13789, get=get)
    assert "latitude=50.82" in asked[0] and "longitude=-0.14" in asked[0] and "50.823" not in asked[0]
    assert out == {"now": {"temp": 9.4, "code": 61, "day": False}, "today": {"code": 61, "high": 11, "low": 4, "rain": 80},
                   "tomorrow": {"code": 3, "high": 12, "low": 6, "rain": 20}}
    found = home.places("Brighton", get=lambda url: {"results": [{"name": "Brighton", "admin1": "England", "country": "United Kingdom",
                                                                  "latitude": 50.82838, "longitude": -0.13947}]})
    assert found == [{"name": "Brighton, England, United Kingdom", "lat": 50.83, "lon": -0.14}]


def test_widget_and_place_preferences_are_checked():
    from gateway import dashboard_api as d
    assert d._pref_ok("widgets", ["next", "weather"]) and d._pref_ok("widgets", [])
    assert not d._pref_ok("widgets", ["next", "next"]) and not d._pref_ok("widgets", ["tiles"]) and not d._pref_ok("widgets", "next")
    assert d._pref_ok("weather_place", {"name": "Brighton", "lat": 50.83, "lon": -0.14}) and d._pref_ok("weather_place", None)
    assert not d._pref_ok("weather_place", {"name": "x", "lat": 91, "lon": 0})
    assert not d._pref_ok("weather_place", {"name": "x", "lat": 1, "lon": 2, "extra": 1})


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
    assert ar.briefing(agent) == {"weekdays": "30 6 * * 1-5", "weekends": "0 9 * * 0,6"}
    assert agent["digest"] == {"enabled": False}
