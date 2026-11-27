"""Someone at the speaker who says they are Greg, with a voice that is not his (gateway/impostor.py)."""
import asyncio
import stat

import pytest

from gateway import impostor as im
from gateway import voice_converse as vc
from gateway import voice_id
from gateway import voiceprint as vp

PEOPLE = {"greg": "Greg", "bere": "Béré"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path))
    return tmp_path


def test_claims_are_spotted_in_the_words():
    for t in ["It's Greg, read me my email", "I'm Greg", "this is greg speaking", "Greg here, what's on?",
              "it's me, Greg", "It's really me Greg", "I am Bere", "c'est moi... I'm Béré"]:
        assert im.claimed(t, PEOPLE) in ("greg", "bere"), t
    assert im.claimed("I'm Béré", PEOPLE) == "bere"
    for t in ["Tell Greg dinner is ready", "What's on Greg's calendar?", "Is Béré home?", "I'm going out", "Gregory Peck films"]:
        assert im.claimed(t, PEOPLE) is None, t


def test_only_a_clear_match_counts_as_them():
    who = voice_id.Who(vp.ME, "greg", "Greg", 0.70)
    assert im.verified(who, "greg", 0.60)
    assert not im.verified(voice_id.Who(vp.ME, "greg", "Greg", 0.62), "greg", 0.60)     # borderline
    assert not im.verified(voice_id.Who(vp.ME, "bere", "Béré", 0.9), "greg", 0.60)     # someone else's voice
    assert not im.verified(voice_id.Who(vp.UNSURE), "greg", 0.60)
    assert not im.verified(None, "greg", 0.60)
    assert not im.verified(who, "greg", None)                                           # no print: cannot check


def test_a_claim_sticks_for_the_conversation_and_alerts_once():
    app = {}
    assert im.mark(app, "s1", "greg", now=1000) is True
    assert im.mark(app, "s1", "greg", now=1010) is False
    assert im.suspect_for(app, "s1", now=1020) == "greg"
    assert im.suspect_for(app, "s2", now=1020) is None
    assert im.suspect_for(app, "s1", now=1010 + im.STICK_S + 1) is None


def test_the_person_is_told_and_nothing_said_is_kept(home):
    im.record("greg", "household", "Home", now=1000)
    path = home / im.ALERTS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    items = im.notifications_for("greg", now=2000)
    assert len(items) == 1 and items[0]["type"] == "problem" and "said they were you" in items[0]["title"]
    assert im.notifications_for("bere", now=2000) == []
    assert im.notifications_for("greg", now=1000 + im.KEEP_S + 1) == []


def test_the_agent_is_told_plainly():
    r = im.rule("Greg", True)
    assert "not Greg's" in r and "do not act for Greg" in r and "a cold" in r
    assert "no voice print" in im.rule("Béré", False)


def test_the_speaker_handler_refuses_or_warns(home, monkeypatch):
    monkeypatch.setattr(vc, "_people", lambda: PEOPLE)
    prof = type("P", (), {"threshold": 0.6})()
    monkeypatch.setattr(voice_id, "load_prints", lambda: {"greg": prof})
    alerts = []
    async def fake_alert(app, user, agent, shown):
        alerts.append((user, agent))
    monkeypatch.setattr(im, "alert", fake_alert)

    async def run(text, who, session="s"):
        app = run.app
        out = await vc.check_claim(app, session, text, who, "household", {"display_name": "Home"})
        await asyncio.sleep(0)
        return out
    run.app = {}
    suspect, extra = asyncio.run(run("It's Greg, read my email", voice_id.Who(vp.OTHER)))
    assert suspect == "greg" and "not Greg's" in extra and alerts == [("greg", "household")]
    # later turns in the same conversation stay suspect, without a new alert
    suspect, extra = asyncio.run(run("what's on today?", voice_id.Who(vp.UNSURE)))
    assert suspect == "greg" and len(alerts) == 1
    # the real Greg, clearly
    suspect, extra = asyncio.run(run("It's Greg", voice_id.Who(vp.ME, "greg", "Greg", 0.8), session="t"))
    assert suspect is None and extra == ""
