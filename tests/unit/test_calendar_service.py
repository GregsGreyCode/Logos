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
    """Calendars as {path: {name: (ics, etag)}}; items returns everything in the calendar (the
    service filters by time itself). ``version`` moves on every change, like Radicale's."""

    def __init__(self):
        self.cals = {"/greg/personal/": {}, "/bere/personal/": {}, "/household/calendar/": {}}
        self.n = 0
        self.reads = []                 # paths read with items()
        self.frozen = None              # a version that never moves, to test the service's own writes

    def version(self, path):
        return self.frozen if self.frozen is not None else f"v{self.n}"

    def items(self, path, start, end):
        self.reads.append(path)
        return [(name, ics) for name, (ics, _) in self.cals[path].items()]

    def _split(self, path):
        cal, name = path.rsplit("/", 1)
        return self.cals[cal + "/"], name

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
        self.n += 1
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


# ── reading fast: repeats expanded here, months kept until the calendar changes ──

VTZ = ("BEGIN:VTIMEZONE\r\nTZID:Europe/London\r\nBEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0000\r\nTZOFFSETTO:+0100\r\n"
       "TZNAME:BST\r\nDTSTART:19700329T010000\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\nEND:DAYLIGHT\r\n"
       "BEGIN:STANDARD\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0000\r\nTZNAME:GMT\r\nDTSTART:19701025T020000\r\n"
       "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n")


def vcal(*vevents):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + VTZ + "".join(
        "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n" + v + "END:VEVENT\r\n" for v in vevents) + "END:VCALENDAR\r\n"


def titles(out):
    return [(e["start"], e["title"]) for e in out["events"]]


def test_repeats_are_expanded_with_exceptions_and_time_zones(svc):
    # Weekly 18:00 London time from October, across the clocks going back: one week skipped
    # (EXDATE), one moved to 20:00 (RECURRENCE-ID), one cancelled.
    svc.dav.put("/greg/personal/gym.ics", vcal(
        "UID:gym\r\nDTSTART;TZID=Europe/London:20261005T180000\r\nDTEND;TZID=Europe/London:20261005T190000\r\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20261201T000000Z\r\nEXDATE;TZID=Europe/London:20261019T180000\r\nSUMMARY:Gym\r\n",
        "UID:gym\r\nRECURRENCE-ID;TZID=Europe/London:20261102T180000\r\nDTSTART;TZID=Europe/London:20261102T200000\r\n"
        "DTEND;TZID=Europe/London:20261102T210000\r\nSUMMARY:Gym (late)\r\n",
        "UID:gym\r\nRECURRENCE-ID:20261109T180000Z\r\nDTSTART:20261109T180000Z\r\nDTEND:20261109T190000Z\r\n"
        "STATUS:CANCELLED\r\nSUMMARY:Gym\r\n"))
    svc.dav.put("/greg/personal/bday.ics", vcal(
        "UID:bday\r\nDTSTART;VALUE=DATE:20001020\r\nDTEND;VALUE=DATE:20001021\r\nRRULE:FREQ=YEARLY\r\nSUMMARY:Birthday\r\n"))
    out = svc.list_events("greg", "2026-10-01", "2026-11-30")
    assert titles(out) == [("2026-10-05 18:00", "Gym"), ("2026-10-12 18:00", "Gym"),   # 19 Oct skipped
                           ("2026-10-20", "Birthday"),
                           ("2026-10-26 18:00", "Gym"),                                  # 18:00 after the clocks change
                           ("2026-11-02 20:00", "Gym (late)"),                           # moved; 9 Nov cancelled
                           ("2026-11-16 18:00", "Gym"), ("2026-11-23 18:00", "Gym"), ("2026-11-30 18:00", "Gym")]
    assert all(e["repeats"] and e["id"] in ("personal/gym.ics", "personal/bday.ics") for e in out["events"])
    assert titles(svc.list_events("greg", "2026-11-09")) == []


def test_all_day_events_follow_the_readers_own_days(svc):
    # 1 July in London starts at 23:00 UTC on 30 June: 30 June's all-day event is not on 1 July.
    svc.dav.put("/greg/personal/d.ics", vcal("UID:d\r\nDTSTART;VALUE=DATE:20260630\r\nDTEND;VALUE=DATE:20260701\r\nSUMMARY:WFH\r\n"))
    assert titles(svc.list_events("greg", "2026-07-01")) == []
    assert titles(svc.list_events("greg", "2026-06-30")) == [("2026-06-30", "WFH")]


def test_a_repeat_read_comes_from_the_cache_until_the_calendar_changes(svc):
    svc.add_event("greg", "personal", "Dentist", "2026-11-21 10:00")
    first = svc.list_events("greg", "2026-11-21", "2026-11-27")
    svc.dav.reads.clear()
    assert svc.list_events("greg", "2026-11-21", "2026-11-27") == first
    assert svc.list_events("greg", "2026-11-23")["events"] == []      # another range in the same month
    assert svc.dav.reads == []
    # Changed on a phone (not through this service): the calendar's version moves, it is read again.
    svc.dav.put("/greg/personal/phone.ics", cs.build_event("phone@x", "Added on the phone",
                                                           dt.datetime(2026, 11, 22, 9, tzinfo=LONDON),
                                                           dt.datetime(2026, 11, 22, 10, tzinfo=LONDON), False, now=NOW))
    assert "Added on the phone" in [e["title"] for e in svc.list_events("greg", "2026-11-21", "2026-11-27")["events"]]
    assert "/greg/personal/" in svc.dav.reads


