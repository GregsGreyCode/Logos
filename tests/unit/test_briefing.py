"""The morning briefing's sandbox scripts (infra/agent-jobs/briefing_*.py, story C4)."""
import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[2] / "infra" / "agent-jobs"
TODAY = datetime.date(2026, 11, 19)
ENV = {"LOGOS_WEATHER_URL": "http://w/weather", "LOGOS_WEB_TOKEN": "wt",
       "LOGOS_CALENDAR_URL": "http://c/mcp", "LOGOS_CALENDAR_TOKEN": "ct"}
EVENTS = [
    {"calendar": "household", "title": "Boiler service", "start": "2026-11-19 18:00", "end": "2026-11-19 19:00"},
    {"calendar": "personal", "title": "Dentist", "start": "2026-11-19 16:30", "end": "2026-11-19 17:00",
     "location": "Smile Clinic"},
    {"calendar": "personal", "title": "Holiday", "start": "2026-11-19", "end": "2026-11-20", "all_day": True},
    {"calendar": "bere", "title": "Pilates", "start": "2026-11-19 18:00", "end": "2026-11-19 19:00", "read_only": True},
]


def load(name):
    sys.path.insert(0, str(JOBS))
    try:
        spec = importlib.util.spec_from_file_location(name, JOBS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
        return mod
    finally:
        sys.path.remove(str(JOBS))


@pytest.fixture
def bt(monkeypatch):
    mod = load("briefing_today")
    state = {"weather": "Rain, 6 to 10°C, 100% chance of rain", "events": EVENTS, "calls": []}

    def fake(url, token, body=None):
        state["calls"].append((url, token, body))
        if url == ENV["LOGOS_WEATHER_URL"]:
            if state["weather"] is None:
                raise OSError("reader down")
            return {"today": state["weather"]}
        if state["events"] is None:
            raise OSError("calendar down")
        return {"result": {"content": [{"type": "text", "text": json.dumps({"events": state["events"]})}]}}

    monkeypatch.setattr(mod, "get_json", fake)
    monkeypatch.setattr(mod, "env", lambda: dict(ENV))
    mod.state = state
    return mod


def test_facts_list_the_day_in_order_with_others_marked(bt):
    assert bt.facts(TODAY) == [
        "Today: Thursday 19 November 2026.",
        "Weather: Rain, 6 to 10°C, 100% chance of rain.",
        "Your person's events today (3):",
        "- all day: Holiday (personal)",
        "- 16:30 to 17:00: Dentist, at Smile Clinic (personal)",
        "- 18:00 to 19:00: Boiler service (household)",
        "Other people's events today (read only; mention one only if it matters to your person's day):",
        "- 18:00 to 19:00: Pilates (bere)",
    ]
    (cal,) = [c for c in bt.state["calls"] if c[0] == ENV["LOGOS_CALENDAR_URL"]]
    assert cal[1] == "ct" and cal[2]["params"] == {"name": "list_events", "arguments": {"start": "2026-11-19"}}


def test_facts_never_claim_an_empty_day_when_the_calendar_is_down(bt):
    bt.state["weather"], bt.state["events"] = None, None
    assert bt.facts(TODAY) == ["Today: Thursday 19 November 2026.", "Weather: not available this morning.",
                               "Calendar: could not be read this morning."]
    bt.state["events"] = []
    assert bt.facts(TODAY)[-1] == "Your person's events today: none."


def _job(name, status="ok", at="2026-11-19T07:00:40+00:00", err=None):
    return {"name": name, "last_status": status, "last_run_at": at, "last_delivery_error": err}


def test_check_is_silent_once_the_briefing_went_out(bt):
    bc = load("briefing_check")
    assert bc.sent_today([_job("morning-briefing")], TODAY)
    assert bc.sent_today([_job("morning-briefing-weekend")], TODAY)
    assert not bc.sent_today([_job("morning-briefing", status="error")], TODAY)
    assert not bc.sent_today([_job("morning-briefing", err="room not found")], TODAY)
    assert not bc.sent_today([_job("morning-briefing", at="2026-11-18T07:00:40+00:00")], TODAY)
    assert not bc.sent_today([_job("weekly-digest")], TODAY)
    assert not bc.sent_today([{"name": "morning-briefing", "last_status": "ok"}], TODAY)


def test_plain_version(bt):
    bc = load("briefing_check")
    bc.bt = bt
    assert bc.plain(TODAY, ENV).splitlines() == [
        "Thursday 19 November. Rain, 6 to 10°C, 100% chance of rain.",
        "Holiday", "16:30 Dentist, Smile Clinic", "18:00 Boiler service",
        "(Plain version: I couldn't write this morning's briefing.)"]
    bt.state["events"], bt.state["weather"] = [], None
    assert bc.plain(TODAY, ENV).splitlines()[:2] == ["Thursday 19 November.", "Nothing in the calendar today."]
    bt.state["events"] = None
    assert "I couldn't read the calendar this morning." in bc.plain(TODAY, ENV)


# ── noticeboard check fingerprint (story D1) ──────────────────────────────────

def test_noticeboard_fingerprint_retries_every_half_hour_until_read(tmp_path):
    ns = load("noticeboard_state")
    retry = tmp_path / "retry"
    base = datetime.datetime(2026, 11, 19, 10, 0).timestamp()
    fp = lambda pending, minutes: ns.fingerprint(pending, base + minutes * 60, retry)
    quiet = {"latest": 5, "latest_expired": 0, "unread": 0}
    assert fp(quiet, 0) == "latest message 5, latest expired request 0"     # as before D1: no wasted run on deploy
    waiting = {"latest": 6, "latest_expired": 0, "unread": 1}
    first = fp(waiting, 12)                        # a new message: changed, the agent runs (LM Studio off)
    assert first == "latest message 6, latest expired request 0, retry 2026-11-19T10:00"
    assert fp(waiting, 20) == first                # same half hour: no run
    second = fp(waiting, 31)
    assert second != first                         # next half hour, still unread: run again
    read = {"latest": 6, "latest_expired": 0, "unread": 0}
    assert fp(read, 33) == second and fp(read, 95) == second   # read: the stamp stays, nothing changes
