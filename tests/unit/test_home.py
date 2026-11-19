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
            return "tok-greg"

        def service_url(self):
            return "http://svc/mcp"

    monkeypatch.setattr(calendar_host, "CalendarHost", Host)
    monkeypatch.setattr(calendar_host, "uses_calendar", lambda d: True)
    monkeypatch.setattr(home, "today_in", lambda tz, now=None: __import__("datetime").date(2026, 11, 19))
    seen = {}

    def post(url, body, token):
        seen.update(url=url, token=token, req=json.loads(body))
        return {"result": {"content": [{"type": "text", "text": json.dumps({"events": [{"title": "Dentist"}]})}]}}

    out = home.calendar_today(object(), "greg", {"timezone": "Europe/London"}, post=post)
    assert out == {"date": "2026-11-19", "events": [{"title": "Dentist"}], "busy": []}
    assert seen["token"] == "tok-greg" and seen["req"]["params"]["name"] == "list_events"
    assert seen["req"]["params"]["arguments"] == {"start": "2026-11-19", "end": "2026-11-20"}
    failing = home.calendar_today(object(), "greg", {}, post=lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert failing["error"] == "calendar_unavailable"