def test_the_services_own_changes_show_at_once(svc):
    svc.dav.frozen = "same"         # even if the server's version did not move (same second)
    svc.list_events("greg", "2026-11-21")
    added = svc.add_event("greg", "personal", "Haircut", "2026-11-21 15:00")["added"]
    assert titles(svc.list_events("greg", "2026-11-21")) == [("2026-11-21 15:00", "Haircut")]
    svc.update_event("greg", added["id"], title="Haircut (moved)", start="2026-11-21 16:00")
    assert titles(svc.list_events("greg", "2026-11-21")) == [("2026-11-21 16:00", "Haircut (moved)")]
    svc.cancel_event("greg", added["id"])
    assert titles(svc.list_events("greg", "2026-11-21")) == []


def test_no_version_means_no_cache(svc):
    svc.dav.frozen = ""             # changed within the last second, or the server would not say
    svc.list_events("greg", "2026-11-21")
    svc.list_events("greg", "2026-11-21")
    assert svc.dav.reads.count("/greg/personal/") == 2 and not svc.cache.chunks


def test_the_cache_never_shows_a_calendar_outside_the_readers_own(svc):
    svc.agents["outsider"] = {"token": "to", "timezone": "Europe/London", "busy": {},
                              "full": {"household": {"path": "/household/calendar/", "label": "Household"}}}
    svc.add_event("greg", "personal", "Doctor about my knee", "2026-11-21 10:00", location="Surgery")
    assert titles(svc.list_events("greg", "2026-11-21")) == [("2026-11-21 10:00", "Doctor about my knee")]
    assert any(path == "/greg/personal/" for path, _ in svc.cache.chunks)   # Greg's calendar is cached now
    svc.dav.reads.clear()
    for who in ("outsider", "household"):                             # no access / busy times only
        out = svc.list_events(who, "2026-11-21")
        assert out["events"] == [] and "knee" not in json.dumps(out) and "Surgery" not in json.dumps(out)
    assert "busy" not in svc.list_events("outsider", "2026-11-21")
    assert svc.list_events("household", "2026-11-21")["busy"][0]["who"] == "Greg"
    assert "/greg/personal/" not in svc.dav.reads                     # the busy times came from the cache
    free = svc.find_free_time("outsider", "2026-11-21", "2026-11-21", 60, "09:00", "12:00")
    assert free["free"] == [{"from": "2026-11-21 09:00", "until": "2026-11-21 12:00"}]   # his time is not seen
    with pytest.raises(cs.Refused):
        svc.cancel_event("outsider", "personal/" + next(iter(svc.dav.cals["/greg/personal/"])))


def test_calendars_are_read_at_the_same_time(svc):
    import threading
    import time
    lock, now, most = threading.Lock(), [0], [0]
    plain = svc.dav.items

    def slow(path, start, end):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        time.sleep(0.2)
        with lock:
            now[0] -= 1
        return plain(path, start, end)
    svc.dav.items = slow
    svc.agents["greg"]["read"] = {"bere": {"path": "/bere/personal/", "label": "Béré"}}
    t0 = time.perf_counter()
    svc.list_events("greg", "2026-11-21")
    assert most[0] == 3 and time.perf_counter() - t0 < 0.5


def test_months_and_cache_bounds():
    utc = dt.timezone.utc
    assert cs.months(dt.datetime(2026, 11, 30, 23, tzinfo=utc), dt.datetime(2027, 1, 2, tzinfo=utc)) == [
        dt.datetime(2026, 11, 1, tzinfo=utc), dt.datetime(2026, 12, 1, tzinfo=utc), dt.datetime(2027, 1, 1, tzinfo=utc)]
    c = cs.EventCache(size=2)
    for i in range(3):
        c.put("/p/", dt.datetime(2026, i + 1, 1, tzinfo=utc), "v", [])
    assert len(c.chunks) == 2 and c.get("/p/", dt.datetime(2026, 1, 1, tzinfo=utc), "v") is None
    march = dt.datetime(2026, 3, 1, tzinfo=utc)
    assert c.get("/p/", march, "v") == [] and c.get("/p/", march, "w") is None and c.get("/p/", march, "") is None


def test_caldav_version_and_items():
    import email.utils
    import time
    sent = []
    when = [time.time() - 60]

    def http(method, url, headers, body):
        sent.append((method, body.decode() if body else ""))
        if method == "PROPFIND":
            stamp_ = email.utils.formatdate(when[0], usegmt=True)
            return 207, {}, (f'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/p/</D:href><D:propstat><D:prop>'
                             f'<D:getlastmodified>{stamp_}</D:getlastmodified></D:prop></D:propstat></D:response>'
                             f'</D:multistatus>').encode()
        return 207, {}, (f'<D:multistatus xmlns:D="DAV:" xmlns:C="{cs.CALDAV}"><D:response><D:href>/p/a%40b.ics</D:href>'
                         f'<D:propstat><D:prop><C:calendar-data>BEGIN:VCALENDAR</C:calendar-data></D:prop></D:propstat>'
                         f'</D:response></D:multistatus>').encode()
    dav = cs.CalDAV("http://x", "u", "p", http=http)
    assert dav.version("/p/").endswith("GMT")
    when[0] = time.time()                                             # changed just now: not trusted yet
    assert dav.version("/p/") == ""
    assert dav.items("/p/", NOW, NOW + dt.timedelta(days=1)) == [("a@b.ics", "BEGIN:VCALENDAR")]
    assert "expand" not in sent[-1][1] and "time-range" in sent[-1][1]
