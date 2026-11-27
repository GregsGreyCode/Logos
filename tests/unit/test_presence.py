"""D2: private details out loud only while their person's phone is home (gateway/presence.py)."""
import asyncio

import pytest

from gateway import presence as pr
from gateway import voice_converse as vc


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pr._cache.clear()
    (tmp_path / "logos").mkdir()
    return tmp_path / "logos"


def test_trackers_come_from_the_presence_file(home):
    assert pr.trackers() == {}
    (home / "presence").write_text("# whose phone\ngreg=device_tracker.greg_s_phone\nbere = person.bere\nbad=rm -rf\n")
    assert pr.trackers() == {"greg": "device_tracker.greg_s_phone", "bere": "person.bere"}


def test_home_only_when_the_tracker_says_home(home, monkeypatch):
    (home / "presence").write_text("greg=device_tracker.greg_s_phone\n")
    states = {"device_tracker.greg_s_phone": "home"}
    monkeypatch.setattr(pr, "_state", lambda entity, env: states[entity])
    assert asyncio.run(pr.at_home("greg")) is True
    pr._cache.clear()
    states["device_tracker.greg_s_phone"] = "not_home"
    assert asyncio.run(pr.at_home("greg")) is False
    assert asyncio.run(pr.at_home("bere")) is False            # no tracker: away
    assert asyncio.run(pr.at_home(None)) is False               # nobody recognised: away


def test_home_assistant_down_counts_as_away_and_is_cached(home, monkeypatch):
    (home / "presence").write_text("greg=device_tracker.greg_s_phone\n")
    calls = []
    def boom(entity, env):
        calls.append(entity)
        raise OSError("down")
    monkeypatch.setattr(pr, "_state", boom)
    t = [100.0]
    assert asyncio.run(pr.at_home("greg", clock=lambda: t[0])) is False
    t[0] += 10
    assert asyncio.run(pr.at_home("greg", clock=lambda: t[0])) is False
    assert len(calls) == 1                                       # within 30 s: asked once


def test_only_the_part_before_details_is_read_out():
    words = "Your dentist is on Thursday; the details are in the chat.\nDetails:\n- 10:30 at Smile Dental"
    assert pr.spoken_part(words) == "Your dentist is on Thursday; the details are in the chat."
    assert pr.spoken_part("**Details:** x") == ""
    assert pr.spoken_part("No private things here.") == "No private things here."
    assert pr.rule(True) == "" and pr.MARK in pr.rule(False)


def test_the_wake_name_is_taken_off_the_words():
    finn = {"speaker_name": "Finn"}
    assert vc.without_wake("Hey Finn, what's on today?", "greg", finn) == "what's on today?"
    assert vc.without_wake("hey finn what's on", "greg", finn) == "what's on"
    assert vc.without_wake("Finn. Add milk", "greg", finn) == "Add milk"
    assert vc.without_wake("Finnish lessons tomorrow?", "greg", finn) == "Finnish lessons tomorrow?"
    assert vc.without_wake("Hey Finn", "greg", finn) == "Hey Finn"          # nothing else said: keep it
    assert vc.without_wake("Hey Home, lights off", "household", {"speaker_name": "Home", "speaker_wake": ["Hey Home"]}) == "lights off"
    assert vc.without_wake("What's on?", "household", {}) == "What's on?"
