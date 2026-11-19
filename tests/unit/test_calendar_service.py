"""gateway/calendar_service.py: how agents use the calendars (story C3)."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("vobject")

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("logos_calendar_service_test", REPO / "gateway" / "calendar_service.py")
cs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cs
spec.loader.exec_module(cs)

LONDON = ZoneInfo("Europe/London")
NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)


class FakeDAV:
    """Calendars as {path: {name: (ics, etag)}}; query returns everything in the calendar."""

    def __init__(self):
        self.cals = {"/greg/personal/": {}, "/bere/personal/": {}, "/household/calendar/": {}}
        self.n = 0

    def _split(self, path):
        cal, name = path.rsplit("/", 1)
        return self.cals[cal + "/"], name

    def query(self, path, start, end):
        return [(name, ics) for name, (ics, _) in self.cals[path].items()]

    def get(self, path):
        cal, name = self._split(path)
        if name not in cal:
            raise cs.Refused("There is no such event.")
        return cal[name]

    def put(self, path, ics, etag=""):
        cal, name = self._split(path)
        if etag and cal.get(name, (None, None))[1] != etag:
            raise cs.Refused("Someone changed this event meanwhile.")
        self.n += 1
        cal[name] = (ics, f'"{self.n}"')

    def delete(self, path, etag):
        cal, name = self._split(path)
        cal.pop(name, None)


def agents():
    return {
        "greg": {"token": "tg", "timezone": "Europe/London",
                 "full": {"personal": {"path": "/greg/personal/", "label": "Greg's own calendar"},
                          "household": {"path": "/household/calendar/", "label": "Household"}},
                 "busy": {}},
        "household": {"token": "th", "timezone": "Europe/London",
                      "full": {"household": {"path": "/household/calendar/", "label": "Household"}},
                      "busy": {"greg": {"paths": ["/greg/personal/"], "label": "Greg"},
                               "bere": {"paths": ["/bere/personal/"], "label": "Béré"}}},
    }


@pytest.fixture
def svc():
    return cs.Service(FakeDAV(), agents(), log=lambda m: None, clock=lambda: NOW)


def test_times():
    assert cs.parse_when("2026-11-21", LONDON) == (dt.date(2026, 11, 21), True)
    when, day = cs.parse_when("2026-07-01 19:00", LONDON)
    assert not day and cs.as_utc(when, LONDON).hour == 18            # summer time
    with pytest.raises(cs.Refused):
        cs.parse_when("next tuesday", LONDON)
    with pytest.raises(cs.Refused):
        cs.window("2026-11-01", "2027-06-01", LONDON)                # too long


def test_event_text_round_trips_and_escapes():
    ics = cs.build_event("u1@logos", "Dinner; Mum, Dad", dt.datetime(2026, 11, 21, 19, tzinfo=LONDON),
                         dt.datetime(2026, 11, 21, 21, tzinfo=LONDON), False, "Café", "line1\nline2" + "é" * 80, NOW)
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))
    ev = cs.events_in(ics)[0]
    assert (ev["title"], ev["location"]) == ("Dinner; Mum, Dad", "Café")
    assert ev["notes"].startswith("line1\nline2")
    assert not ev["all_day"] and not ev["repeats"]


def test_add_list_move_cancel(svc):
    added = svc.add_event("greg", "household", "Dinner with Mum", "2026-11-21 19:00")["added"]
    assert added["end"] == "2026-11-21 20:00"                         # one hour by default
    listed = svc.list_events("greg", "2026-11-21")
    assert [e["title"] for e in listed["events"]] == ["Dinner with Mum"]
    assert "busy" not in listed                                       # Greg's agent sees no busy-only calendars
    moved = svc.update_event("greg", added["id"], start="2026-11-22 18:30")["updated"]
    assert (moved["start"], moved["end"]) == ("2026-11-22 18:30", "2026-11-22 19:30")   # keeps its length
    assert svc.cancel_event("greg", added["id"])["cancelled"]["title"] == "Dinner with Mum"
    assert svc.list_events("greg", "2026-11-22")["events"] == []


def test_all_day_event_end_is_inclusive(svc):
    added = svc.add_event("greg", "personal", "Trip", "2026-12-01", "2026-12-03")["added"]
    assert (added["start"], added["end"], added["all_day"]) == ("2026-12-01", "2026-12-03", True)
    ev = cs.events_in(next(iter(svc.dav.cals["/greg/personal/"].values()))[0])[0]
    assert ev["end"] == dt.date(2026, 12, 4)                          # iCalendar end is exclusive


def test_household_agent_sees_only_busy_times(svc):
    svc.add_event("greg", "personal", "Doctor about my knee", "2026-11-21 10:00", "2026-11-21 11:00",
                  location="Surgery", notes="private")
    out = svc.list_events("household", "2026-11-21")
    assert out["events"] == []
    assert out["busy"] == [{"who": "Greg", "busy_from": "2026-11-21 10:00", "busy_until": "2026-11-21 11:00"}]
    assert "knee" not in json.dumps(out) and "Surgery" not in json.dumps(out)


def test_agents_cannot_reach_beyond_their_calendars(svc):
    with pytest.raises(cs.Refused):
        svc.add_event("household", "personal", "x", "2026-11-21")
    added = svc.add_event("greg", "personal", "Mine", "2026-11-21 09:00")["added"]
    with pytest.raises(cs.Refused):
        svc.cancel_event("household", added["id"])                    # not its calendar
    for bad in ("household/../greg/personal/x.ics", "household/x", "nope/x.ics", ""):
        with pytest.raises(cs.Refused):
            svc.cancel_event("greg", bad)


def test_repeating_events_are_read_only(svc):
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:r1\r\nDTSTAMP:20261101T000000Z\r\n"
           "DTSTART:20261102T180000Z\r\nDTEND:20261102T190000Z\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:Gym\r\n"
           "END:VEVENT\r\nEND:VCALENDAR\r\n")
    svc.dav.put("/greg/personal/r1.ics", ics)
    with pytest.raises(cs.Refused, match="repeating"):
        svc.update_event("greg", "personal/r1.ics", start="2026-11-03 18:00")
    with pytest.raises(cs.Refused, match="repeating"):
        svc.cancel_event("greg", "personal/r1.ics")


def test_find_free_time_skips_everyones_busy_times(svc):
    svc.add_event("greg", "personal", "Work", "2026-11-21 09:00", "2026-11-21 17:00")
    svc.dav.put("/bere/personal/b.ics", cs.build_event("b@x", "Class", dt.datetime(2026, 11, 21, 18, tzinfo=LONDON),
                                                        dt.datetime(2026, 11, 21, 19, tzinfo=LONDON), False, now=NOW))
    out = svc.find_free_time("household", "2026-11-21", "2026-11-21", 60, "08:00", "21:00")
    assert out["free"] == [{"from": "2026-11-21 08:00", "until": "2026-11-21 09:00"},
                           {"from": "2026-11-21 17:00", "until": "2026-11-21 18:00"},
                           {"from": "2026-11-21 19:00", "until": "2026-11-21 21:00"}]
    assert set(out["checked"]) == {"Household", "Greg", "Béré"}


def test_rpc(svc):
    tools = cs.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_calendars", "list_events", "add_event", "update_event",
                                          "cancel_event", "find_free_time"}
    bad = cs.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                      "params": {"name": "add_event", "arguments": {"calendar": "work", "title": "x",
                                                                                     "start": "2026-11-21"}}})
    assert bad["result"]["isError"] and "Not done" in bad["result"]["content"][0]["text"]
    assert svc.agent_for_token("th") == "household" and svc.agent_for_token("nope") is None


def test_a_reader_key_reads_everything_and_changes_nothing(svc):
    svc.agents["person:greg"] = {"token": "rg", "timezone": "Europe/London", "read_only": True, "busy": {},
                                 "full": {"personal": {"path": "/greg/personal/", "label": "Your own calendar"},
                                          "bere": {"path": "/bere/personal/", "label": "Béré"}}}
    svc.dav.put("/bere/personal/b.ics", cs.build_event("b@x", "Pilates", dt.datetime(2026, 11, 21, 18, tzinfo=LONDON),
                                                        dt.datetime(2026, 11, 21, 19, tzinfo=LONDON), False, now=NOW))
    assert [e["title"] for e in svc.list_events("person:greg", "2026-11-21")["events"]] == ["Pilates"]
    for change in (lambda: svc.add_event("person:greg", "personal", "x", "2026-11-21"),
                   lambda: svc.cancel_event("person:greg", "bere/b.ics")):
        with pytest.raises(cs.Refused, match="read-only"):
            change()
    assert all(c["access"] == "read only" for c in svc.calendars("person:greg")["calendars"])


def test_a_person_agent_sees_the_other_calendar_but_cannot_change_it(svc):
    svc.agents["greg"]["read"] = {"bere": {"path": "/bere/personal/", "label": "Béré"}}
    svc.dav.put("/bere/personal/p.ics", cs.build_event("p@x", "Pilates", dt.datetime(2026, 11, 21, 18, tzinfo=LONDON),
                                                        dt.datetime(2026, 11, 21, 19, tzinfo=LONDON), False, now=NOW))
    ev = svc.list_events("greg", "2026-11-21")["events"]
    assert [(e["title"], e["calendar"], e.get("read_only")) for e in ev] == [("Pilates", "bere", True)]
    with pytest.raises(cs.Refused, match="someone else"):
        svc.add_event("greg", "bere", "Dinner", "2026-11-21 19:00")
    with pytest.raises(cs.Refused, match="someone else"):
        svc.cancel_event("greg", "bere/p.ics")
    free = svc.find_free_time("greg", "2026-11-21", "2026-11-21", 60, "17:00", "21:00")
    assert free["free"] == [{"from": "2026-11-21 17:00", "until": "2026-11-21 18:00"},     # her Pilates counts
                            {"from": "2026-11-21 19:00", "until": "2026-11-21 21:00"}]
    assert "Béré" in free["checked"]
    assert {"name": "bere", "shows": "Béré", "access": "read only (see, never change)"} in svc.calendars("greg")["calendars"]
